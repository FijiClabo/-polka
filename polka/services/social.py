"""Друзья, толчки, пары и что о ком можно показывать.

Другу видно: имя, аватар, текущая книга, стрик, статус дня, ачивки, полка.
Пересказы и конспекты друзьям не отдаются никогда. Напарнику — только при той же книге
и только за ту часть, которую смотрящий уже сдал сам.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from core import rules
from db.models import (
    Book,
    DayResult,
    Enrollment,
    FriendNudge,
    Friendship,
    Pair,
    Retelling,
    Run,
    Segment,
    User,
)
from services.common import Outbox, OutMsg, allow_social, log_event, random_code, today_for
from services.progress import accepted_by_day, load_view
from services.runs import current_enrollment, enroll

# --------------------------------------------------------------------------- дружба


async def are_friends(session: AsyncSession, a: int, b: int) -> bool:
    if a == b:
        return False
    low, high = rules.friendship_key(a, b)
    return bool(
        await session.scalar(select(Friendship.id).where(Friendship.user_low_id == low, Friendship.user_high_id == high))
    )


async def friend_ids(session: AsyncSession, user_id: int) -> list[int]:
    rows = await session.execute(
        select(Friendship.user_low_id, Friendship.user_high_id).where(
            or_(Friendship.user_low_id == user_id, Friendship.user_high_id == user_id)
        )
    )
    return [h if lo == user_id else lo for lo, h in rows.all()]


async def add_friend_by_code(
    session: AsyncSession, user: User, code: str, *, is_new_user: bool, outbox: Outbox
) -> User | None:
    """Переход по личной ссылке: дружба сразу, без подтверждения. Повторный переход ничего не меняет."""
    inviter = await session.scalar(select(User).where(User.friend_code == code))
    if inviter is None or inviter.id == user.id:
        return None
    if await are_friends(session, inviter.id, user.id):
        return None
    low, high = rules.friendship_key(inviter.id, user.id)
    try:
        async with session.begin_nested():
            session.add(Friendship(user_low_id=low, user_high_id=high, invited_by_id=inviter.id))
    except IntegrityError:
        return None
    if is_new_user and user.invited_by_id is None:
        user.invited_by_id = inviter.id
    await log_event(session, "friend_added", user.id, kind="new" if is_new_user else "existing", inviter_id=inviter.id)
    if not inviter.bot_blocked:
        outbox.add(OutMsg(inviter.tg_id, texts.friend_joined(user.display_name), kind="friend",
                          buttons=[[{"text": "Друзья", "webapp": "friends"}]]))
    return inviter


# --------------------------------------------------------------------------- публичный статус


@dataclass
class PublicStatus:
    user_id: int
    name: str
    photo_url: str | None
    book_title: str | None
    book_author: str | None
    spine_color: str | None
    streak: int
    today: str  # done | reading | burned | idle | finished | waiting
    plan_day: int | None
    plan_days: int | None
    done_at: str | None = None


async def public_status(session: AsyncSession, user: User) -> PublicStatus:
    enr = await current_enrollment(session, user.id)
    st = PublicStatus(user.id, user.display_name, user.photo_url, None, None, None, 0, "idle", None, None)
    if enr is None:
        return st
    book = await session.get(Book, enr.book_id) if enr.book_id else None
    if book:
        st.book_title, st.book_author, st.spine_color = book.title, book.author, book.spine_color
    st.streak = enr.streak
    view = await load_view(session, user, enr)
    st.plan_day, st.plan_days = view.plan_day, enr.plan_days
    if view.state == "finished":
        st.today = "finished"
    elif view.state in ("to_read", "clarify", "checking"):
        today = today_for(user)
        yesterday = await session.scalar(
            select(DayResult.result).where(DayResult.enrollment_id == enr.id, DayResult.user_day < today)
            .order_by(DayResult.user_day.desc()).limit(1)
        )
        st.today = "burned" if (yesterday == rules.MISSED and enr.streak == 0) else "reading"
    elif view.state == "done_today":
        st.today = "done"
        r = await session.scalar(
            select(Retelling.decided_at).where(
                Retelling.enrollment_id == enr.id, Retelling.user_day == view.today, Retelling.verdict == "accepted"
            ).order_by(Retelling.id).limit(1)
        )
        if r:
            from core.days import local_now

            st.done_at = local_now(r, user.timezone).strftime("%H:%M")
    elif view.state in ("not_started", "waiting_start", "plan_needed", "no_book", "awaiting_payment", "parsing"):
        st.today = "waiting"
    return st


# --------------------------------------------------------------------------- толчки


async def nudge(session: AsyncSession, sender: User, target: User, outbox: Outbox, *, kind: str = "friend") -> str:
    """Толкнуть друга/напарника: не чаще раза в день на пару «от кого — кому»."""
    if sender.id == target.id:
        return "self"
    if kind == "friend" and not await are_friends(session, sender.id, target.id):
        if not await is_partner(session, sender.id, target.id):
            return "not_friends"
    day = today_for(sender)
    already = await session.scalar(
        select(FriendNudge.id).where(
            FriendNudge.from_user_id == sender.id, FriendNudge.to_user_id == target.id, FriendNudge.user_day == day
        )
    )
    st = await public_status(session, target)
    ok, reason = rules.can_nudge(bool(already), True, st.today == "done")
    if not ok:
        return reason
    delivered = target.nudges_enabled and await allow_social(session, target, sender.id)
    try:
        async with session.begin_nested():
            session.add(FriendNudge(from_user_id=sender.id, to_user_id=target.id, user_day=day, kind=kind, delivered=delivered))
    except IntegrityError:
        return "already"
    if delivered:
        outbox.add(OutMsg(target.tg_id, texts.nudge_received(sender.display_name), kind="nudge",
                          buttons=[[{"text": "Открыть отрезок", "webapp": "today"}]]))
    await log_event(session, "friend_nudge_sent" if kind == "friend" else "nudge_sent", sender.id, to=target.id,
                    delivered=delivered)
    return "ok"


async def nudged_today(session: AsyncSession, sender: User) -> set[int]:
    day = today_for(sender)
    return set(
        await session.scalars(select(FriendNudge.to_user_id).where(FriendNudge.from_user_id == sender.id, FriendNudge.user_day == day))
    )


# --------------------------------------------------------------------------- пары


async def is_partner(session: AsyncSession, a: int, b: int) -> bool:
    return bool(
        await session.scalar(
            select(Pair.id).where(
                or_((Pair.user_a_id == a) & (Pair.user_b_id == b), (Pair.user_a_id == b) & (Pair.user_b_id == a))
            )
        )
    )


async def get_pair_for(session: AsyncSession, enr: Enrollment | None) -> tuple[Pair | None, User | None, Enrollment | None]:
    if enr is None or enr.pair_id is None:
        return None, None, None
    pair = await session.get(Pair, enr.pair_id)
    if pair is None:
        return None, None, None
    pid = pair.user_b_id if pair.user_a_id == enr.user_id else pair.user_a_id
    partner = await session.get(User, pid)
    p_enr = await session.scalar(select(Enrollment).where(Enrollment.pair_id == pair.id, Enrollment.user_id == pid))
    return pair, partner, p_enr


async def make_pair(session: AsyncSession, run: Run, ea: Enrollment, eb: Enrollment) -> Pair | None:
    if ea.user_id == eb.user_id or ea.run_id != eb.run_id:
        return None
    if ea.pair_id or eb.pair_id:
        return None
    pair = Pair(run_id=run.id, user_a_id=ea.user_id, user_b_id=eb.user_id, streak=0, best_streak=0)
    session.add(pair)
    await session.flush()
    ea.pair_id = pair.id
    eb.pair_id = pair.id
    await log_event(session, "pair_created", ea.user_id, run.id, partner=eb.user_id)
    return pair


async def join_pair_by_code(session: AsyncSession, user: User, code: str, outbox: Outbox) -> tuple[str, User | None]:
    inviter_enr = await session.scalar(select(Enrollment).where(Enrollment.pair_code == code))
    if inviter_enr is None:
        return "not_found", None
    inviter = await session.get(User, inviter_enr.user_id)
    if inviter is None or inviter.id == user.id:
        return "self", None
    if inviter_enr.pair_id:
        return "taken", inviter
    run = await session.get(Run, inviter_enr.run_id)
    my = await session.scalar(select(Enrollment).where(Enrollment.run_id == run.id, Enrollment.user_id == user.id))
    if my is None:
        if run.kind == "sprint":
            my = await enroll(session, user, run, "active")
        else:
            my = await enroll(session, user, run, "invited")
    if my.pair_id:
        return "already_paired", inviter
    pair = await make_pair(session, run, inviter_enr, my)
    if pair is None:
        return "error", inviter
    outbox.add(OutMsg(inviter.tg_id, texts.pair_joined(user.display_name), kind="pair",
                      buttons=[[{"text": "Напарник", "webapp": "friends"}]]))
    # пара — это ещё и друзья
    if not await are_friends(session, inviter.id, user.id):
        low, high = rules.friendship_key(inviter.id, user.id)
        session.add(Friendship(user_low_id=low, user_high_id=high, invited_by_id=inviter.id))
    return "ok", inviter


async def auto_pairs(session: AsyncSession, run: Run, seed: int | None = None) -> list[Pair]:
    """Случайно распределить по парам половину оплативших без пары. Вторая половина — без пары (контроль)."""
    free = list(
        await session.scalars(
            select(Enrollment).where(
                Enrollment.run_id == run.id, Enrollment.status.in_(("paid", "active")), Enrollment.pair_id.is_(None)
            )
        )
    )
    rnd = random.Random(seed)
    rnd.shuffle(free)
    n = (len(free) // 2) // 2 * 2  # половина, округлённая вниз до чётного
    chosen = free[:n]
    pairs = []
    for i in range(0, n, 2):
        p = await make_pair(session, run, chosen[i], chosen[i + 1])
        if p:
            pairs.append(p)
    await log_event(session, "pairs_auto", None, run.id, pairs=len(pairs), solo=len(free) - n)
    return pairs


async def ensure_pair_code(session: AsyncSession, enr: Enrollment) -> str:
    if not enr.pair_code:
        enr.pair_code = random_code(10)
        await session.flush()
    return enr.pair_code


# --------------------------------------------------------------------------- пересказы напарника


async def partner_feed(session: AsyncSession, viewer: User, viewer_enr: Enrollment) -> dict:
    """Лента пересказов напарника с отметкой locked. Текст закрытых не отдаётся."""
    pair, partner, p_enr = await get_pair_for(session, viewer_enr)
    if not pair or not partner or not p_enr:
        return {"same_book": False, "items": []}
    my_book = await session.get(Book, viewer_enr.book_id) if viewer_enr.book_id else None
    p_book = await session.get(Book, p_enr.book_id) if p_enr.book_id else None
    same = bool(
        my_book and p_book and rules.same_book(
            my_book.title_norm, my_book.author_norm, my_book.file_hash,
            p_book.title_norm, p_book.author_norm, p_book.file_hash,
        )
    )
    if not same:
        return {"same_book": False, "items": []}
    my_acc = await accepted_by_day(session, viewer_enr)
    my_progress = 0.0
    if my_acc:
        seg = await session.scalar(select(Segment).where(Segment.book_id == my_book.id, Segment.day_number == max(my_acc)))
        my_progress = seg.pos_to if seg else 0.0
    rows = await session.execute(
        select(Retelling, Segment)
        .join(Segment, Segment.id == Retelling.segment_id)
        .where(Retelling.enrollment_id == p_enr.id, Retelling.verdict == "accepted", Segment.book_id == p_book.id)
        .order_by(Segment.day_number)
    )
    items = []
    seen = set()
    for r, s in rows.all():
        if s.day_number in seen:
            continue
        seen.add(s.day_number)
        visible = rules.can_view_partner_retelling(True, s.pos_to, my_progress)
        items.append({
            "day_number": s.day_number,
            "title": s.title,
            "pos_to": s.pos_to,
            "locked": not visible,
            "text": r.raw_text if visible else None,
            "source": r.source,
        })
    return {"same_book": True, "items": items}
