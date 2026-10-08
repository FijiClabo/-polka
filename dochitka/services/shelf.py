"""Полка дочитанных книг и значки."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Book, DayResult, Enrollment, Pair, Retelling, Segment, User, UserAchievement
from services.achievements import all_with_status
from services.progress import accepted_by_day


async def _partner_name(session: AsyncSession, enr: Enrollment) -> str | None:
    if not enr.pair_id:
        return None
    pair = await session.get(Pair, enr.pair_id)
    if not pair:
        return None
    pid = pair.user_b_id if pair.user_a_id == enr.user_id else pair.user_a_id
    p = await session.get(User, pid)
    return p.display_name if p else None


async def shelf(session: AsyncSession, user: User, current: Enrollment | None) -> dict:
    """Дочитанные книги и текущая с долей прогресса."""
    finished = list(
        await session.scalars(
            select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.status == "finished").order_by(Enrollment.finished_at)
        )
    )
    items = []
    total_retells = 0
    for enr in finished:
        book = await session.get(Book, enr.book_id) if enr.book_id else None
        if not book:
            continue
        n = await session.scalar(
            select(func.count(Retelling.id)).where(Retelling.enrollment_id == enr.id, Retelling.verdict == "accepted")
        )
        total_retells += n or 0
        bad = await session.scalar(
            select(func.count(DayResult.id)).where(DayResult.enrollment_id == enr.id, DayResult.result != "done")
        )
        items.append({
            "book_id": book.id, "title": book.title, "author": book.author, "spine_color": book.spine_color,
            "pages": book.total_pages, "status": "finished",
            "finished_at": enr.finished_at.isoformat() if enr.finished_at else None,
            "started_at": enr.plan_start_date.isoformat() if enr.plan_start_date else None,
            "plan_days": enr.plan_days, "no_skips": not bad, "partner": await _partner_name(session, enr),
            "retellings": n or 0, "progress": 1.0,
        })
    cur = None
    if current and current.status != "finished" and current.book_id:
        book = await session.get(Book, current.book_id)
        if book and book.parse_status == "ok":
            acc = await accepted_by_day(session, current)
            progress = 0.0
            if acc:
                seg = await session.scalar(select(Segment).where(Segment.book_id == book.id, Segment.day_number == max(acc)))
                progress = seg.pos_to if seg else 0.0
            total_retells += len(acc)
            cur = {
                "book_id": book.id, "title": book.title, "author": book.author, "spine_color": book.spine_color,
                "pages": book.total_pages, "status": "reading", "progress": round(progress, 3),
                "plan_days": current.plan_days, "partner": await _partner_name(session, current),
                "retellings": len(acc),
            }
    return {"finished": items, "current": cur, "finished_count": len(items), "retellings_count": total_retells}


async def achievements_of(session: AsyncSession, user_id: int) -> list[dict]:
    rows = await session.execute(
        select(UserAchievement.achievement_code, UserAchievement.awarded_at).where(UserAchievement.user_id == user_id)
    )
    return all_with_status({c: a for c, a in rows.all()})
