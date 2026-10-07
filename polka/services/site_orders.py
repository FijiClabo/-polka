"""Оплата рублями на сайте через API ЮKassa.

Путь покупателя: лендинг → форма (тариф, e-mail, согласия) → страница оплаты ЮKassa (карта, СБП, SberPay, T-Pay)
→ возврат на /pay/done → одноразовый код активации → бот (t.me/<бот>?start=act_<код>).

Уведомлениям ЮKassa не верим на слово: по id из уведомления платёж перечитывается из API с ключом магазина,
так что подделанный запрос ничего не выдаст. Каждое событие обрабатывается идемпотентно.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import smtplib
import ssl
import uuid
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Consent, Purchase
from services import billing
from services.common import Outbox, log_event
from settings import get_settings

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
SRC_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class OrderError(Exception):
    pass


def _auth() -> tuple[str, str]:
    s = get_settings()
    return s.yookassa_shop_id, s.yookassa_secret_key


def doc_sha256(name: str) -> str | None:
    f = WEB_DIR / name
    return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None


async def create_order(session: AsyncSession, *, product: str, email: str, promo: str | None, source: str | None,
                       agree_offer: bool, agree_pd: bool) -> Purchase:
    s = get_settings()
    if not s.site_checkout:
        raise OrderError("Оплата на сайте пока не подключена.")
    if product not in billing.PRODUCTS:
        raise OrderError("Неизвестный тариф.")
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise OrderError("Проверь e-mail — на него придут чек и код активации.")
    if not (agree_offer and agree_pd):
        raise OrderError("Нужно принять оферту и дать согласие на обработку данных.")
    prices = await billing.prices_with_promo(session, promo)
    price = prices[product]
    if price.free:
        raise OrderError("Этот промокод даёт бесплатный доступ — активируй его в боте командой /promo.")
    if price.promo and not await billing.promo_take(session, price.promo):
        raise OrderError("Промокод только что закончился — цена без скидки. Обнови страницу.")  # резерв под этот заказ
    p = Purchase(
        user_id=None, product=product, provider="yookassa", currency="RUB", amount=price.rub * 100,
        list_amount=price.list_rub * 100, promo_code=price.promo, status="pending", order_id=uuid.uuid4().hex,
        email=email[:128], source=(f"site:{source}" if source and SRC_RE.match(source) else "site")[:64],
        offer_version=s.offer_version, is_recurring=False,
    )
    session.add(p)
    await session.flush()
    for doc_type, name in (("offer", "offer.html"), ("pd", "consent.html")):
        session.add(Consent(purchase_id=p.id, doc_type=doc_type, doc_version=s.offer_version,
                            doc_sha256=doc_sha256(name), channel="web", email=email[:128]))
    await log_event(session, "site_order", None, product=product, amount=p.amount, promo=p.promo_code, source=p.source)
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
        "metadata": {"order_id": p.order_id, "product": p.product},
    }
    if s.fiscal_receipts:
        body["receipt"] = {"customer": {"email": p.email}, "items": [billing.receipt_item(p.product, rub)]}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{billing.YOOKASSA_API}/payments", json=body, auth=_auth(),
                         headers={"Idempotence-Key": f"order-{p.order_id}"})
    if r.status_code not in (200, 201):
        log.error("yookassa create payment failed: %s %s", r.status_code, r.text[:500])
        raise OrderError("Платёжный сервис не ответил. Попробуй ещё раз через минуту.")
    data = r.json()
    p.provider_charge_id = data.get("id")
    url = (data.get("confirmation") or {}).get("confirmation_url")
    if not url:
        raise OrderError("Платёжный сервис не вернул ссылку на оплату.")
    return url


async def fetch_payment(payment_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f-]{8,64}", payment_id or ""):
        return None  # id из уведомления — чужие данные, в адрес запроса их подставляем только после проверки
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{billing.YOOKASSA_API}/payments/{payment_id}", auth=_auth())
    if r.status_code != 200:
        log.warning("yookassa get payment %s: %s", payment_id, r.status_code)
        return None
    return r.json()


async def apply_payment_state(session: AsyncSession, p: Purchase, payment: dict, outbox: Outbox | None) -> bool:
    """Перенести статус платежа ЮKassa в заказ. True — заказ только что оплачен (пора отправить код)."""
    if payment.get("id") != p.provider_charge_id:
        return False
    status = payment.get("status")
    if status == "succeeded" and p.status == "pending":
        amount = payment.get("amount") or {}
        if amount.get("currency") != "RUB" or round(float(amount.get("value", 0)) * 100) != p.amount:
            log.error("order %s: amount mismatch %s", p.id, amount)
            return False
        p.status = "paid"
        p.activation_code = billing.new_activation_code()
        await session.flush()  # промокод учтён при создании заказа
        await log_event(session, "purchase", None, product=p.product, provider="yookassa", currency="RUB",
                        amount=p.amount, promo=p.promo_code, order=p.id, source=p.source)
        await billing.notify_admins_payment(session, None, p, outbox)
        return True
    if status == "canceled" and p.status == "pending":
        p.status = "canceled"
        if p.promo_code:
            await billing.promo_release(session, p.promo_code)  # заказ не оплачен — промокод снова свободен
        await log_event(session, "site_order_canceled", None, order=p.id,
                        reason=(payment.get("cancellation_details") or {}).get("reason"))
    return False


async def handle_notification(session: AsyncSession, body: dict, outbox: Outbox | None = None) -> Purchase | None:
    """Уведомление ЮKassa (payment.succeeded, payment.canceled, refund.succeeded). Возвращает заказ, если он оплачен сейчас."""
    event = body.get("event", "")
    obj = body.get("object") or {}
    if event.startswith("payment."):
        pid = obj.get("id")
        if not pid:
            return None
        p = await session.scalar(select(Purchase).where(Purchase.provider_charge_id == pid).with_for_update())
        if p is None:
            return None
        payment = await fetch_payment(pid)
        if payment and await apply_payment_state(session, p, payment, outbox):
            return p
        return None
    if event == "refund.succeeded":
        refund = await fetch_refund(obj.get("id", "")) if obj.get("id") else None
        if not refund or refund.get("status") != "succeeded":
            return None  # возврат подтверждается только ответом API ЮKassa
        pid = refund.get("payment_id")
        p = await session.scalar(select(Purchase).where(Purchase.provider_charge_id == pid)) if pid else None
        if p is not None and p.status != "refunded":
            await billing.mark_refunded(session, p.id)
            await log_event(session, "refund_webhook", p.user_id, order=p.id,
                            amount=(refund.get("amount") or {}).get("value"))
    return None


async def fetch_refund(refund_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f-]{8,64}", refund_id or ""):
        return None
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{billing.YOOKASSA_API}/refunds/{refund_id}", auth=_auth())
    return r.json() if r.status_code == 200 else None


async def refresh_order(session: AsyncSession, order_id: str, outbox: Outbox | None = None) -> tuple[Purchase | None, bool]:
    """Для страницы «спасибо»: если уведомление ещё не дошло — спросить статус у ЮKassa самим."""
    p = await session.scalar(select(Purchase).where(Purchase.order_id == order_id).with_for_update())
    if p is None:
        return None, False
    if p.status == "pending" and p.provider_charge_id and get_settings().site_checkout:
        payment = await fetch_payment(p.provider_charge_id)
        if payment:
            return p, await apply_payment_state(session, p, payment, outbox)
    return p, False


def activation_link(code: str) -> str | None:
    from bot.ui import bot_username

    name = bot_username() or get_settings().bot_username
    return f"https://t.me/{name}?start=act_{code}" if name else None


# --------------------------------------------------------------------------- письмо с кодом


def _send_mail(to: str, subject: str, text: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=30) as smtp:
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ctx)
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)


async def email_code(p: Purchase) -> bool:
    """Отправить код активации на e-mail покупателя (если настроена почта)."""
    s = get_settings()
    if not (s.smtp_enabled and p.email and p.activation_code):
        return False
    link = activation_link(p.activation_code)
    text = (
        f"Спасибо за оплату! {billing.product_title(p.product)}.\n\n"
        f"Код активации: {p.activation_code}\n"
        + (f"Открыть в Telegram и активировать одним нажатием: {link}\n" if link else "")
        + "\nИли открой бота и отправь команду /code с этим кодом.\n"
        f"Код действует {s.code_valid_days} дней. Возврат в первые {s.refund_days} дня после активации — без вопросов.\n\n"
        f"{s.project_name}"
    )
    try:
        await asyncio.to_thread(_send_mail, p.email, f"{s.project_name}: код активации", text)
        return True
    except Exception as e:
        log.warning("email to order %s failed: %s", p.id, e)
        return False


async def expire_stale_orders(session: AsyncSession, older_than: timedelta = timedelta(days=2)) -> int:
    """Неоплаченные заказы старше двух дней закрываем и освобождаем зарезервированный промокод."""
    from services.common import now

    rows = list(await session.scalars(
        select(Purchase).where(Purchase.status == "pending", Purchase.created_at < now() - older_than).with_for_update()
    ))
    for p in rows:
        payment = await fetch_payment(p.provider_charge_id) if p.provider_charge_id and get_settings().site_checkout else None
        if payment and await apply_payment_state(session, p, payment, None):
            continue  # оказалось оплачено — уведомление просто потерялось
        if p.status == "pending":
            p.status = "canceled"
            if p.promo_code:
                await billing.promo_release(session, p.promo_code)
    return len(rows)
