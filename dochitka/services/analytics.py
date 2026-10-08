"""Аналитика для админки и /sales: пользователи, воронка, удержание, продажи, промокоды, источники, расходы на ИИ.

Всё считается по журналу событий и таблицам покупок и дней. Дни — по московскому времени.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import clock
from db.models import DayResult, Enrollment, Event, PromoCode, Purchase, User
from services import billing

MSK = ZoneInfo("Europe/Moscow")
DAYS_CHART = 14


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC"))


async def _users_with(s: AsyncSession, *types: str, since: datetime | None = None) -> int:
    q = select(func.count(func.distinct(Event.user_id))).where(Event.type.in_(types), Event.user_id.is_not(None))
    if since is not None:
        q = q.where(Event.created_at >= since)
    return await s.scalar(q) or 0


async def ai_costs(s: AsyncSession, since: datetime | None = None) -> dict:
    q = select(Event.payload).where(Event.type == "ai_usage")
    if since is not None:
        q = q.where(Event.created_at >= since)
    rub = 0.0
    tokens = 0
    voice_sec = 0.0
    by_kind: dict[str, float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)
    for p in await s.scalars(q):
        p = p or {}
        r = float(p.get("rub") or 0)
        rub += r
        kind = p.get("kind") or "check"
        by_kind[kind] += r
        calls[kind] += 1
        tokens += int(p.get("in") or 0) + int(p.get("out") or 0)
        voice_sec += float(p.get("sec") or 0)
    return {"rub": round(rub, 2), "tokens": tokens, "voice_min": round(voice_sec / 60, 1),
            "by_kind": {k: round(v, 2) for k, v in by_kind.items()}, "calls": dict(calls)}


async def overview(s: AsyncSession) -> dict:
    now = clock.real_now()
    day_ago, week_ago, month_ago = now - timedelta(days=1), now - timedelta(days=7), now - timedelta(days=30)

    # --- пользователи и активность
    users_total = await s.scalar(select(func.count(User.id))) or 0
    new_7d = await s.scalar(select(func.count(User.id)).where(User.created_at >= week_ago)) or 0
    new_today = await s.scalar(select(func.count(User.id)).where(User.created_at >= day_ago)) or 0
    active_today = await _users_with(s, "day_done", since=day_ago)
    active_7d = await _users_with(s, "day_done", since=week_ago)
    submitted_7d = await s.scalar(
        select(func.count(Event.id)).where(Event.type == "retelling_submitted", Event.created_at >= week_ago)) or 0
    voice_7d = 0
    for p in await s.scalars(select(Event.payload).where(Event.type == "retelling_submitted", Event.created_at >= week_ago)):
        voice_7d += (p or {}).get("source") == "voice"

    # --- доступ
    subs_active = await s.scalar(select(func.count(User.id)).where(User.subscription_until > now)) or 0
    credits = await s.scalar(select(func.coalesce(func.sum(User.run_credits), 0))) or 0
    paid_users = await s.scalar(
        select(func.count(func.distinct(Purchase.user_id))).where(Purchase.status == "paid", Purchase.user_id.is_not(None))
    ) or 0

    # --- воронка (уникальные люди за всё время)
    funnel = [
        ("Нажали /start", await _users_with(s, "start")),
        ("Дали согласие", await _users_with(s, "consent_pd")),
        ("Прошли знакомство", await _users_with(s, "onboarding_done")),
        ("Добавили книгу", await _users_with(s, "book_parsed", "book_paper_added")),
        ("Выбрали план", await _users_with(s, "plan_confirmed")),
        ("Увидели тарифы", await _users_with(s, "paywall_shown")),
        ("Получили доступ (оплата, промокод, спринт)", paid_users + await _users_with(s, "sprint_started")),
        ("Сдали первый день", await _users_with(s, "day_done")),
        ("Дочитали книгу", await _users_with(s, "finish")),
    ]

    # --- удержание: сколько дней сдал каждый человек (лучшее участие)
    rows = (await s.execute(
        select(Enrollment.user_id, func.count(DayResult.id))
        .join(DayResult, DayResult.enrollment_id == Enrollment.id)
        .where(DayResult.result == "done").group_by(Enrollment.user_id, Enrollment.id)
    )).all()
    best: dict[int, int] = {}
    for uid, n in rows:
        best[uid] = max(best.get(uid, 0), n)
    retention = [(f"Сдали {k}+ {_days(k)}", sum(1 for v in best.values() if v >= k)) for k in (1, 3, 7, 14, 21)]
    retention.append(("Дочитали книгу", await _users_with(s, "finish")))

    # --- продажи
    sales = {
        "today": await billing.sales_summary(s, 1), "week": await billing.sales_summary(s, 7),
        "month": await billing.sales_summary(s, 30), "total": await billing.sales_summary(s),
    }
    total = sales["total"]
    arppu = round(total["rub"] / total["payers"]) if total["payers"] else 0

    # --- источники: пришли и заплатили
    src_rows = (await s.execute(
        select(User.source, func.count(User.id)).group_by(User.source).order_by(func.count(User.id).desc()).limit(12)
    )).all()
    pay_rows = dict((await s.execute(
        select(User.source, func.count(func.distinct(Purchase.user_id)))
        .join(Purchase, Purchase.user_id == User.id)
        .where(Purchase.status == "paid", Purchase.amount > 0).group_by(User.source)
    )).all())
    sources = [{"source": src or "без метки", "users": n, "payers": pay_rows.get(src, 0)} for src, n in src_rows]

    # --- промокоды
    promos = []
    for p in await s.scalars(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(50)):
        promos.append(await promo_row(s, p))

    # --- расходы на ИИ
    ai = {"today": await ai_costs(s, day_ago), "week": await ai_costs(s, week_ago),
          "month": await ai_costs(s, month_ago), "total": await ai_costs(s)}
    ai["per_reader_week"] = round(ai["week"]["rub"] / active_7d, 2) if active_7d else None

    return {
        "generated_at": now.isoformat(),
        "users": {"total": users_total, "new_today": new_today, "new_7d": new_7d,
                  "active_today": active_today, "active_7d": active_7d,
                  "retellings_7d": submitted_7d, "voice_share_7d": round(voice_7d / submitted_7d, 2) if submitted_7d else 0},
        "access": {"subscriptions": subs_active, "credits": int(credits), "with_access": paid_users},
        "funnel": [{"title": t, "n": n} for t, n in funnel],
        "retention": [{"title": t, "n": n} for t, n in retention],
        "sales": sales, "arppu": arppu,
        "sources": sources, "promos": promos, "ai": ai,
        "daily": await daily(s, now),
    }


def _days(n: int) -> str:
    return "день" if n % 10 == 1 and n % 100 != 11 else "дня" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "дней"


async def promo_row(s: AsyncSession, p: PromoCode) -> dict:
    came = await s.scalar(select(func.count(User.id)).where(User.source == f"promo:{p.code}")) or 0
    paid = list(await s.scalars(select(Purchase).where(Purchase.promo_code == p.code, Purchase.status == "paid")))
    from bot.ui import deep_link

    return {
        "code": p.code, "discount": p.discount_percent, "products": (p.products or "").split(","),
        "days": p.trial_days, "used": p.used, "max_uses": p.max_uses, "active": p.active, "owner": p.owner,
        "came": came, "purchases": len(paid), "revenue": sum(x.amount for x in paid) // 100,
        "link": deep_link(f"promo_{p.code}"),
    }


async def daily(s: AsyncSession, now: datetime) -> list[dict]:
    """Динамика по дням (МСК): новые люди, кто сдал день, выручка и расходы на ИИ."""
    since = now - timedelta(days=DAYS_CHART + 1)
    today = now.astimezone(MSK).date()
    days = [today - timedelta(days=i) for i in range(DAYS_CHART - 1, -1, -1)]
    out = {d: {"date": d.isoformat(), "new_users": 0, "active": set(), "revenue": 0, "ai_rub": 0.0} for d in days}

    def day_of(dt: datetime):
        return _aware(dt).astimezone(MSK).date()

    for created in await s.scalars(select(User.created_at).where(User.created_at >= since)):
        d = day_of(created)
        if d in out:
            out[d]["new_users"] += 1
    for typ, uid, created, payload in (await s.execute(
        select(Event.type, Event.user_id, Event.created_at, Event.payload)
        .where(Event.type.in_(("day_done", "ai_usage")), Event.created_at >= since)
    )).all():
        d = day_of(created)
        if d not in out:
            continue
        if typ == "day_done" and uid:
            out[d]["active"].add(uid)
        elif typ == "ai_usage":
            out[d]["ai_rub"] += float((payload or {}).get("rub") or 0)
    for created, amount in (await s.execute(
        select(Purchase.created_at, Purchase.amount).where(Purchase.status == "paid", Purchase.created_at >= since)
    )).all():
        d = day_of(created)
        if d in out:
            out[d]["revenue"] += amount // 100
    return [{**v, "active": len(v["active"]), "ai_rub": round(v["ai_rub"], 2)} for v in out.values()]
