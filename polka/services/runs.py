"""Забеги и участие: текущий забег, запись, оплата, спринт, старт плана."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.days import local_now
from db.models import Enrollment, Run, User
from services.common import log_event, now, random_code, today_for

ACTIVE_STATUSES = ("paid", "active")
OPEN_STATUSES = ("invited", "paid", "active")


async def current_main_run(session: AsyncSession) -> Run | None:
    return await session.scalar(
        select(Run).where(Run.kind == "main", Run.status.in_(("open", "active"))).order_by(Run.id.desc()).limit(1)
    )


async def sprint_run(session: AsyncSession) -> Run:
    run = await session.scalar(select(Run).where(Run.kind == "sprint").order_by(Run.id).limit(1))
    if run is None:
        run = Run(title="Спринт", kind="sprint", status="active", grace_days=2, price_rub=0)
        session.add(run)
        await session.flush()
    return run


async def user_enrollments(session: AsyncSession, user_id: int) -> list[Enrollment]:
    return list(
        await session.scalars(
            select(Enrollment).where(Enrollment.user_id == user_id).order_by(Enrollment.created_at.desc(), Enrollment.id.desc())
        )
    )


async def current_enrollment(session: AsyncSession, user_id: int, *, lock: bool = False) -> Enrollment | None:
    """Текущее участие: сначала идущий план, потом оплаченное/ожидающее, иначе последнее завершённое."""
    q = select(Enrollment).where(Enrollment.user_id == user_id)
    if lock:
        q = q.with_for_update(of=Enrollment)
    rows = list(await session.scalars(q.order_by(Enrollment.id.desc())))
    if not rows:
        return None
    running = [e for e in rows if e.status in ACTIVE_STATUSES and e.plan_start_date is not None]
    if running:
        return running[0]
    pending = [e for e in rows if e.status in OPEN_STATUSES]
    if pending:
        return pending[0]
    return rows[0]


async def enroll(session: AsyncSession, user: User, run: Run, status: str = "invited") -> Enrollment:
    enr = await session.scalar(select(Enrollment).where(Enrollment.run_id == run.id, Enrollment.user_id == user.id))
    if enr:
        return enr
    enr = Enrollment(
        run_id=run.id, user_id=user.id, status=status, streak=0, best_streak=0, freezes_left=1,
        freezes_week_start=0,
    )
    session.add(enr)
    await session.flush()
    await session.refresh(enr, ["run"])
    return enr


async def ensure_main_enrollment(session: AsyncSession, user: User) -> Enrollment | None:
    """Если ведущий открыл групповой забег — записываем в него (статус «ожидаем оплату»)."""
    run = await current_main_run(session)
    if run is None:
        return None
    return await enroll(session, user, run, "invited")


async def new_personal_run(session: AsyncSession) -> Run:
    """Личный забег: стартует в любой день, у каждого свой. Отдельная запись — можно проходить книгу за книгой."""
    run = Run(title="Личный забег", kind="solo", status="active", grace_days=3, price_rub=0)
    session.add(run)
    await session.flush()
    return run


async def ensure_enrollment(session: AsyncSession, user: User) -> Enrollment:
    """Текущее открытое участие, а если его нет — групповой забег ведущего или новый личный забег."""
    enr = await current_enrollment(session, user.id)
    if enr is not None and enr.status in OPEN_STATUSES:
        return enr
    cohort = await current_main_run(session)
    if cohort is not None:
        existing = await session.scalar(select(Enrollment).where(Enrollment.run_id == cohort.id, Enrollment.user_id == user.id))
        if existing is None:
            return await enroll(session, user, cohort, "invited")
    run = await new_personal_run(session)
    enr = await enroll(session, user, run, "invited")
    await log_event(session, "run_created", user.id, run.id)
    return enr


async def sprint_used(session: AsyncSession, user_id: int) -> bool:
    return bool(
        await session.scalar(
            select(Enrollment.id).join(Run, Run.id == Enrollment.run_id).where(
                Enrollment.user_id == user_id, Run.kind == "sprint"
            )
        )
    )


async def start_sprint(session: AsyncSession, user: User) -> Enrollment | None:
    """Бесплатный 7-дневный спринт — один раз на человека. None — уже был."""
    run = await sprint_run(session)
    existing = await session.scalar(select(Enrollment).where(Enrollment.run_id == run.id, Enrollment.user_id == user.id))
    if existing is not None:
        return existing if existing.status in OPEN_STATUSES else None
    enr = await enroll(session, user, run, "active")
    enr.access = "free"
    await log_event(session, "sprint_started", user.id, run.id)
    return enr


async def is_in_any_run(session: AsyncSession, user_id: int) -> bool:
    return bool(
        await session.scalar(
            select(Enrollment.id).where(Enrollment.user_id == user_id, Enrollment.status.in_(OPEN_STATUSES + ("finished",)))
        )
    )


async def grant(session: AsyncSession, user: User, run: Run) -> Enrollment:
    enr = await enroll(session, user, run, "paid")
    if enr.status in ("invited", "refunded", "dropped"):
        enr.status = "paid"
    enr.paid_at = now()
    if enr.pair_code is None:
        enr.pair_code = random_code(10)
    if enr.plan_confirmed_at and enr.plan_start_date is None:
        enr.plan_start_date = plan_start_for(run, user)
    await log_event(session, "paid", user.id, run.id)
    return enr


async def refund(session: AsyncSession, enr: Enrollment) -> None:
    enr.status = "refunded"
    await log_event(session, "refund", enr.user_id, enr.run_id)


def plan_start_for(run: Run, user: User, confirmed_at: datetime | None = None) -> date | None:
    """День старта плана: в групповом забеге — день старта, при позднем подтверждении — следующий день.

    Личный забег и спринт стартуют сразу, если доступ открыт до 18:00, иначе завтра.
    """
    at = confirmed_at or now()
    today = today_for(user, at)
    if run.kind in ("sprint", "solo"):
        hour = local_now(at, user.timezone).hour
        return today if 4 <= hour < 18 else today + timedelta(days=1)
    if run.start_date is None:
        return None
    if today < run.start_date:
        return run.start_date
    return today + timedelta(days=1)


def can_start_plan(enr: Enrollment) -> bool:
    """План стартует только у оплативших (или в бесплатном спринте)."""
    return enr.status in ACTIVE_STATUSES
