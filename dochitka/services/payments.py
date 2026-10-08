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

from core import clock
from db.models import Purchase, User
from services import billing
from services.common import Outbox, OutMsg, log_event
from settings import get_settings

log = logging.getLogger(__name__)

API = "https://api.yookassa.ru/v3"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
ID_RE = re.compile(r"^[0-9a-f-]{8,64}$")
REUSE_ORDER_FOR = timedelta(minutes=30)  # повторное «Оплатить» в течение получаса открывает тот же платёж


class PaymentError(Exception):
    pass


class RetryLater(Exception):
    """ЮKassa не ответила: уведомление нужно повторить позже (ЮKassa сама повторяет его до суток)."""


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
        raise PaymentError("По этому промокоду доступ бесплатный — платить не нужно, нажми кнопку с промокодом.")
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
        if r.status_code >= 500 or r.status_code == 429:
            raise PaymentError("Платёжный сервис не ответил. Попробуй ещё раз через минуту.")
        try:
            param = str((r.json() or {}).get("parameter") or "")
        except ValueError:
            param = ""
        if "email" in param:
            raise PaymentError("Проверь e-mail — на него придёт чек.")
        raise PaymentError("Не получилось создать платёж. Напиши в поддержку — разберёмся.")
    data = r.json()
    p.provider_charge_id = data.get("id")
    url = (data.get("confirmation") or {}).get("confirmation_url")
    if not url:
        raise PaymentError("Платёжный сервис не вернул ссылку на оплату.")
    return url


def return_url(order_id: str) -> str:
    return f"{get_settings().public_url}/pay/done?order={order_id}"


async def _reusable_order(session: AsyncSession, user: User, product: str) -> tuple[str, Purchase] | None:
    """Человек снова нажал «Оплатить», а прошлый платёж ещё ждёт: отдаём ту же ссылку.

    Так повторное нажатие не создаёт второй заказ и не занимает ещё одно использование промокода.
    """
    p = await session.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id, Purchase.product == product, Purchase.provider == "yookassa",
            Purchase.status == "pending", Purchase.provider_charge_id.is_not(None),
            Purchase.created_at >= clock.real_now() - REUSE_ORDER_FOR,
        ).order_by(Purchase.id.desc()).limit(1)
    )
    if p is None or (p.promo_code or None) != (user.promo_code or None):
        return None
    if get_settings().fiscal_receipts and user.email and p.email != user.email:
        return None
    try:
        payment = await fetch_payment(p.provider_charge_id)
    except RetryLater:
        return None
    if not payment or payment.get("status") != "pending":
        return None
    url = (payment.get("confirmation") or {}).get("confirmation_url")
    return (url, p) if url else None


async def start_payment(session: AsyncSession, user: User, product: str,
                        email: str | None = None) -> tuple[str, Purchase]:
    """Заказ + платёж: (ссылка на оплату, заказ). Сумма для кнопки — из заказа, а не пересчитанная."""
    if email:
        email = email.strip()
        if not EMAIL_RE.match(email):
            raise PaymentError("Проверь e-mail — на него придёт чек.")
        user.email = email[:128]
    if enabled() and product in billing.PRODUCTS and not needs_email(user):
        reused = await _reusable_order(session, user, product)
        if reused:
            return reused
    p = await create_order(session, user, product, email)
    url = await create_payment(p, return_url(p.order_id))
    return url, p


async def _api_get(path: str) -> dict | None:
    """GET к API ЮKassa. None — объекта нет (404); RetryLater — ЮKassa сейчас не ответила."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{API}/{path}", auth=_auth())
    except httpx.HTTPError as e:
        raise RetryLater(str(e)) from e
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    log.warning("yookassa GET %s: %s", path, r.status_code)
    raise RetryLater(f"yookassa {r.status_code}")


async def fetch_payment(payment_id: str) -> dict | None:
    if not ID_RE.match(payment_id or ""):
        return None  # id из уведомления — чужие данные, в адрес запроса их подставляем только после проверки
    return await _api_get(f"payments/{payment_id}")


async def fetch_refund(refund_id: str) -> dict | None:
    if not ID_RE.match(refund_id or ""):
        return None
    return await _api_get(f"refunds/{refund_id}")


async def _lock(session: AsyncSession, p: Purchase) -> Purchase:
    """Перечитать заказ под блокировкой: два уведомления (или уведомление и опрос) не выдадут доступ дважды."""
    return await session.scalar(
        select(Purchase).where(Purchase.id == p.id).with_for_update().execution_options(populate_existing=True)
    )


async def apply_payment_state(session: AsyncSession, p: Purchase, payment: dict, outbox: Outbox | None) -> bool:
    """Перенести статус платежа ЮKassa в заказ. True — заказ только что оплачен и доступ выдан."""
    if payment.get("id") != p.provider_charge_id:
        return False
    status = payment.get("status")
    if status == "succeeded" and p.status in ("pending", "canceled"):
        amount = payment.get("amount") or {}
        if amount.get("currency") != "RUB" or round(float(amount.get("value", 0)) * 100) != p.amount:
            log.error("order %s: amount mismatch %s", p.id, amount)
            return False
        if p.status == "canceled":
            # заказ закрыли у нас, а оплата всё же прошла (например, деньги дошли поздно) — доступ всё равно выдаём
            log.warning("order %s: paid after local cancel", p.id)
            if p.promo_code:
                await billing.promo_take(session, p.promo_code, enforce_limit=False)
        p.status = "paid"
        await session.flush()
        user = await session.get(User, p.user_id) if p.user_id else None
        await log_event(session, "purchase", p.user_id, product=p.product, provider="yookassa", amount=p.amount,
                        promo=p.promo_code, order=p.id)
        if user is not None:
            if p.promo_code and user.promo_code == p.promo_code:
                user.promo_code = None  # промокод использован; источник (promo:КОД) остаётся в user.source
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
    """Уведомление ЮKassa. Возвращает заказ, если он оплачен прямо сейчас.

    RetryLater — ЮKassa не ответила на проверку: вебхук отвечает ошибкой, и ЮKassa повторит уведомление.
    """
    event = body.get("event", "")
    obj = body.get("object") or {}
    if event.startswith("payment."):
        pid = obj.get("id")
        p = await session.scalar(select(Purchase).where(Purchase.provider_charge_id == pid)) if pid else None
        if p is None:
            return None
        payment = await fetch_payment(pid)  # до блокировки: не держим заказ, пока ждём ответа ЮKassa
        if not payment:
            return None
        p = await _lock(session, p)
        if await apply_payment_state(session, p, payment, outbox):
            return p
        return None
    if event == "refund.succeeded":
        refund = await fetch_refund(obj.get("id", ""))
        if not refund or refund.get("status") != "succeeded":
            return None  # возврат подтверждается только ответом API ЮKassa
        pid = refund.get("payment_id")
        p = await session.scalar(select(Purchase).where(Purchase.provider_charge_id == pid)) if pid else None
        if p is None or p.status == "refunded":
            return None
        refunded = _kopecks((refund.get("amount") or {}).get("value"))
        payment = await fetch_payment(pid)
        if payment and payment.get("refunded_amount"):
            refunded = max(refunded, _kopecks(payment["refunded_amount"].get("value")))
        if refunded < p.amount:
            # частичный возврат (жест доброй воли) — доступ не забираем, просто сообщаем админам
            await log_event(session, "partial_refund", p.user_id, order=p.id, amount=refunded)
            if outbox is not None:
                for admin in get_settings().admin_ids:
                    outbox.add(OutMsg(admin, f"Частичный возврат по заказу #{p.id}: {refunded / 100:.0f} ₽ "
                                             f"из {p.amount / 100:.0f} ₽. Доступ оставлен.", kind="admin"))
            return None
        await billing.mark_refunded(session, p.id)
    return None


def _kopecks(value) -> int:
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return 0


async def refresh_order(session: AsyncSession, order_id: str, outbox: Outbox | None = None) -> tuple[Purchase | None, bool]:
    """Для страницы «спасибо» и мини-приложения: если уведомление ещё не дошло — спросить статус у ЮKassa самим."""
    p = await session.scalar(select(Purchase).where(Purchase.order_id == order_id))
    if p is None:
        return None, False
    if p.status != "pending" or not p.provider_charge_id or not enabled():
        return p, False
    try:
        payment = await fetch_payment(p.provider_charge_id)
    except RetryLater:
        return p, False
    if not payment:
        return p, False
    p = await _lock(session, p)
    return p, await apply_payment_state(session, p, payment, outbox)


async def expire_stale_orders(session: AsyncSession, older_than: timedelta = timedelta(days=2)) -> int:
    """Старые неоплаченные заказы: закрываем только то, что ЮKassa подтвердила как отменённое (или где платежа нет).

    Если ЮKassa не ответила или платёж ещё ждёт — заказ не трогаем: ЮKassa сама отменит неоплаченный платёж,
    и придёт уведомление. Время — настоящее (created_at пишется по реальным часам, даже в ускоренном режиме).
    """
    if not enabled():
        return 0
    ids = list(await session.scalars(
        select(Purchase.id).where(Purchase.status == "pending", Purchase.provider == "yookassa",
                                  Purchase.created_at < clock.real_now() - older_than)
    ))
    closed = 0
    for pid in ids:
        p = await session.get(Purchase, pid)
        if p is None or p.status != "pending":
            continue
        if p.provider_charge_id:
            try:
                payment = await fetch_payment(p.provider_charge_id)
            except RetryLater:
                continue
            if payment is not None:
                p = await _lock(session, p)
                await apply_payment_state(session, p, payment, None)
                closed += p.status != "pending"
                continue
        p = await _lock(session, p)  # платёж в ЮKassa так и не создался или его там нет
        if p.status == "pending":
            p.status = "canceled"
            if p.promo_code:
                await billing.promo_release(session, p.promo_code)
            closed += 1
    return closed
