"""Состояние дня, закрытие дней, общий стрик пары, финиш."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import rules
from core.achievements import TRIGGER_DAY_CLOSED, TRIGGER_FINISH, AchievementContext
from core.days import deadline_day, plan_day_number
from db.models import Book, DayResult, Enrollment, Pair, Retelling, Run, Segment, User
from services.common import Outbox, age, log_event, now, today_for

PENDING_GRACE = timedelta(hours=6)  # сколько ждём ИИ, прежде чем засчитать отложенный пересказ без сверки


@dataclass
class DayView:
    state: str
    user: User
    enrollment: Enrollment | None = None
    run: Run | None = None
    book: Book | None = None
    today: date | None = None
    plan_day: int | None = None
    plan_days: int | None = None
    segment: Segment | None = None
    next_segment: Segment | None = None
    accepted: dict[int, Retelling] = field(default_factory=dict)  # day_number → принятый пересказ
    accepted_today: int = 0
    limit: int = 1
    open_retelling: Retelling | None = None
    progress: float = 0.0
    starts_on: date | None = None
    deadline: date | None = None
    freezes_left: int = 0
    catching_up: bool = False
    today_result: str | None = None
    # extra: "" — отрезок дня; "more" — сегодняшний уже сдан, человек читает дальше; "ahead" — сегодня по плану
    # ничего не нужно (всё сдано заранее), открыт следующий отрезок
    extra: str = ""
    behind: int = 0  # сколько отрезков по плану не сдано, включая сегодняшний
    ahead: int = 0  # на сколько отрезков впереди плана
    paused_until: date | None = None  # пауза: первый день после неё

    @property
    def can_read_more(self) -> bool:
        """День сдан, но можно пересказать ещё отрезок: прочитано больше или догоняем."""
        return self.state == "done_today" and self.next_segment is not None and rules.can_submit_more_today(self.accepted_today)

    @property
    def done_today(self) -> bool:
        return self.accepted_today > 0


def paused_on(enr: Enrollment, d: date) -> bool:
    """День d — внутри паузы."""
    return bool(enr.paused_from and enr.paused_until and enr.paused_from <= d < enr.paused_until)


async def accepted_by_day(session: AsyncSession, enr: Enrollment) -> dict[int, Retelling]:
    if enr.book_id is None:
        return {}
    rows = await session.execute(
        select(Segment.day_number, Retelling)
        .join(Segment, Segment.id == Retelling.segment_id)
        .where(
            Retelling.enrollment_id == enr.id,
            Retelling.verdict == "accepted",
            Segment.book_id == enr.book_id,
        )
        .order_by(Retelling.id)
    )
    out: dict[int, Retelling] = {}
    for day_number, r in rows.all():
        out.setdefault(day_number, r)
    return out


async def segment_by_day(session: AsyncSession, book_id: int, day_number: int) -> Segment | None:
    return await session.scalar(select(Segment).where(Segment.book_id == book_id, Segment.day_number == day_number))


def effective_freezes(enr: Enrollment, plan_day: int | None) -> int:
    if not plan_day or plan_day < 1:
        return enr.freezes_left
    left, _ = rules.refresh_freezes(enr.freezes_left, enr.freezes_week_start, plan_day, enr.freezes_per_week or 1)
    return left


async def load_view(session: AsyncSession, user: User, enr: Enrollment | None) -> DayView:
    v = DayView(state="no_run", user=user, enrollment=enr)
    if enr is None:
        return v
    v.run = enr.run
    v.today = today_for(user)
    if enr.status == "refunded":
        v.state = "refunded"
        return v
    book = await session.get(Book, enr.book_id) if enr.book_id else None
    v.book = book
    if book is None:
        v.state = "no_book"
        return v
    if book.parse_status == "pending":
        v.state = "parsing"
        return v
    if book.parse_status == "failed":
        v.state = "parse_failed"
        return v
    if not enr.plan_days:
        v.state = "plan_needed"
        return v
    if enr.status == "invited":
        # книга и план готовы — осталось открыть доступ (оплата, абонемент или ведущий)
        v.state = "awaiting_payment"
        v.plan_days = enr.plan_days
        v.starts_on = enr.run.start_date if enr.run.kind == "main" else None
        return v
    v.plan_days = enr.plan_days
    v.accepted = await accepted_by_day(session, enr)
    if v.accepted:
        last = max(v.accepted)
        seg = await segment_by_day(session, book.id, last)
        v.progress = seg.pos_to if seg else len(v.accepted) / enr.plan_days
    if enr.status == "finished":
        v.state = "finished"
        return v
    if enr.status == "dropped":
        v.state = "expired"
        return v
    if enr.plan_start_date is None:
        v.state = "waiting_start"
        v.starts_on = enr.run.start_date
        return v
    v.starts_on = enr.plan_start_date
    v.deadline = deadline_day(enr.plan_start_date, enr.plan_days, enr.run.grace_days)
    plan_day = plan_day_number(enr.plan_start_date, v.today)
    v.plan_day = plan_day
    v.freezes_left = effective_freezes(enr, plan_day)
    if paused_on(enr, v.today):
        # пауза: ничего не ждём и не напоминаем; продолжить раньше можно на экране «Темп»
        v.state = "paused"
        v.paused_until = enr.paused_until
        v.next_segment = await segment_by_day(session, book.id, rules.first_open_day(set(v.accepted), enr.plan_days) or 1)
        return v
    if plan_day < 1 and not v.accepted:
        # (с прогрессом старт «в будущем» бывает только после паузы, начатой со следующего дня, — это не «не начался»)
        v.state = "not_started"
        v.segment = await segment_by_day(session, book.id, 1)
        return v
    if v.today > v.deadline:
        v.state = "expired"
        return v

    today_rows = list(
        await session.scalars(
            select(Retelling).where(Retelling.enrollment_id == enr.id, Retelling.user_day == v.today).order_by(Retelling.id)
        )
    )
    v.accepted_today = len({r.segment_id for r in today_rows if r.verdict == "accepted"})
    v.limit = rules.MAX_SEGMENTS_PER_DAY
    tr = await session.scalar(select(DayResult.result).where(DayResult.enrollment_id == enr.id, DayResult.user_day == v.today))
    v.today_result = tr

    acc = set(v.accepted)
    if rules.all_segments_done(acc, enr.plan_days):
        v.state = "finished"
        return v
    v.behind = rules.behind_by(acc, enr.plan_days, plan_day)
    v.ahead = rules.ahead_by(acc, plan_day)
    due = rules.next_segment_day(acc, enr.plan_days, plan_day)
    nxt = rules.first_open_day(acc, enr.plan_days)
    v.next_segment = await segment_by_day(session, book.id, nxt) if nxt else None
    room = rules.can_submit_more_today(v.accepted_today)
    if due is not None and v.accepted_today == 0:
        seg = await segment_by_day(session, book.id, due)
        v.catching_up = due < plan_day
    elif room and v.accepted_today == 0:
        seg, v.extra = v.next_segment, "ahead"  # по плану сегодня ничего, но можно читать дальше
    elif room and (enr.more_day == v.today or due is not None):
        # догоняем план — следующий открыт сразу; читать вперёд — по просьбе («Перескажу ещё один»)
        seg, v.extra = v.next_segment, "more"
        v.catching_up = due is not None
    else:
        v.state = "done_today"
        return v
    if seg is None:
        v.state = "done_today"
        return v
    v.segment = seg
    open_r = next((r for r in reversed(today_rows) if r.segment_id == seg.id), None)
    if open_r and open_r.verdict == "clarify":
        v.state = "clarify"
        v.open_retelling = open_r
    elif open_r and open_r.verdict == "pending":
        v.state = "checking"
        v.open_retelling = open_r
    else:
        v.state = "to_read"
    return v


# --------------------------------------------------------------------------- закрытие дня


async def _day_result(session: AsyncSession, enr_id: int, day: date) -> DayResult | None:
    return await session.scalar(select(DayResult).where(DayResult.enrollment_id == enr_id, DayResult.user_day == day))


async def close_pending_days(session: AsyncSession, enr: Enrollment, user: User, outbox: Outbox | None = None) -> list[str]:
    """Закрыть все прошедшие дни (идемпотентно). Вызывается планировщиком и лениво перед сдачей."""
    results: list[str] = []
    if enr.status not in ("paid", "active") or not enr.plan_days or enr.plan_start_date is None:
        return results
    today = today_for(user)
    deadline = deadline_day(enr.plan_start_date, enr.plan_days, enr.run.grace_days)
    # дни паузы закрываются тоже, даже если пауза сдвинула старт плана дальше них
    lower = min(enr.plan_start_date, enr.paused_from) if enr.paused_from else enr.plan_start_date
    start = lower if enr.last_closed_day is None else max(enr.last_closed_day + timedelta(days=1), lower)
    d = start
    when_accepted: dict[int, date] | None = None
    while d < today:
        if d > deadline:
            break
        # отложенные проверки за этот день: ждём ИИ, но не вечно
        pending = list(
            await session.scalars(
                select(Retelling).where(
                    Retelling.enrollment_id == enr.id, Retelling.user_day == d, Retelling.verdict == "pending"
                )
            )
        )
        if pending:
            if any(age(r.created_at) < PENDING_GRACE for r in pending):
                break
            from services.retell import force_accept_pending

            for r in pending:
                await force_accept_pending(session, r, enr, user, outbox)
            when_accepted = None
        plan_day = plan_day_number(enr.plan_start_date, d)
        if plan_day >= 1:
            enr.freezes_left, enr.freezes_week_start = rules.refresh_freezes(
                enr.freezes_left, enr.freezes_week_start, plan_day, enr.freezes_per_week or 1
            )
        existing = await _day_result(session, enr.id, d)
        had_done = existing is not None and existing.result == rules.DONE
        banked = False
        if not had_done:
            if when_accepted is None:
                when_accepted = {n: r.user_day for n, r in (await accepted_by_day(session, enr)).items()}
            due = range(1, min(plan_day, enr.plan_days) + 1)
            banked = all(n in when_accepted and when_accepted[n] < d for n in due)  # всё к этому дню сдано заранее
        res = rules.close_day(had_done, enr.streak, enr.freezes_left, banked, paused=paused_on(enr, d))
        if existing is None:
            session.add(DayResult(enrollment_id=enr.id, user_day=d, result=res.result))
        enr.streak = res.streak
        enr.freezes_left = res.freezes_left
        enr.last_closed_day = d
        results.append(res.result)
        if res.result == rules.FROZEN:
            await log_event(session, "day_frozen", user.id, enr.run_id, day=d.isoformat())
        elif res.result == rules.AHEAD:
            await log_event(session, "day_ahead", user.id, enr.run_id, day=d.isoformat())
        elif res.result == rules.PAUSED:
            await log_event(session, "day_paused", user.id, enr.run_id, day=d.isoformat())
        elif res.result == rules.MISSED:
            await log_event(session, "day_missed", user.id, enr.run_id, day=d.isoformat())
            if res.streak_lost:
                await log_event(session, "streak_lost", user.id, enr.run_id, day=d.isoformat())
        await session.flush()
        d += timedelta(days=1)

    if today > deadline and enr.status in ("paid", "active"):
        accepted = await accepted_by_day(session, enr)
        if not rules.all_segments_done(set(accepted), enr.plan_days):
            enr.status = "dropped"
            await log_event(session, "dropped", user.id, enr.run_id)
    if enr.pair_id:
        pair = await session.get(Pair, enr.pair_id)
        if pair:
            await close_pair_days(session, pair, outbox)
    return results


async def _pair_member_result(session: AsyncSession, enr: Enrollment | None, d: date) -> tuple[bool, str | None]:
    """(можно ли уже судить о дне, результат). Закончивший книгу считается сдавшим."""
    if enr is None:
        return True, None
    if enr.status == "finished":
        return True, rules.DONE
    if enr.plan_start_date is None or d < enr.plan_start_date or enr.status not in ("paid", "active"):
        return True, None
    if enr.last_closed_day is None or enr.last_closed_day < d:
        return False, None
    r = await _day_result(session, enr.id, d)
    return True, r.result if r else None


async def close_pair_days(session: AsyncSession, pair: Pair, outbox: Outbox | None = None) -> None:
    ea = await session.scalar(select(Enrollment).where(Enrollment.pair_id == pair.id, Enrollment.user_id == pair.user_a_id))
    eb = await session.scalar(select(Enrollment).where(Enrollment.pair_id == pair.id, Enrollment.user_id == pair.user_b_id))
    starts = [e.plan_start_date for e in (ea, eb) if e and e.plan_start_date]
    if not starts:
        return
    d = min(starts) if pair.last_closed_day is None else pair.last_closed_day + timedelta(days=1)
    limit_day = max(today_for(await session.get(User, pair.user_a_id)), today_for(await session.get(User, pair.user_b_id)))
    while d < limit_day:
        ok_a, ra = await _pair_member_result(session, ea, d)
        ok_b, rb = await _pair_member_result(session, eb, d)
        if not (ok_a and ok_b):
            break
        if ra is None and rb is None:
            pair.last_closed_day = d
            d += timedelta(days=1)
            continue
        pair.streak = rules.pair_streak_after_day(ra, rb, pair.streak)
        pair.best_streak = max(pair.best_streak, pair.streak)
        pair.last_closed_day = d
        from services.achievements import award

        for uid in (pair.user_a_id, pair.user_b_id):
            u = await session.get(User, uid)
            if u:
                await award(session, u, AchievementContext(TRIGGER_DAY_CLOSED, pair_streak=pair.streak), outbox)
        d += timedelta(days=1)


# --------------------------------------------------------------------------- финиш


async def finish_enrollment(session: AsyncSession, enr: Enrollment, user: User, outbox: Outbox | None) -> list[str]:
    enr.status = "finished"
    enr.finished_at = now()
    bad = await session.scalar(
        select(func.count(DayResult.id)).where(
            DayResult.enrollment_id == enr.id, DayResult.result.in_((rules.FROZEN, rules.MISSED))
        )
    )
    await log_event(session, "finish", user.id, enr.run_id, plan_days=enr.plan_days, streak=enr.streak)
    from services.achievements import award

    return await award(
        session, user, AchievementContext(TRIGGER_FINISH, finished=True, finished_iron=not bad), outbox
    )
