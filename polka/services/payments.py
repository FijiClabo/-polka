"""Оплата через ЮKassa: кнопка «Оплатить» → страница оплаты ЮKassa → доступ сразу после подтверждения.

Чтобы подключить: договор с ЮKassa → в .env YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY, а в кабинете ЮKassa
HTTP-уведомления на https://<домен>/pay/yookassa (payment.succeeded, payment.canceled, refund.succeeded).

Уведомлениям ЮKassa не верим на слово: по id из уведомления платёж перечитывается из API с ключом магазина,
так что подделанный запрос ничего не выдаст. Каждое событие обрабатывается идемпотентно.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Purchase, User
from services import billing
from services.common import Outbox, log_event, now
from settings import get_settings

log = logging.getLogger(__name__)

API = "https://api.yookassa.ru/v3"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
ID_RE = re.compile(r"^[0-9a-f-]{8,64}$")


class PaymentError(Exception):
    pass


def enabled() -> bool:
    return get_settings().payments_enabled


def needs_email(user: User) -> bool:
    """Для чека 54-ФЗ ЮKassa нужен e-mail покупателя — спрашиваем один раз, если чеки включены."""
    return get_settings().fiscal_receipts and not user.email


def _auth() -> tuple[str, str]:
    s = get_settings()
    return s.yookassa_shop_id, s.yookassa_secret_key


async def create_order(session: AsyncSession, user: User, product: str, email: str | None = None) -> Purchase:
    s = get_settings()
    if not s.payments_enabled:
        raise PaymentError("Оплата скоро появится.")
    if product not in billing.PRODUCTS:
        raise PaymentError("Неизвестный тариф.")
    if email:
        email = email.strip()
        if not EMAIL_RE.match(email):
            raise PaymentError("Проверь e-mail — на него придёт чек.")
        user.email = email[:128]
    if needs_email(user):
        raise PaymentError("Нужен e-mail для чека.")
    price = (await billing.prices_for(session, user))[product]
    if price.free:
        raise PaymentError("По этому промокоду доступ бесплатный — нажми «Активировать».")
    if price.promo and not await billing.promo_take(session, price.promo):
        raise PaymentError("Промокод только что закончился — обнови экран.")
    p = Purchase(
        user_id=user.id, product=product, provider="yookassa", currency="RUB", amount=price.rub * 100,
        list_amount=price.list_rub * 100, promo_code=price.promo, status="pending", order_id=uuid.uuid4().hex,
        email=user.email, source=user.source, offer_version=s.offer_version,
    )
    session.add(p)
    await session.flush()
    await log_event(session, "order_created", user.id, product=product, amount=p.amount, promo=p.promo_code)
    return p


async def create_payment(p: Purchase, return_url: str) -> str:
    """Платёж в ЮKassa с переходом на её страницу. Возвращает ссылку для оплаты."""
    s = get_settings()
    rub = p.amount / 100
    body: dict = {
        "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"{s.project_name}: {billing.product_title(p.product)} (заказ {p.id})"[:128],
        "metadata": {"order_id": p.order_id, "product": p.product, "user_id": str(p.user_id)},
    }
    if s.fiscal_receipts and p.email:
        body["receipt"] = {"customer": {"email": p.email}, "items": [billing.receipt_item(p.product, rub)]}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{API}/payments", json=body, auth=_auth(), headers={"Idempotence-Key": f"order-{p.order_id}"})
    except httpx.HTTPError as e:
        raise PaymentError("Платёжный сервис не ответил. Попробуй ещё раз через минуту.") from e
    if r.status_code not in (200, 201):
        log.error("yookassa create payment failed: %s %s", r.status_code, r.text[:500])
        raise PaymentError("Платёжный сервис не ответил. Попробуй ещё раз через минуту.")
    data = r.json()
    p.provider_charge_id = data.get("id")
    url = (data.get("confirmation") or {}).get("confirmation_url")
    if not url:
        raise PaymentError("Платёжный сервис не вернул ссылку на оплату.")
    return url


def return_url(order_id: str) -> str:
    return f"{get_settings().public_url}/pay/done?order={order_id}"


async def start_payment(session: AsyncSession, user: User, product: str, email: str | None = None) -> tuple[str, str]:
    """Заказ + платёж: (ссылка на оплату, id заказа)."""
    p = await create_order(session, user, product, email)
    url = await create_payment(p, return_url(p.order_id))
    return url, p.order_id


async def fetch_payment(payment_id: str) -> dict | None:
    if not ID_RE.match(payment_id or ""):
        return None  # id из уведомления — чужие данные, в адрес запроса их подставляем только после проверки
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{API}/payments/{payment_id}", auth=_auth())
    if r.status_code != 200:
        log.warning("yookassa get payment %s: %s", payment_id, r.status_code)
        return None
    return r.json()


async def fetch_refund(refund_id: str) -> dict | None:
    if not ID_RE.match(refund_id or ""):
        return None
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{API}/refunds/{refund_id}", auth=_auth())
    return r.json() if r.status_code == 200 else None


async def apply_payment_state(session: AsyncSession, p: Purchase, payment: dict, outbox: Outbox | None) -> bool:
    """Перенести статус платежа ЮKassa в заказ. True — заказ только что оплачен и доступ выдан."""
    if payment.get("id") != p.provider_charge_id:
        return False
    status = payment.get("status")
    if status == "succeeded" and p.status == "pending":
        amount = payment.get("amount") or {}
        if amount.get("currency") != "RUB" or round(float(amount.get("value", 0)) * 100) != p.amount:
            log.error("order %s: amount mismatch %s", p.id, amount)
            return False
        p.status = "paid"
        await session.flush()
        user = await session.get(User, p.user_id) if p.user_id else None
        await log_event(session, "purchase", p.user_id, product=p.product, provider="yookassa", amount=p.amount,
                        promo=p.promo_code, order=p.id)
        if user is not None:
            await billing.grant_entitlement(session, user, p)
        await billing.notify_admins_payment(session, user, p, outbox)
        return True
    if status == "canceled" and p.status == "pending":
        p.status = "canceled"
        if p.promo_code:
            await billing.promo_release(session, p.promo_code)  # заказ не оплачен — промокод снова свободен
        await log_event(session, "order_canceled", p.user_id, order=p.id,
                        reason=(payment.get("cancellation_details") or {}).get("reason"))
    return False


async def handle_notification(session: AsyncSession, body: dict, outbox: Outbox | None = None) -> Purchase | None:
    """Уведомление ЮKassa. Возвращает заказ, если он оплачен прямо сейчас."""
    event = body.get("event", "")
    obj = body.get("object") or {}
    if event.startswith("payment."):
        pid = obj.get("id")
        p = await session.scalar(select(Purchase).where(Purchase.provider_charge_id == pid).with_for_update()) if pid else None
        if p is None:
            return None
        payment = await fetch_payment(pid)
        if payment and await apply_payment_state(session, p, payment, outbox):
            return p
        return None
    if event == "refund.succeeded":
        refund = await fetch_refund(obj.get("id", ""))
        if not refund or refund.get("status") != "succeeded":
            return None  # возврат подтверждается только ответом API ЮKassa
        pid = refund.get("payment_id")
        p = await session.scalar(select(Purchase).where(Purchase.provider_charge_id == pid)) if pid else None
        if p is not None and p.status != "refunded":
            await billing.mark_refunded(session, p.id)
    return None


async def refresh_order(session: AsyncSession, order_id: str, outbox: Outbox | None = None) -> tuple[Purchase | None, bool]:
    """Для страницы «спасибо» и мини-приложения: если уведомление ещё не дошло — спросить статус у ЮKassa самим."""
    p = await session.scalar(select(Purchase).where(Purchase.order_id == order_id).with_for_update())
    if p is None:
        return None, False
    if p.status == "pending" and p.provider_charge_id and enabled():
        payment = await fetch_payment(p.provider_charge_id)
        if payment:
            return p, await apply_payment_state(session, p, payment, outbox)
    return p, False


async def expire_stale_orders(session: AsyncSession, older_than: timedelta = timedelta(days=2)) -> int:
    """Неоплаченные заказы старше двух дней закрываем и освобождаем зарезервированный промокод."""
    rows = list(await session.scalars(
        select(Purchase).where(Purchase.status == "pending", Purchase.created_at < now() - older_than).with_for_update()
    ))
    for p in rows:
        payment = await fetch_payment(p.provider_charge_id) if p.provider_charge_id and enabled() else None
        if payment and await apply_payment_state(session, p, payment, None):
            continue  # оказалось оплачено — уведомление просто потерялось
        if p.status == "pending":
            p.status = "canceled"
            if p.promo_code:
                await billing.promo_release(session, p.promo_code)
    return len(rows)
