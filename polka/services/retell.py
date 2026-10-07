"""Сдача пересказа: подготовка → проверка ИИ (вне транзакции) → применение вердикта.

Так долгий запрос к ИИ не держит транзакцию, а при сбое пересказ остаётся в статусе pending
и его доделает планировщик («принял, проверю чуть позже», день не сгорает).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from ai.checker import CheckInput, check_retelling
from ai.llm import LLMUnavailable
from ai.verdict import Verdict, fallback_accept
from core import rules
from core.achievements import TRIGGER_ACCEPTED, TRIGGER_FRIEND_FIRST, AchievementContext
from db.models import Book, DayResult, DialogTurn, Enrollment, Pair, Retelling, Segment, User
from services.achievements import award
from services.common import Outbox, OutMsg, age, allow_initiative, log_event, now, today_for
from services.progress import accepted_by_day, close_pending_days, finish_enrollment, load_view
from services.runs import current_enrollment

log = logging.getLogger(__name__)

NOT_SUBMITTABLE = {
    "no_run", "awaiting_payment", "no_book", "parsing", "parse_failed", "plan_needed", "waiting_start",
    "not_started", "finished", "expired", "refunded", "done_today",
}


@dataclass
class Prepared:
    retelling_id: int
    check: CheckInput


@dataclass
class RetellOutcome:
    status: str  # accepted | clarify | rejected | queued | checking | too_short | rate_limited | <состояние дня>
    reply: str = ""
    question: str | None = None
    retelling_id: int | None = None
    segment_title: str = ""
    day_number: int | None = None
    streak: int = 0
    streak_grew: bool = False
    pair_streak: int | None = None
    partner_name: str | None = None
    partner_done: bool | None = None
    achievements: list[str] = field(default_factory=list)
    finished: bool = False
    verified: bool = True
    can_submit_more: bool = False
    next_title: str | None = None


# --------------------------------------------------------------------------- вход для ИИ


async def build_check_input(session: AsyncSession, r: Retelling) -> CheckInput:
    seg = await session.get(Segment, r.segment_id) if r.segment_id else None
    book = await session.get(Book, seg.book_id) if seg else None
    prev: list[str] = []
    if seg and book:
        prev = list(
            await session.scalars(
                select(Segment.summary)
                .where(Segment.book_id == book.id, Segment.day_number < seg.day_number, Segment.summary.is_not(None))
                .order_by(Segment.day_number)
            )
        )
    turns = list(await session.scalars(select(DialogTurn).where(DialogTurn.retelling_id == r.id).order_by(DialogTurn.id)))
    dialog = [(t.role, t.text) for t in turns]
    last_user = next((t for t in reversed(dialog) if t[0] == "user"), ("user", r.raw_text))
    if dialog and dialog[-1][0] == "user":
        dialog = dialog[:-1]
    pages = f"стр. {seg.page_from}–{seg.page_to}" if seg else ""
    return CheckInput(
        title=book.title if book else "",
        author=book.author if book else "",
        segment_title=seg.title if seg else "",
        day_number=seg.day_number if seg else 0,
        pages=pages,
        segment_text=seg.text if (seg and book and book.has_text) else None,
        segment_summary=seg.summary if seg else None,
        previous_summaries=[p for p in prev if p],
        retelling=last_user[1],
        dialog=dialog,
        clarify_count=r.clarify_count,
    )


# --------------------------------------------------------------------------- фаза 1


async def start_submission(
    session: AsyncSession, user: User, text: str, *, source: str = "text", via: str = "bot",
    voice_file_id: str | None = None, voice_duration: int | None = None, outbox: Outbox | None = None,
) -> Prepared | RetellOutcome:
    enr = await current_enrollment(session, user.id, lock=True)
    if enr is None:
        return RetellOutcome("no_run")
    await close_pending_days(session, enr, user, outbox)
    view = await load_view(session, user, enr)
    if view.state in NOT_SUBMITTABLE:
        out = RetellOutcome(view.state, streak=enr.streak)
        nxt = view.next_segment
        out.next_title = nxt.title if nxt else None
        return out
    if view.state == "checking":
        return RetellOutcome("checking", retelling_id=view.open_retelling.id if view.open_retelling else None)

    recent = await session.scalar(
        select(func.count(Retelling.id)).where(
            Retelling.enrollment_id == enr.id, Retelling.created_at >= now_real_minus(hours=1)
        )
    )
    if (recent or 0) >= rules.MAX_ATTEMPTS_PER_HOUR:
        return RetellOutcome("rate_limited")

    text = (text or "").strip()
    seg = view.segment
    assert seg is not None
    if view.state == "clarify" and view.open_retelling is not None:
        r = view.open_retelling
        if len(text) < 2:
            return RetellOutcome("too_short", segment_title=seg.title, day_number=seg.day_number)
        session.add(DialogTurn(retelling_id=r.id, role="user", text=text[:4000]))
        r.raw_text = f"{r.raw_text}\n— {text}"[:20000]
        r.verdict = "pending"
    else:
        if not rules.is_retell_attempt(text):
            return RetellOutcome("too_short", segment_title=seg.title, day_number=seg.day_number)
        attempts = await session.scalar(
            select(func.count(Retelling.id)).where(Retelling.enrollment_id == enr.id, Retelling.segment_id == seg.id)
        )
        r = Retelling(
            enrollment_id=enr.id, segment_id=seg.id, user_day=view.today, source=source, via=via,
            raw_text=text[:20000], voice_file_id=voice_file_id, voice_duration_sec=voice_duration,
            verdict="pending", attempt_no=(attempts or 0) + 1, clarify_count=0, verified=False,
        )
        session.add(r)
        await session.flush()
        session.add(DialogTurn(retelling_id=r.id, role="user", text=text[:4000]))
    await session.flush()
    await log_event(session, "retelling_submitted", user.id, enr.run_id, source=source, length=len(text), via=via)
    return Prepared(r.id, await build_check_input(session, r))


def now_real_minus(**kw):
    from core import clock

    return clock.real_now() - timedelta(**kw)


# --------------------------------------------------------------------------- фаза 2


async def run_check(prep: Prepared) -> Verdict | None:
    try:
        return await check_retelling(prep.check)
    except LLMUnavailable as e:
        log.warning("AI unavailable for retelling %s: %s", prep.retelling_id, e)
        return None


# --------------------------------------------------------------------------- фаза 3


async def finish_submission(
    session: AsyncSession, retelling_id: int, verdict: Verdict | None, outbox: Outbox | None = None
) -> RetellOutcome:
    r = await session.get(Retelling, retelling_id)
    if r is None:
        return RetellOutcome("no_run")
    enr = await session.scalar(select(Enrollment).where(Enrollment.id == r.enrollment_id).with_for_update(of=Enrollment))
    user = await session.get(User, enr.user_id)
    seg = await session.get(Segment, r.segment_id) if r.segment_id else None
    base = RetellOutcome("queued", retelling_id=r.id, segment_title=seg.title if seg else "",
                         day_number=seg.day_number if seg else None, streak=enr.streak)
    if r.verdict != "pending":  # уже обработан (например, планировщиком)
        return await _outcome_from(session, r, enr, user, base)
    if verdict is None:
        from core import clock

        r.pending_attempts += 1
        r.decided_at = clock.real_now()  # время последней попытки — реальное, для очереди повторов
        return base
    return await apply_verdict(session, r, enr, user, verdict, outbox, base)


async def apply_verdict(
    session: AsyncSession, r: Retelling, enr: Enrollment, user: User, v: Verdict, outbox: Outbox | None,
    base: RetellOutcome,
) -> RetellOutcome:
    r.verdict = v.verdict
    r.ai_reply = v.reply
    r.ai_question = v.question
    r.confidence = v.confidence
    r.verified = v.verified
    r.provider = v.provider
    r.decided_at = now()
    if v.verdict == "clarify":
        # уточнение: текст и диалог нужны до окончательного ответа
        ai_text = v.reply + (f"\n{v.question}" if v.question else "")
        session.add(DialogTurn(retelling_id=r.id, role="ai", text=ai_text))
    else:
        await forget_texts(session, r)
    if v.fallback:
        await log_event(session, "ai_error", user.id, enr.run_id, retelling_id=r.id)
    await log_event(session, "verdict", user.id, enr.run_id, type=v.verdict, verified=v.verified,
                    attempt_no=r.attempt_no, clarify=r.clarify_count, provider=v.provider)
    base.reply, base.question, base.verified = v.reply, v.question, v.verified
    if v.verdict == "clarify":
        r.clarify_count += 1
        base.status = "clarify"
        return base
    if v.verdict == "rejected":
        base.status = "rejected"
        return base
    info = await on_accepted(session, enr, user, r, outbox)
    base.status = "accepted"
    base.streak = enr.streak
    base.streak_grew = info["first_today"]
    base.achievements = info["achievements"]
    base.finished = info["finished"]
    base.pair_streak = info.get("pair_streak")
    base.partner_name = info.get("partner_name")
    base.partner_done = info.get("partner_done")
    base.can_submit_more = info.get("can_submit_more", False)
    base.next_title = info.get("next_title")
    return base


async def forget_texts(session: AsyncSession, r: Retelling) -> None:
    """Пересказы не храним: после окончательного вердикта текст, ответ ИИ и диалог удаляются.

    Остаётся только факт сдачи — день, результат, время — для стрика и прогресса.
    """
    r.raw_text = ""
    r.ai_reply = None
    r.ai_question = None
    r.note_for_summary = None
    await session.execute(delete(DialogTurn).where(DialogTurn.retelling_id == r.id))


async def _outcome_from(session: AsyncSession, r: Retelling, enr: Enrollment, user: User, base: RetellOutcome) -> RetellOutcome:
    base.status = r.verdict if r.verdict in ("accepted", "clarify", "rejected") else "queued"
    base.reply, base.question, base.verified = r.ai_reply or "", r.ai_question, r.verified
    base.streak = enr.streak
    return base


async def on_accepted(session: AsyncSession, enr: Enrollment, user: User, r: Retelling, outbox: Outbox | None) -> dict:
    info: dict = {"achievements": [], "finished": False, "first_today": False}
    day = r.user_day
    others_today = await session.scalar(
        select(func.count(Retelling.id)).where(
            Retelling.enrollment_id == enr.id, Retelling.user_day == day, Retelling.verdict == "accepted",
            Retelling.id != r.id,
        )
    )
    first_today = not others_today
    info["first_today"] = first_today
    enr.streak, enr.best_streak = rules.streak_after_accept(enr.streak, enr.best_streak, first_today)
    dr = await session.scalar(select(DayResult).where(DayResult.enrollment_id == enr.id, DayResult.user_day == day))
    if dr is None:
        session.add(DayResult(enrollment_id=enr.id, user_day=day, segment_id=r.segment_id, result=rules.DONE))
    elif dr.result != rules.DONE and r.overridden:
        dr.result = rules.DONE
    if enr.status == "paid":
        enr.status = "active"
    if first_today:
        await log_event(session, "day_done", user.id, enr.run_id, day=day.isoformat())
    await session.flush()

    total_accepted = await session.scalar(
        select(func.count(Retelling.id))
        .join(Enrollment, Enrollment.id == Retelling.enrollment_id)
        .where(Enrollment.user_id == user.id, Retelling.verdict == "accepted")
    )
    prev = await session.scalar(
        select(DayResult.result).where(DayResult.enrollment_id == enr.id, DayResult.user_day == day - timedelta(days=1))
    )
    pair = await session.get(Pair, enr.pair_id) if enr.pair_id else None
    ctx = AchievementContext(
        TRIGGER_ACCEPTED, total_accepted=total_accepted or 0, streak=enr.streak,
        prev_day_result=prev, pair_streak=pair.streak if pair else None,
    )
    info["achievements"] = await award(session, user, ctx, outbox)

    # друг по ссылке сдал первый пересказ → ачивка пригласившему
    if (total_accepted or 0) == 1 and user.invited_by_id:
        inviter = await session.get(User, user.invited_by_id)
        if inviter:
            got = await award(session, inviter, AchievementContext(TRIGGER_FRIEND_FIRST, invited_friend_first_accept=True), outbox)
            if got:
                await log_event(session, "friend_first_accept", inviter.id, friend_id=user.id)

    # напарник
    if pair:
        partner_id = pair.user_b_id if pair.user_a_id == user.id else pair.user_a_id
        partner = await session.get(User, partner_id)
        p_enr = await session.scalar(select(Enrollment).where(Enrollment.pair_id == pair.id, Enrollment.user_id == partner_id))
        info["pair_streak"] = pair.streak
        if partner and p_enr:
            info["partner_name"] = partner.display_name
            p_today = today_for(partner)
            p_done = bool(
                await session.scalar(
                    select(DayResult.id).where(
                        DayResult.enrollment_id == p_enr.id, DayResult.user_day == p_today, DayResult.result == rules.DONE
                    )
                )
            )
            info["partner_done"] = p_done
            if (
                first_today and not p_done and p_enr.status in ("paid", "active") and p_enr.plan_start_date
                and outbox is not None and await allow_initiative(session, partner, "partner")
            ):
                outbox.add(OutMsg(partner.tg_id, texts.partner_done(user.display_name), kind="partner",
                                  buttons=[[{"text": "Открыть отрезок", "webapp": "today"}]]))

    # финиш
    accepted = await accepted_by_day(session, enr)
    seg = await session.get(Segment, r.segment_id) if r.segment_id else None
    if seg:
        accepted.setdefault(seg.day_number, r)
    if enr.plan_days and rules.all_segments_done(set(accepted), enr.plan_days):
        got = await finish_enrollment(session, enr, user, outbox)
        info["achievements"] += got
        info["finished"] = True
    else:
        view = await load_view(session, user, enr)
        info["can_submit_more"] = view.state in ("to_read",)
        nxt = view.segment if view.state == "to_read" else view.next_segment
        info["next_title"] = nxt.title if nxt else None
    return info


async def force_accept_pending(
    session: AsyncSession, r: Retelling, enr: Enrollment, user: User, outbox: Outbox | None
) -> RetellOutcome:
    """ИИ так и не ответил — засчитываем без сверки, чтобы день не сгорел."""
    v = fallback_accept()
    base = RetellOutcome("accepted", retelling_id=r.id)
    out = await apply_verdict(session, r, enr, user, v, outbox, base)
    if outbox is not None:
        outbox.add(OutMsg(user.tg_id, texts.pending_resolved(out), kind="reply"))
    return out


async def override(session: AsyncSession, retelling_id: int, outbox: Outbox | None) -> RetellOutcome | None:
    """Ведущий вручную засчитывает ошибочно отклонённый пересказ; стрик пересчитывается."""
    r = await session.get(Retelling, retelling_id)
    if r is None:
        return None
    enr = await session.get(Enrollment, r.enrollment_id)
    user = await session.get(User, enr.user_id)
    if r.verdict == "accepted":
        return RetellOutcome("accepted", retelling_id=r.id, streak=enr.streak)
    r.verdict = "accepted"
    r.overridden = True
    r.decided_at = now()
    await forget_texts(session, r)
    await log_event(session, "override", user.id, enr.run_id, retelling_id=r.id)
    await on_accepted(session, enr, user, r, outbox)
    await recompute_streak(session, enr)
    if outbox is not None:
        outbox.add(OutMsg(user.tg_id, texts.override_notice(), kind="reply"))
    return RetellOutcome("accepted", retelling_id=r.id, streak=enr.streak)


async def recompute_streak(session: AsyncSession, enr: Enrollment) -> None:
    rows = list(await session.scalars(select(DayResult).where(DayResult.enrollment_id == enr.id).order_by(DayResult.user_day)))
    streak = best = 0
    for d in rows:
        if d.result == rules.DONE:
            streak += 1
        elif d.result == rules.MISSED:
            streak = 0
        best = max(best, streak)
    enr.streak = streak
    enr.best_streak = max(enr.best_streak, best)


# --------------------------------------------------------------------------- очередь отложенных


async def pending_to_retry(session: AsyncSession, limit: int = 20) -> list[int]:
    rows = list(
        await session.scalars(select(Retelling).where(Retelling.verdict == "pending").order_by(Retelling.id).limit(200))
    )
    out = []
    for r in rows:
        last = r.decided_at or r.created_at
        retry_after_fail = r.pending_attempts > 0 and age(last) > timedelta(minutes=2)
        stuck = r.pending_attempts == 0 and age(r.created_at) > timedelta(minutes=3)  # процесс упал посреди проверки
        if retry_after_fail or stuck:
            out.append(r.id)
        if len(out) >= limit:
            break
    return out
