"""Друзья, толчки, пары и что о ком можно показывать.

Другу и напарнику видно: имя, аватар, текущая книга, стрик, статус дня, ачивки, полка.
Тексты пересказов не хранятся и никому не показываются.
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
    User,
)
from services.common import Outbox, OutMsg, allow_social, log_event, random_code, today_for
from services.progress import load_view
from services.runs import (
    OPEN_STATUSES,
    current_enrollment,
    enroll,
    ensure_enrollment,
    sprint_used,
    start_sprint,
)

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
    if kind == "partner":
        _, partner, _ = await get_pair_for(session, await current_enrollment(session, sender.id))
        if partner is None or partner.id != target.id:
            return "not_friends"  # напоминать можно только нынешнему напарнику
    elif not await are_friends(session, sender.id, target.id):
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
    if st.today not in ("reading", "burned"):
        return "idle"  # человек ещё не начал или уже дочитал — толкать не к чему
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
    """Пара — это два участия, не обязательно в одном забеге: личные забеги стартуют в любой день."""
    if ea.user_id == eb.user_id:
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


async def release_stale_pair(session: AsyncSession, enr: Enrollment | None) -> None:
    """Напарник уже закончил своё участие (дочитал, выбыл, вернул деньги) — освобождаем место для новой пары."""
    if enr is None or enr.pair_id is None:
        return
    other = await session.scalar(
        select(Enrollment).where(Enrollment.pair_id == enr.pair_id, Enrollment.id != enr.id).limit(1)
    )
    if other is None or other.status not in OPEN_STATUSES:
        enr.pair_id = None
        if other is not None:
            other.pair_id = None


async def join_pair_by_code(session: AsyncSession, user: User, code: str, outbox: Outbox) -> tuple[str, User | None]:
    inviter_enr = await session.scalar(select(Enrollment).where(Enrollment.pair_code == code))
    if inviter_enr is None:
        return "not_found", None
    inviter = await session.get(User, inviter_enr.user_id)
    if inviter is None or inviter.id == user.id:
        return "self", None
    await release_stale_pair(session, inviter_enr)
    if inviter_enr.pair_id:
        return "taken", inviter
    if inviter_enr.status not in OPEN_STATUSES:
        return "not_found", None
    run = await session.get(Run, inviter_enr.run_id)
    my = await _enrollment_to_pair(session, user, run)
    await release_stale_pair(session, my)
    if my.pair_id:
        return "already_paired", inviter
    pair = await make_pair(session, run, inviter_enr, my)
    if pair is None:
        return "error", inviter
    outbox.add(OutMsg(inviter.tg_id, texts.pair_joined(user.display_name), kind="pair",
                      buttons=[[{"text": "Напарник", "webapp": "friends"}]]))
    # в друзья напарник не добавляется: напарнику виден только прогресс, а друзьям — ещё книга и полка
    return "ok", inviter


async def _enrollment_to_pair(session: AsyncSession, user: User, run: Run) -> Enrollment:
    """С каким участием приглашённый встаёт в пару.

    Уже в этом забеге — с ним. Идёт свой забег — со своим. Иначе: в спринт друга (если свой спринт
    ещё не был), в групповой забег друга, а если он закрыт — в новый личный забег.
    """
    same = await session.scalar(select(Enrollment).where(Enrollment.run_id == run.id, Enrollment.user_id == user.id))
    if same is not None and same.status in OPEN_STATUSES:
        return same
    cur = await current_enrollment(session, user.id)
    if cur is not None and cur.status in OPEN_STATUSES:
        return cur
    if run.kind == "sprint" and not await sprint_used(session, user.id):
        enr = await start_sprint(session, user)
        if enr is not None:
            return enr
    if run.kind == "main" and run.status in ("open", "active") and same is None:
        return await enroll(session, user, run, "invited")
    return await ensure_enrollment(session, user)


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


async def remove_friend(session: AsyncSession, user: User, friend_id: int) -> bool:
    """Убрать из друзей (в обе стороны): больше не видим книги, стрика и полки друг друга."""
    low, high = rules.friendship_key(user.id, friend_id)
    f = await session.scalar(select(Friendship).where(Friendship.user_low_id == low, Friendship.user_high_id == high))
    if f is None:
        return False
    await session.delete(f)
    await log_event(session, "friend_removed", user.id, friend=friend_id)
    return True


async def leave_pair(session: AsyncSession, enr: Enrollment | None) -> bool:
    """Выйти из пары: общий стрик заканчивается, каждый читает дальше сам."""
    if enr is None or enr.pair_id is None:
        return False
    pair_id = enr.pair_id
    for e in await session.scalars(select(Enrollment).where(Enrollment.pair_id == pair_id)):
        e.pair_id = None
    await log_event(session, "pair_left", enr.user_id, pair=pair_id)
    return True
