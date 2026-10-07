"""Админ-статистика и выгрузка для анализа теста (раздел 11 общего документа)."""

from __future__ import annotations

import csv
import io
import zipfile

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DayResult, Enrollment, Event, Retelling, Run, User
from services.common import today_for


async def find_user(session: AsyncSession, ref: str) -> User | None:
    ref = ref.strip()
    if ref.startswith("@"):
        return await session.scalar(select(User).where(func.lower(User.tg_username) == ref[1:].lower()))
    if ref.lstrip("-").isdigit():
        n = int(ref)
        u = await session.scalar(select(User).where(User.tg_id == n))
        return u or await session.get(User, n)
    return await session.scalar(select(User).where(func.lower(User.tg_username) == ref.lower()))


async def stats(session: AsyncSession, run: Run) -> dict:
    enrs = list(await session.scalars(select(Enrollment).where(Enrollment.run_id == run.id)))
    by_status: dict[str, int] = {}
    for e in enrs:
        by_status[e.status] = by_status.get(e.status, 0) + 1
    active = [e for e in enrs if e.status in ("paid", "active") and e.plan_start_date]
    done_today = 0
    for e in active:
        u = await session.get(User, e.user_id)
        if await session.scalar(
            select(DayResult.id).where(DayResult.enrollment_id == e.id, DayResult.user_day == today_for(u),
                                       DayResult.result == "done")
        ):
            done_today += 1
    avg_streak = round(sum(e.streak for e in active) / len(active), 1) if active else 0
    ids = [e.id for e in enrs] or [-1]
    verdicts = dict(
        (await session.execute(
            select(Retelling.verdict, func.count(Retelling.id)).where(Retelling.enrollment_id.in_(ids)).group_by(Retelling.verdict)
        )).all()
    )
    total_v = sum(verdicts.values()) or 1
    ai_errors = await session.scalar(select(func.count(Event.id)).where(Event.run_id == run.id, Event.type == "ai_error"))
    overrides = await session.scalar(select(func.count(Event.id)).where(Event.run_id == run.id, Event.type == "override"))
    no_plan = sum(1 for e in enrs if e.status in ("paid", "active") and not e.plan_days)
    paired = sum(1 for e in enrs if e.pair_id)
    return {
        "run": run.title, "statuses": by_status, "active": len(active), "done_today": done_today,
        "avg_streak": avg_streak, "verdicts": verdicts,
        "rejected_share": round(verdicts.get("rejected", 0) / total_v, 3),
        "ai_errors": ai_errors or 0, "overrides": overrides or 0, "no_plan": no_plan, "paired": paired,
        "finished": by_status.get("finished", 0), "dropped": by_status.get("dropped", 0),
    }


async def export_zip(session: AsyncSession, run: Run) -> bytes:
    """CSV для анализа: участники, пересказы, дни, события и сводка четырёх метрик теста."""
    enrs = list(await session.scalars(select(Enrollment).where(Enrollment.run_id == run.id)))
    users = {u.id: u for u in await session.scalars(select(User).where(User.id.in_([e.user_id for e in enrs] or [-1])))}
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)

    def write(name: str, header: list[str], rows: list[list]) -> None:
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(header)
        w.writerows(rows)
        zf.writestr(name, "﻿" + s.getvalue())  # BOM — чтобы Excel открыл кириллицу

    # следующий оплаченный забег после этого (для метрики «оплатили второй забег»)
    later_paid = set(
        await session.scalars(
            select(Enrollment.user_id).join(Run, Run.id == Enrollment.run_id).where(
                Run.kind == "main", Run.id > run.id, Enrollment.paid_at.is_not(None)
            )
        )
    )
    part_rows = []
    for e in enrs:
        u = users.get(e.user_id)
        acc = await session.scalar(select(func.count(Retelling.id)).where(Retelling.enrollment_id == e.id, Retelling.verdict == "accepted"))
        days = dict((await session.execute(
            select(DayResult.result, func.count(DayResult.id)).where(DayResult.enrollment_id == e.id).group_by(DayResult.result)
        )).all())
        part_rows.append([
            e.user_id, u.tg_username if u else "", e.status, "pair" if e.pair_id else "solo", e.pair_id or "",
            e.plan_days or "", e.plan_start_date or "", e.paid_at.date() if e.paid_at else "",
            e.finished_at.date() if e.finished_at else "", acc or 0, days.get("done", 0), days.get("frozen", 0),
            days.get("missed", 0), e.streak, e.best_streak, "yes" if e.user_id in later_paid else "no",
            u.invited_by_id if u and u.invited_by_id else "",
        ])
    write("participants.csv",
          ["user_id", "username", "status", "group", "pair_id", "plan_days", "plan_start", "paid_at", "finished_at",
           "accepted_retellings", "days_done", "days_frozen", "days_missed", "streak", "best_streak",
           "paid_next_run", "invited_by"], part_rows)

    ids = [e.id for e in enrs] or [-1]
    rets = list(await session.scalars(select(Retelling).where(Retelling.enrollment_id.in_(ids)).order_by(Retelling.id)))
    enr_by_id = {e.id: e for e in enrs}
    write("retellings.csv",
          ["retelling_id", "user_id", "user_day", "source", "via", "length", "verdict", "verified", "confidence",
           "attempt_no", "clarify_count", "provider", "overridden", "created_at"],
          [[r.id, enr_by_id[r.enrollment_id].user_id, r.user_day, r.source, r.via, len(r.raw_text or ""), r.verdict,
            r.verified, r.confidence, r.attempt_no, r.clarify_count, r.provider, r.overridden, r.created_at]
           for r in rets])
    drs = list(await session.scalars(select(DayResult).where(DayResult.enrollment_id.in_(ids)).order_by(DayResult.user_day)))
    write("days.csv", ["user_id", "user_day", "result"],
          [[enr_by_id[d.enrollment_id].user_id, d.user_day, d.result] for d in drs])
    evs = list(await session.scalars(select(Event).where(Event.run_id == run.id).order_by(Event.id)))
    write("events.csv", ["id", "user_id", "type", "payload", "created_at"],
          [[ev.id, ev.user_id or "", ev.type, ev.payload or "", ev.created_at] for ev in evs])

    # сводка метрик: отдельно пары и одиночки
    paid = [e for e in enrs if e.paid_at]
    lines = [f"Забег: {run.title}", f"Оплатили: {len(paid)}", ""]
    for label, group in (("Все", paid), ("Пары", [e for e in paid if e.pair_id]), ("Одиночки", [e for e in paid if not e.pair_id])):
        n = len(group) or 1
        fin = sum(1 for e in group if e.status == "finished")
        nxt = sum(1 for e in group if e.user_id in later_paid)
        lines.append(f"{label}: {len(group)} чел. Дочитали: {fin} ({fin / n:.0%}). Оплатили следующий забег: {nxt} ({nxt / n:.0%})")
    verified = [r for r in rets if r.verdict in ("accepted", "rejected", "clarify")]
    rejected_honest = sum(1 for r in rets if r.overridden)
    lines += ["", f"Пересказов с вердиктом: {len(verified)}",
              f"Отклонено (rejected): {sum(1 for r in rets if r.verdict == 'rejected')}",
              f"Ошибочные отказы (исправлены ведущим): {rejected_honest} ({rejected_honest / (len(verified) or 1):.1%})",
              "", "Цели: дочитали ≥ 40%, пары дочитывают заметно чаще, второй забег ≥ 30%, ошибочные отказы < 5%."]
    zf.writestr("metrics.txt", "\n".join(lines))
    zf.close()
    return buf.getvalue()


def stats_text(st: dict) -> str:
    v = st["verdicts"]
    return (
        f"<b>{st['run']}</b>\n"
        f"Статусы: {', '.join(f'{k}: {n}' for k, n in st['statuses'].items()) or '—'}\n"
        f"Идут по плану: {st['active']}, сдали сегодня: {st['done_today']}\n"
        f"Средний стрик: {st['avg_streak']}\n"
        f"В парах: {st['paired']}, без плана: {st['no_plan']}\n"
        f"Дочитали: {st['finished']}, выбыли: {st['dropped']}\n"
        f"Вердикты: принято {v.get('accepted', 0)}, уточнение {v.get('clarify', 0)}, "
        f"отказ {v.get('rejected', 0)}, в очереди {v.get('pending', 0)}\n"
        f"Доля отказов ИИ: {st['rejected_share']:.1%} · ошибок ИИ: {st['ai_errors']} · ручных засчитываний: {st['overrides']}"
    )


