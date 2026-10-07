"""Продажи: цены с промокодами, оплата, права доступа, активация забега, коды, возвраты.

Где платят:
- в боте и мини-приложении — только звёздами Telegram (правило Telegram для цифровых услуг);
- на сайте — рублями через ЮKassa (services.site_orders): после оплаты — одноразовый код, его активируют в боте.

Права доступа:
- разовый забег → +1 кредит; кредит тратится, когда план готов и забег стартует;
- абонемент (месяц/год) → забеги без доплат, пока он действует, и вторая заморозка в неделю;
- бесплатный спринт — один раз (services.runs.start_sprint).

Возвраты: гарантия «без вопросов» в первые REFUND_DAYS дней (один раз на человека) — автоматически.
Позже — по закону о защите прав потребителей: за неиспользованную часть, через заявку администратору.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from core.days import plan_day_number
from db.models import Enrollment, PromoCode, Purchase, User
from services.common import Outbox, OutMsg, log_event, now, random_code, today_for
from services.runs import current_enrollment, plan_start_for
from settings import get_settings

log = logging.getLogger(__name__)

PRODUCTS = ("run", "month", "year")
SUB_DAYS = {"month": 30, "year": 365}
STARS_SUBSCRIPTION_PERIOD = 30 * 24 * 3600  # Telegram поддерживает подписки на Stars только с периодом 30 дней
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих O/0, I/1
YOOKASSA_API = "https://api.yookassa.ru/v3"


def product_title(product: str) -> str:
    return {"run": "Забег: одна книга до финиша", "month": "Абонемент на месяц", "year": "Абонемент на год"}[product]


def product_description(product: str) -> str:
    s = get_settings()
    p = s.project_name
    return {
        "run": f"{p}: план на 21–60 дней для твоей книги, ежедневная проверка пересказов ИИ, напарник, полка и конспект. "
               f"Возврат в первые {s.refund_days} дня без вопросов.",
        "month": f"{p}: забеги без доплат 30 дней — книга за книгой, плюс вторая заморозка в неделю.",
        "year": f"{p}: забеги без доплат 365 дней — книга за книгой, плюс вторая заморозка в неделю.",
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
    rub: int  # рубли
    stars: int
    list_rub: int
    list_stars: int
    promo: str | None = None
    discount: int = 0

    @property
    def free(self) -> bool:
        return self.rub == 0 and self.stars == 0


def list_prices() -> dict[str, tuple[int, int]]:
    s = get_settings()
    return {
        "run": (s.price_run_rub, s.price_run_stars),
        "month": (s.price_month_rub, s.price_month_stars),
        "year": (s.price_year_rub, s.price_year_stars),
    }


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
    for product, (rub, stars) in list_prices().items():
        pr = Price(product, rub, stars, rub, stars)
        if promo and product in (promo.products or "").split(","):
            pr.rub, pr.stars = _discounted(rub, promo.discount_percent), _discounted(stars, promo.discount_percent)
            pr.promo, pr.discount = promo.code, promo.discount_percent
        out[product] = pr
    return out


async def prices_for(session: AsyncSession, user: User) -> dict[str, Price]:
    prices = await prices_with_promo(session, user.promo_code)
    # месячная подписка звёздами продлевается по той же цене — промокод на неё не действует
    m = prices["month"]
    if m.promo and not m.free:
        m.stars = m.list_stars
    return prices


def stars_promo(price: Price) -> str | None:
    """Промокод, который реально применён к цене в звёздах (у месячной подписки — никогда)."""
    if price.promo and price.stars < price.list_stars:
        return price.promo
    return None


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
                select(Purchase).where(Purchase.user_id == user.id, Purchase.product == "run",
                                       Purchase.status.in_(("paid", "refund_requested")))
                .order_by(Purchase.id.desc()).limit(1)
            )
        await activate(session, user, enr, "credit", purchase)
        return "credit"
    return None


async def grant_entitlement(session: AsyncSession, user: User, p: Purchase, *,
                            subscription_expiration: datetime | None = None) -> str | None:
    """Выдать права по покупке и сразу стартовать забег, если план уже готов.

    Сроки абонементов складываются: новая покупка прибавляет свой срок к оставшемуся.
    Возвращает способ доступа, если забег стартовал прямо сейчас.
    """
    if p.product == "run":
        user.run_credits += 1
    else:
        base = max(now(), _aware(user.subscription_until)) if user.subscription_until else now()
        until = base + timedelta(days=SUB_DAYS[p.product])
        if subscription_expiration is not None and _aware(subscription_expiration) > until:
            until = _aware(subscription_expiration)
        user.subscription_until = until
        user.subscription_kind = p.product
        # живая звёздная подписка не отменяется оттого, что докупили год или активировали код
        user.subscription_recurring = bool(user.subscription_recurring or p.is_recurring)
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


# --------------------------------------------------------------------------- счёт в звёздах


def make_payload(product: str, user_id: int, promo: str | None) -> str:
    return f"v1:{product}:{user_id}:{promo or '-'}:{random_code(6)}"


def parse_payload(payload: str) -> tuple[str, int, str | None] | None:
    try:
        v, product, uid, promo, _ = payload.split(":", 4)
    except ValueError:
        return None
    if v != "v1" or product not in PRODUCTS or not uid.isdigit():
        return None
    return product, int(uid), (None if promo == "-" else promo)


def receipt_provider_data(product: str, rub: int) -> str | None:
    """Чек 54-ФЗ для счёта в рублях внутри бота (только при аварийном флаге PAYMENTS_RUB_IN_BOT)."""
    if not get_settings().fiscal_receipts:
        return None
    return json.dumps({"receipt": {"items": [receipt_item(product, rub)]}}, ensure_ascii=False)


@dataclass
class Invoice:
    title: str
    description: str
    payload: str
    currency: str
    amount: int  # Stars или копейки
    provider_token: str
    subscription_period: int | None = None
    need_email: bool = False
    send_email_to_provider: bool = False
    provider_data: str | None = None


async def build_invoice(session: AsyncSession, user: User, product: str, method: str) -> Invoice:
    s = get_settings()
    if product not in PRODUCTS:
        raise ValueError("unknown product")
    price = (await prices_for(session, user))[product]
    if price.free:
        raise ValueError("free")
    if method == "stars":
        if not s.payments_stars:
            raise ValueError("stars disabled")
        return Invoice(
            title=product_title(product), description=product_description(product),
            payload=make_payload(product, user.id, stars_promo(price)), currency="XTR", amount=price.stars,
            provider_token="", subscription_period=STARS_SUBSCRIPTION_PERIOD if product == "month" else None,
        )
    if method == "card":
        if not s.payments_yookassa:
            raise ValueError("card disabled")
        return Invoice(
            title=product_title(product), description=product_description(product),
            payload=make_payload(product, user.id, price.promo), currency="RUB",
            amount=price.rub * 100, provider_token=s.yookassa_provider_token,
            need_email=s.fiscal_receipts, send_email_to_provider=s.fiscal_receipts,
            provider_data=receipt_provider_data(product, price.rub),
        )
    raise ValueError("unknown method")


async def check_pre_checkout(session: AsyncSession, tg_user_id: int, payload: str, currency: str, total: int) -> str | None:
    """Проверка перед списанием (Telegram ждёт ответ до 10 секунд). None — можно платить."""
    parsed = parse_payload(payload)
    if parsed is None:
        return "Счёт устарел. Открой оплату заново."
    product, uid, promo = parsed
    user = await session.get(User, uid)
    if user is None or user.tg_id != tg_user_id:
        return "Этот счёт выписан для другого аккаунта."
    price = (await prices_for(session, user))[product]
    if currency == "XTR":
        expected, expected_promo = price.stars, stars_promo(price)
    else:
        expected, expected_promo = price.rub * 100, price.promo
    if (promo or None) != (expected_promo or None) or total != expected:
        return "Цена изменилась (например, закончился промокод). Открой оплату заново."
    if product == "month" and currency == "XTR" and user.subscription_recurring and subscription_active(user):
        return "У тебя уже есть абонемент с автопродлением."
    return None


def expected_amount(price: Price, currency: str) -> int:
    return price.stars if currency == "XTR" else price.rub * 100


async def payment_amount_ok(session: AsyncSession, user: User, product: str, currency: str, amount: int,
                            promo: str | None, renewal: bool) -> bool:
    """Сумма из successful_payment не меньше цены тарифа (с учётом промокода из счёта).

    Защита от подделанных апдейтов: даже если кто-то узнал адрес вебхука, «оплата» за 1 звезду ничего не даст.
    Продление подписки списывается по цене на момент оформления — его проверяем только по валюте.
    """
    if currency not in ("XTR", "RUB") or amount <= 0:
        return False
    if renewal:
        return currency == "XTR" and bool(user.star_sub_charge_id)
    rub, stars = list_prices()[product]
    base = stars if currency == "XTR" else rub * 100
    if promo and not (currency == "XTR" and product == "month"):
        code = await session.get(PromoCode, normalize_code(promo))
        if code is not None:
            base = _discounted(base, code.discount_percent)
    return amount >= base


async def record_payment(
    session: AsyncSession, user: User, *, product: str, provider: str, currency: str, amount: int,
    telegram_charge_id: str | None, provider_charge_id: str | None = None, promo: str | None = None,
    is_recurring: bool = False, renewal: bool = False, subscription_expiration: datetime | None = None,
    email: str | None = None, outbox: Outbox | None = None,
) -> Purchase:
    """Записать оплату из Telegram и выдать права. Повторная доставка того же платежа ничего не дублирует."""
    if telegram_charge_id:
        existing = await session.scalar(select(Purchase).where(Purchase.telegram_charge_id == telegram_charge_id))
        if existing is not None:
            return existing
    rub, stars = list_prices()[product]
    recurring = is_recurring or (provider == "stars" and product == "month")
    p = Purchase(
        user_id=user.id, product=product, provider=provider, currency=currency, amount=amount,
        list_amount=stars if currency == "XTR" else rub * 100, promo_code=promo, status="paid",
        telegram_charge_id=telegram_charge_id, provider_charge_id=provider_charge_id, is_recurring=recurring,
        email=email, offer_version=get_settings().offer_version,
    )
    session.add(p)
    if recurring and telegram_charge_id and (not renewal or not user.star_sub_charge_id):
        user.star_sub_charge_id = telegram_charge_id  # отмена автопродления — по первому платежу подписки
    if promo and not renewal:  # автопродление идёт по тому же счёту — промокод не считаем повторно
        await promo_take(session, promo, enforce_limit=False)  # деньги уже списаны — учитываем в любом случае
    if user.promo_code and normalize_code(user.promo_code) == (promo or ""):
        user.promo_code = None
    await session.flush()
    await log_event(session, "purchase", user.id, product=product, provider=provider, currency=currency, amount=amount,
                    promo=promo, recurring=recurring, renewal=renewal)
    await grant_entitlement(session, user, p, subscription_expiration=subscription_expiration)
    await notify_admins_payment(session, user, p, outbox)
    return p


async def grant_manual(session: AsyncSession, user: User, product: str, outbox: Outbox | None = None) -> Purchase:
    """Администратор выдал доступ вручную (оплата переводом, подарок, бартер)."""
    p = Purchase(user_id=user.id, product=product, provider="manual", currency="RUB", amount=0, list_amount=0,
                 status="paid", telegram_charge_id=None)
    session.add(p)
    await session.flush()
    await log_event(session, "purchase", user.id, product=product, provider="manual", currency="RUB", amount=0)
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
                 list_amount=list_prices()[product][0] * 100, promo_code=price.promo, status="paid")
    session.add(p)
    user.promo_code = None
    await session.flush()
    await log_event(session, "purchase", user.id, product=product, provider="promo", currency="RUB", amount=0,
                    promo=price.promo)
    await grant_entitlement(session, user, p)
    return p


async def notify_admins_payment(session: AsyncSession, user: User | None, p: Purchase, outbox: Outbox | None) -> None:
    if outbox is None:
        return
    amount = format_amount(p)
    via = {"stars": "Stars", "yookassa": "ЮKassa", "manual": "вручную", "promo": "промокод"}.get(p.provider, p.provider)
    if user is not None:
        who = user.display_name + (f" (@{user.tg_username})" if user.tg_username else "")
    else:
        who = f"заказ с сайта #{p.id}"
    extra = f", промокод {p.promo_code}" if p.promo_code else ""
    src = user.source if user is not None and user.source else p.source
    src = f", источник: {src}" if src else ""
    for admin in get_settings().admin_ids:
        outbox.add(OutMsg(admin, f"💳 Оплата: {texts.e(who)} — {product_title(p.product).lower()}, {amount} ({via}{extra}{src})",
                          kind="admin"))


def format_amount(p: Purchase) -> str:
    if p.currency == "XTR":
        return f"{p.amount} ⭐"
    return "бесплатно" if p.amount == 0 else texts.rub(p.amount / 100)


# --------------------------------------------------------------------------- коды активации (оплата на сайте)


def new_activation_code() -> str:
    import secrets

    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))


def normalize_activation_code(code: str | None) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())[:24]


async def redeem_code(session: AsyncSession, user: User, code: str, outbox: Outbox | None = None) -> tuple[str, Purchase | None]:
    """Активировать код из заказа на сайте. Статусы: ok | already | used | expired | not_found."""
    code = normalize_activation_code(code)
    if len(code) < 6:
        return "not_found", None
    p = await session.scalar(select(Purchase).where(Purchase.activation_code == code).with_for_update())
    if p is None or p.status not in ("paid", "refund_requested"):
        return "not_found", None
    if p.user_id is not None:
        return ("already" if p.user_id == user.id else "used"), p
    if p.activated_at is not None:  # активирован аккаунтом, который потом удалили, — повторно нельзя
        return "used", p
    if now() > _aware(p.created_at) + timedelta(days=get_settings().code_valid_days):
        return "expired", p
    p.user_id = user.id
    p.activated_at = now()
    if not user.source and p.source:
        user.source = p.source
    await session.flush()
    await log_event(session, "code_redeemed", user.id, product=p.product, purchase=p.id)
    await grant_entitlement(session, user, p)
    return "ok", p


# --------------------------------------------------------------------------- абонемент звёздами: отмена и статусы


async def cancel_subscription(session: AsyncSession, user: User, bot) -> str:
    """Отключить автопродление (376-ФЗ: отказ в электронной форме). Доступ остаётся до конца оплаченного срока."""
    charge = user.star_sub_charge_id
    if not user.subscription_recurring:
        return "not_recurring"
    if bot is None or not charge:
        return "error"  # отменить можно только в Telegram: Настройки → Звёзды → Подписки
    try:
        await bot.edit_user_star_subscription(user_id=user.tg_id, telegram_payment_charge_id=charge, is_canceled=True)
    except Exception as e:
        if "SUBSCRIPTION" not in str(e).upper() and "NOT_FOUND" not in str(e).upper():
            log.warning("cancel star subscription failed: %s", e)
            return "error"
    user.subscription_recurring = False
    await log_event(session, "sub_canceled", user.id, via="bot")
    return "ok"


async def on_subscription_state(session: AsyncSession, user: User, state: str) -> None:
    """Telegram сообщил об изменении подписки: canceled | active | failed."""
    if state == "canceled":
        user.subscription_recurring = False
    elif state == "active":
        user.subscription_recurring = True
    await log_event(session, "sub_state", user.id, state=state)


# --------------------------------------------------------------------------- возвраты


@dataclass
class RefundCheck:
    purchase: Purchase | None
    eligible: bool  # гарантия «без вопросов»: можно вернуть сразу
    reason: str = ""
    until: datetime | None = None
    partial: bool = False  # гарантия не действует, но можно подать заявку на возврат за неиспользованную часть


async def _last_purchase(session: AsyncSession, user: User) -> Purchase | None:
    return await session.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id, Purchase.status == "paid", Purchase.provider.in_(("stars", "yookassa")),
        ).order_by(Purchase.id.desc()).limit(1)
    )


async def guarantee_available(session: AsyncSession, user: User) -> bool:
    """Гарантия «без вопросов» действует один раз: после возврата её больше не обещаем."""
    used = await session.scalar(
        select(func.count(Purchase.id)).where(Purchase.user_id == user.id, Purchase.status.in_(("refunded", "refund_requested")))
    )
    return not used


async def refund_check(session: AsyncSession, user: User) -> RefundCheck:
    s = get_settings()
    p = await _last_purchase(session, user)
    if p is None:
        return RefundCheck(None, False, "Нет оплат, которые можно вернуть. Если оплата была по коду — сначала "
                                        "активируй его (/code), а если что-то не так — напиши в /paysupport.")
    paid_at = _aware(p.activated_at or p.created_at)
    until = paid_at + timedelta(days=s.refund_days)
    refunded_before = await session.scalar(
        select(func.count(Purchase.id)).where(Purchase.user_id == user.id, Purchase.status.in_(("refunded", "refund_requested")))
    )
    if refunded_before:
        return RefundCheck(p, False, "Возврат без вопросов действует один раз. Можно подать заявку на возврат "
                                     "за неиспользованную часть — её рассмотрят в течение 10 дней.", until, partial=True)
    if now() > until:
        return RefundCheck(p, False, f"Гарантия «без вопросов» действует {s.refund_days} дня после оплаты. Можно подать "
                                     f"заявку на возврат за неиспользованную часть — её рассмотрят в течение 10 дней.",
                           until, partial=True)
    enr = await session.scalar(select(Enrollment).where(Enrollment.purchase_id == p.id).limit(1))
    if enr is not None and enr.plan_start_date is not None and enr.status in ("paid", "active"):
        if plan_day_number(enr.plan_start_date, today_for(user)) > s.refund_days:
            return RefundCheck(p, False, "Забег уже идёт дольше гарантийных дней. Можно подать заявку на возврат "
                                         "за неиспользованную часть.", until, partial=True)
    return RefundCheck(p, True, "", until)


async def refund_quote(session: AsyncSession, user: User | None, p: Purchase) -> int:
    """Сумма к возврату за неиспользованную часть (в единицах платежа: звёзды или копейки)."""
    if p.product == "run":
        enr = await session.scalar(select(Enrollment).where(Enrollment.purchase_id == p.id).limit(1))
        if enr is None or enr.plan_start_date is None or not enr.plan_days or user is None:
            return p.amount  # забег ещё не начат
        if enr.status not in ("paid", "active"):
            return 0
        used = max(0, plan_day_number(enr.plan_start_date, today_for(user)) - 1)
        left = max(0, enr.plan_days - used)
        return round(p.amount * left / enr.plan_days)
    if p.valid_until is None:
        return 0
    total = SUB_DAYS[p.product]
    left_days = max(0.0, (_aware(p.valid_until) - now()).total_seconds() / 86400)
    return round(p.amount * min(1.0, left_days / total))


async def revoke(session: AsyncSession, user: User, p: Purchase) -> None:
    """Забрать права, выданные покупкой."""
    if p.product == "run":
        enr = await session.scalar(select(Enrollment).where(Enrollment.purchase_id == p.id).limit(1))
        if enr is not None and enr.status in ("paid", "active"):
            enr.status = "refunded"
        elif enr is None and user.run_credits > 0:
            user.run_credits -= 1
    else:
        # снимаем только срок этой покупки: ранее оплаченные периоды остаются
        until = _aware(user.subscription_until) if user.subscription_until else now()
        user.subscription_until = max(now(), until - timedelta(days=SUB_DAYS[p.product]))
        if p.is_recurring:
            user.subscription_recurring = False
        if not subscription_active(user):
            user.subscription_recurring = False
            # возврат прекращает услугу: забег, открытый по абонементу, закрывается
            active = list(await session.scalars(
                select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.access == "subscription",
                                         Enrollment.status.in_(("paid", "active")))
            ))
            for e in active:
                e.status = "refunded"


async def _star_refund(bot, user: User, p: Purchase) -> bool:
    if p.is_recurring and user.star_sub_charge_id:
        try:
            await bot.edit_user_star_subscription(user_id=user.tg_id, telegram_payment_charge_id=user.star_sub_charge_id,
                                                  is_canceled=True)
        except Exception as e:  # подписки уже может не быть — возврату это не мешает
            log.info("cancel star subscription: %s", e)
    try:
        return bool(await bot.refund_star_payment(user_id=user.tg_id, telegram_payment_charge_id=p.telegram_charge_id))
    except Exception as e:
        if "CHARGE_ALREADY_REFUNDED" in str(e):
            return True
        log.warning("star refund failed: %s", e)
        return False


async def yookassa_refund(p: Purchase, amount_kop: int | None = None) -> bool:
    """Возврат через API ЮKassa (полный или частичный) с чеком возврата."""
    s = get_settings()
    if not (s.site_checkout and p.provider_charge_id):
        return False
    value = f"{(amount_kop if amount_kop is not None else p.amount) / 100:.2f}"
    body: dict = {"payment_id": p.provider_charge_id, "amount": {"value": value, "currency": "RUB"},
                  "description": f"Возврат по заказу #{p.id}"}
    if s.fiscal_receipts and p.email:
        item = receipt_item(p.product, float(value))
        body["receipt"] = {"customer": {"email": p.email}, "items": [item]}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{YOOKASSA_API}/refunds", json=body, auth=(s.yookassa_shop_id, s.yookassa_secret_key),
                             headers={"Idempotence-Key": f"refund-{p.id}-{value}"})
        if r.status_code in (200, 201) and r.json().get("status") in ("succeeded", "pending"):
            return True
        log.warning("yookassa refund failed: %s %s", r.status_code, r.text[:300])
    except httpx.HTTPError as e:
        log.warning("yookassa refund error: %s", e)
    return False


async def request_refund(session: AsyncSession, user: User, bot=None, outbox: Outbox | None = None) -> str:
    """Возврат по гарантии — сразу; иначе — заявка администратору с расчётом неиспользованной части.

    Результат: ok | requested | текст причины отказа.
    """
    chk = await refund_check(session, user)
    p = chk.purchase
    if p is None or not (chk.eligible or chk.partial):
        return chk.reason or "Возврат недоступен."
    ok = False
    if chk.eligible:
        if p.provider == "stars" and bot is not None and p.telegram_charge_id:
            ok = await _star_refund(bot, user, p)
        elif p.provider == "yookassa":
            ok = await yookassa_refund(p)
    if ok:
        p.status = "refunded"
        p.refunded_at = now()
        await revoke(session, user, p)
        await log_event(session, "refund", user.id, product=p.product, provider=p.provider, amount=p.amount, auto=True)
        return "ok"
    p.status = "refund_requested"
    quote = await refund_quote(session, user, p)
    await log_event(session, "refund_requested", user.id, product=p.product, provider=p.provider, amount=p.amount,
                    quote=quote, guarantee=chk.eligible)
    if outbox is not None:
        who = user.display_name + (f" (@{user.tg_username})" if user.tg_username else "")
        unit = (lambda v: f"{v} ⭐") if p.currency == "XTR" else (lambda v: texts.rub(v / 100))  # noqa: E731
        kind = "по гарантии (полностью)" if chk.eligible else "за неиспользованную часть"
        stars_note = " Звёзды возвращаются только целиком." if p.currency == "XTR" and not chk.eligible else ""
        for admin in get_settings().admin_ids:
            outbox.add(OutMsg(admin, f"↩️ Заявка на возврат {kind}: {texts.e(who)}, платёж #{p.id} на {unit(p.amount)} "
                                     f"({p.provider}). К возврату по расчёту: {unit(quote)}.{stars_note}\n"
                                     f"Вернуть: /refund_ok {p.id} [сумма]. Отметить ручной возврат: /refund_done {p.id}",
                              kind="admin"))
    return "requested" if chk.eligible else "requested_partial"


async def approve_refund(session: AsyncSession, purchase_id: int, bot=None, amount: int | None = None) -> str:
    """Администратор одобрил заявку: деньги возвращаются через провайдера, доступ закрывается."""
    p = await session.get(Purchase, purchase_id)
    if p is None:
        return "Нет такого платежа."
    if p.status == "refunded":
        return "Уже возвращено."
    user = await session.get(User, p.user_id) if p.user_id else None
    ok = False
    if p.provider == "stars":
        if user is None or bot is None or not p.telegram_charge_id:
            return "Возврат звёздами невозможен: нет аккаунта или идентификатора платежа."
        ok = await _star_refund(bot, user, p)
    elif p.provider == "yookassa":
        kop = None if amount is None else amount * 100
        if kop is not None and not (0 < kop <= p.amount):
            return f"Сумма должна быть от 1 до {p.amount // 100} ₽."
        ok = await yookassa_refund(p, kop)
    else:
        return "Этот платёж не через провайдера — отметь возврат вручную: /refund_done ID."
    if not ok:
        return "Провайдер не принял возврат — проверь кабинет и при необходимости верни вручную."
    await mark_refunded(session, p.id)
    return "ok"


async def mark_refunded(session: AsyncSession, purchase_id: int) -> Purchase | None:
    p = await session.get(Purchase, purchase_id)
    if p is None or p.status == "refunded":
        return p
    user = await session.get(User, p.user_id) if p.user_id else None
    p.status = "refunded"
    p.refunded_at = now()
    if user is not None:
        await revoke(session, user, p)
    await log_event(session, "refund", p.user_id, product=p.product, provider=p.provider, amount=p.amount, auto=False)
    return p


async def on_star_refunded(session: AsyncSession, telegram_charge_id: str) -> Purchase | None:
    """Telegram сообщил о возврате звёзд (в том числе по спору) — закрыть доступ."""
    p = await session.scalar(select(Purchase).where(Purchase.telegram_charge_id == telegram_charge_id))
    if p is None:
        return None
    return await mark_refunded(session, p.id)


# --------------------------------------------------------------------------- отчёты


async def sales_summary(session: AsyncSession, days: int | None = None) -> dict:
    q = select(Purchase).where(Purchase.status.in_(("paid", "refund_requested")))
    if days:
        q = q.where(Purchase.created_at >= now() - timedelta(days=days))
    rows = list(await session.scalars(q))
    rub = sum(p.amount for p in rows if p.currency == "RUB") / 100
    stars = sum(p.amount for p in rows if p.currency == "XTR")
    by_product: dict[str, int] = {}
    for p in rows:
        by_product[p.product] = by_product.get(p.product, 0) + 1
    refunds = await session.scalar(select(func.count(Purchase.id)).where(Purchase.status == "refunded"))
    not_activated = sum(1 for p in rows if p.activation_code and p.user_id is None)
    return {"count": len(rows), "rub": rub, "stars": stars, "by_product": by_product, "refunds": refunds or 0,
            "payers": len({p.user_id or f"o{p.id}" for p in rows if p.amount > 0}), "not_activated": not_activated}
