"""Цены с промокодами, права доступа и активация забега.

Оплата — одной кнопкой через ЮKassa (services.payments). Права доступа:
- разовый забег → +1 кредит; кредит тратится, когда план готов и забег стартует;
- абонемент (месяц/год) → забеги без доплат, пока он действует, и вторая заморозка в неделю;
- бесплатный спринт — один раз (services.runs.start_sprint).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.models import Enrollment, PromoCode, Purchase, User
from services.common import Outbox, OutMsg, log_event, now, random_code
from services.runs import current_enrollment, plan_start_for
from settings import get_settings

log = logging.getLogger(__name__)

PRODUCTS = ("run", "month", "year")
SUB_DAYS = {"month": 30, "year": 365}


def product_title(product: str) -> str:
    return {"run": "Забег: одна книга до финиша", "month": "Абонемент на месяц", "year": "Абонемент на год"}[product]


def product_description(product: str) -> str:
    p = get_settings().project_name
    return {
        "run": f"{p}: план для твоей книги, ежедневная проверка пересказов, напарник и полка.",
        "month": f"{p}: книга за книгой без доплат 30 дней и вторая заморозка в неделю.",
        "year": f"{p}: книга за книгой без доплат 365 дней и вторая заморозка в неделю.",
    }[product]


def receipt_item(product: str, rub: float) -> dict:
    """Позиция чека 54-ФЗ для ЮKassa."""
    s = get_settings()
    return {
        "description": product_title(product)[:128],
        "quantity": "1.00",
        "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
        "vat_code": s.receipt_vat_code,
        "payment_mode": s.receipt_payment_mode,
        "payment_subject": "service",
        "measure": "piece",
    }


# --------------------------------------------------------------------------- цены и промокоды


@dataclass
class Price:
    product: str
    rub: int
    list_rub: int
    promo: str | None = None
    discount: int = 0

    @property
    def free(self) -> bool:
        return self.rub == 0


def list_prices() -> dict[str, int]:
    s = get_settings()
    return {"run": s.price_run_rub, "month": s.price_month_rub, "year": s.price_year_rub}


def normalize_code(code: str | None) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum() or ch in "-_")[:32]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def valid_promo(session: AsyncSession, code: str | None) -> PromoCode | None:
    code = normalize_code(code)
    if not code:
        return None
    p = await session.get(PromoCode, code)
    if p is None or not p.active:
        return None
    if p.expires_at is not None and _aware(p.expires_at) < now():
        return None
    if p.max_uses is not None and p.used >= p.max_uses:
        return None
    return p


def _discounted(value: int, percent: int) -> int:
    if percent >= 100:
        return 0
    return max(1, round(value * (100 - percent) / 100))


async def prices_with_promo(session: AsyncSession, code: str | None) -> dict[str, Price]:
    promo = await valid_promo(session, code)
    out: dict[str, Price] = {}
    for product, rub in list_prices().items():
        pr = Price(product, rub, rub)
        if promo and product in (promo.products or "").split(","):
            pr.rub = _discounted(rub, promo.discount_percent)
            pr.promo, pr.discount = promo.code, promo.discount_percent
        out[product] = pr
    return out


async def prices_for(session: AsyncSession, user: User) -> dict[str, Price]:
    return await prices_with_promo(session, user.promo_code)


async def promo_take(session: AsyncSession, code: str, *, enforce_limit: bool = True) -> bool:
    """Атомарно учесть использование промокода; с лимитом — только если он ещё не исчерпан."""
    q = update(PromoCode).where(PromoCode.code == code).values(used=PromoCode.used + 1)
    if enforce_limit:
        q = q.where((PromoCode.max_uses.is_(None)) | (PromoCode.used < PromoCode.max_uses))
    res = await session.execute(q.execution_options(synchronize_session=False))
    await session.flush()
    obj = await session.get(PromoCode, code)
    if obj is not None:
        await session.refresh(obj, ["used"])
    return bool(res.rowcount)


async def promo_release(session: AsyncSession, code: str) -> None:
    await session.execute(update(PromoCode).where(PromoCode.code == code, PromoCode.used > 0)
                          .values(used=PromoCode.used - 1).execution_options(synchronize_session=False))


# --------------------------------------------------------------------------- права доступа


def subscription_active(user: User) -> bool:
    return bool(user.subscription_until and _aware(user.subscription_until) > now())


def has_access(user: User) -> bool:
    return subscription_active(user) or user.run_credits > 0


async def activate(session: AsyncSession, user: User, enr: Enrollment, access: str, purchase: Purchase | None = None) -> None:
    """Открыть забег: статус paid, день старта, заморозки."""
    s = get_settings()
    enr.status = "paid"
    enr.paid_at = now()
    enr.access = access
    if purchase is not None:
        enr.purchase_id = purchase.id
    if access == "subscription":
        enr.freezes_per_week = s.sub_freezes_per_week
        enr.freezes_left = max(enr.freezes_left, s.sub_freezes_per_week)
    if enr.pair_code is None:
        enr.pair_code = random_code(10)
    if enr.plan_confirmed_at and enr.plan_start_date is None:
        enr.plan_start_date = plan_start_for(enr.run, user)
    await log_event(session, "paid", user.id, enr.run_id, access=access)


async def try_activate(session: AsyncSession, user: User, enr: Enrollment | None,
                       purchase: Purchase | None = None) -> str | None:
    """Если у человека есть абонемент или оплаченный забег — стартуем план. Возвращает способ доступа."""
    if enr is None or enr.status != "invited" or not enr.plan_confirmed_at:
        return None
    if subscription_active(user):
        await activate(session, user, enr, "subscription", purchase if purchase and purchase.product != "run" else None)
        return "subscription"
    if user.run_credits > 0:
        user.run_credits -= 1
        if purchase is None or purchase.product != "run":
            purchase = await session.scalar(
                select(Purchase).where(Purchase.user_id == user.id, Purchase.product == "run", Purchase.status == "paid")
                .order_by(Purchase.id.desc()).limit(1)
            )
        await activate(session, user, enr, "credit", purchase)
        return "credit"
    return None


async def grant_entitlement(session: AsyncSession, user: User, p: Purchase) -> str | None:
    """Выдать права по покупке и сразу стартовать забег, если план уже готов.

    Сроки абонементов складываются: новая покупка прибавляет свой срок к оставшемуся.
    Возвращает способ доступа, если забег стартовал прямо сейчас.
    """
    # строка пользователя — под блокировкой: две оплаты одновременно не перезапишут друг другу срок и кредиты
    await session.flush()
    await session.refresh(user, with_for_update=True)
    if p.product == "run":
        user.run_credits += 1
    else:
        base = max(now(), _aware(user.subscription_until)) if user.subscription_until else now()
        until = base + timedelta(days=SUB_DAYS[p.product])
        user.subscription_until = until
        user.subscription_kind = p.product
        user.sub_reminded_at = None
        p.valid_until = until
    await session.flush()
    access = await try_activate(session, user, await current_enrollment(session, user.id), p)
    p.activated_now = access  # type: ignore[attr-defined]  # для текста после оплаты, в базу не пишется
    return access


async def activate_pending(session: AsyncSession, user: User) -> str | None:
    """Оплаченный кредит или абонемент → стартовать текущий забег, который ждёт оплаты (например, после спринта)."""
    if not has_access(user):
        return None
    return await try_activate(session, user, await current_enrollment(session, user.id))


async def grant_manual(session: AsyncSession, user: User, product: str, outbox: Outbox | None = None) -> Purchase:
    """Администратор выдал доступ вручную (оплата переводом, подарок, бартер)."""
    p = Purchase(user_id=user.id, product=product, provider="manual", currency="RUB", amount=0, list_amount=0,
                 status="paid")
    session.add(p)
    await session.flush()
    await log_event(session, "purchase", user.id, product=product, provider="manual", amount=0)
    await grant_entitlement(session, user, p)
    return p


async def apply_free_promo(session: AsyncSession, user: User, product: str = "run") -> Purchase | None:
    """Промокод на 100% — доступ без оплаты."""
    price = (await prices_with_promo(session, user.promo_code))[product]
    if not price.free or not price.promo:
        return None
    if not await promo_take(session, price.promo):
        return None  # лимит исчерпан, пока человек думал
    p = Purchase(user_id=user.id, product=product, provider="promo", currency="RUB", amount=0,
                 list_amount=list_prices()[product] * 100, promo_code=price.promo, status="paid")
    session.add(p)
    user.promo_code = None
    await session.flush()
    await log_event(session, "purchase", user.id, product=product, provider="promo", amount=0, promo=price.promo)
    await grant_entitlement(session, user, p)
    return p


def format_amount(p: Purchase) -> str:
    return "бесплатно" if p.amount == 0 else texts.rub(p.amount / 100)


async def notify_admins_payment(session: AsyncSession, user: User | None, p: Purchase, outbox: Outbox | None) -> None:
    if outbox is None:
        return
    via = {"yookassa": "ЮKassa", "manual": "вручную", "promo": "промокод"}.get(p.provider, p.provider)
    who = (user.display_name + (f" (@{user.tg_username})" if user.tg_username else "")) if user else f"заказ #{p.id}"
    extra = f", промокод {p.promo_code}" if p.promo_code else ""
    src = (user.source if user and user.source else p.source) or ""
    src = f", источник: {src}" if src else ""
    for admin in get_settings().admin_ids:
        outbox.add(OutMsg(admin, f"Оплата: {texts.e(who)} — {product_title(p.product).lower()}, "
                                 f"{format_amount(p)} ({via}{extra}{src}) · заказ #{p.id}", kind="admin"))


# --------------------------------------------------------------------------- отмена оплаты (возврат в кабинете ЮKassa)


async def revoke(session: AsyncSession, user: User, p: Purchase) -> None:
    """Забрать права, выданные покупкой (если деньги вернули)."""
    if p.product == "run":
        enr = await session.scalar(select(Enrollment).where(Enrollment.purchase_id == p.id).limit(1))
        if enr is not None and enr.status in ("paid", "active"):
            enr.status = "refunded"
        elif enr is None and user.run_credits > 0:
            user.run_credits -= 1
        return
    # снимаем только срок этой покупки: ранее оплаченные периоды остаются
    until = _aware(user.subscription_until) if user.subscription_until else now()
    user.subscription_until = max(now(), until - timedelta(days=SUB_DAYS[p.product]))
    if not subscription_active(user):
        # закрываем только забеги, открытые этой покупкой (или начатые уже после неё);
        # забег, начатый по прошлому оплаченному периоду, можно дочитать
        for e in await session.scalars(
            select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.access == "subscription",
                                     Enrollment.status.in_(("paid", "active")))
        ):
            started_by_it = e.purchase_id == p.id or (
                e.purchase_id is None and e.paid_at is not None and _aware(e.paid_at) >= _aware(p.created_at))
            if started_by_it:
                e.status = "refunded"


async def mark_refunded(session: AsyncSession, purchase_id: int) -> Purchase | None:
    p = await session.get(Purchase, purchase_id)
    if p is None or p.status == "refunded":
        return p
    user = await session.get(User, p.user_id, with_for_update=True, populate_existing=True) if p.user_id else None
    was_paid = p.status == "paid"
    p.status = "refunded"
    p.refunded_at = now()
    if user is not None and was_paid:
        await revoke(session, user, p)
    await log_event(session, "refund", p.user_id, product=p.product, provider=p.provider, amount=p.amount)
    return p


# --------------------------------------------------------------------------- отчёты


async def sales_summary(session: AsyncSession, days: int | None = None) -> dict:
    q = select(Purchase).where(Purchase.status == "paid")
    if days:
        q = q.where(Purchase.created_at >= now() - timedelta(days=days))
    rows = list(await session.scalars(q))
    rub = sum(p.amount for p in rows) / 100
    by_product: dict[str, int] = {}
    for p in rows:
        by_product[p.product] = by_product.get(p.product, 0) + 1
    refunds = await session.scalar(select(func.count(Purchase.id)).where(Purchase.status == "refunded"))
    return {"count": len(rows), "rub": rub, "by_product": by_product, "refunds": refunds or 0,
            "payers": len({p.user_id for p in rows if p.amount > 0})}
