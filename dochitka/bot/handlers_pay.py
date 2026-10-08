"""Оплата в боте: тарифы и кнопка «Оплатить» (страница ЮKassa), промокоды, условия."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

import texts
from bot.common import load_user
from bot.ui import kb
from db.models import Book, User
from db.session import session_scope
from services import billing, payments
from services.common import log_event
from services.progress import load_view
from services.runs import current_enrollment, ensure_enrollment
from settings import get_settings

log = logging.getLogger(__name__)
router = Router(name="pay")


def public_page(path: str) -> str | None:
    base = get_settings().public_url
    return f"{base}{path}" if base.startswith("http") else None


async def send_paywall(bot: Bot, chat_id: int, user_id: int) -> None:
    s = get_settings()
    async with session_scope() as ses:
        user = await ses.get(User, user_id)
        enr = await current_enrollment(ses, user.id)
        book = await ses.get(Book, enr.book_id) if enr and enr.book_id else None
        prices = await billing.prices_for(ses, user)
        await log_event(ses, "paywall_shown", user.id, via="bot")
        plan_days = enr.plan_days if enr else None
        need_email = payments.needs_email(user)
    free = [p for p in prices.values() if p.free and p.promo]
    if not s.payments_enabled and not free:
        await bot.send_message(chat_id, texts.paywall(book.title if book else None, plan_days, prices) + "\n\n"
                               + texts.e(s.payment_info))
        return
    await bot.send_message(chat_id, texts.paywall(book.title if book else None, plan_days, prices),
                           reply_markup=kb(texts.paywall_buttons(prices, in_chat=not need_email)))


@router.message(Command("buy", "tariffs", "pay"))
async def cmd_buy(message: Message, bot: Bot) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        uid = user.id
    await send_paywall(bot, message.chat.id, uid)


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, bot: Bot) -> None:
    """pay:<тариф> — ссылка на оплату в ЮKassa; pay:free:<тариф> — промокод на 100%."""
    from bot.handlers_start import consent_gate

    await call.answer()
    if not await consent_gate(call.message, call.from_user):
        return
    parts = call.data.split(":")
    if len(parts) == 3 and parts[1] == "free":
        async with session_scope() as s:
            user, _ = await load_user(s, call.from_user)
            p = await billing.apply_free_promo(s, user, parts[2])
            uid = user.id
        if p is None:
            await call.message.answer(texts.PROMO_BAD)
            return
        await after_payment_message(bot, call.message.chat.id, uid, p.product, bool(getattr(p, "activated_now", None)))
        return
    product = parts[1] if len(parts) > 1 else "run"
    try:
        async with session_scope() as s:
            user, _ = await load_user(s, call.from_user)
            url, order = await payments.start_payment(s, user, product)
            amount = order.amount // 100  # сумма из заказа: ровно то, что спишет ЮKassa
            await log_event(s, "payment_link", user.id, product=product, via="bot")
    except payments.PaymentError as e:
        await call.message.answer(texts.e(str(e)), reply_markup=kb([[{"text": "Оплатить в приложении", "webapp": "pay"}]]))
        return
    await call.message.answer(texts.pay_link(product), reply_markup=kb([[{"text": f"Оплатить {texts.rub(amount)}", "url": url}]]))


async def after_payment_message(bot: Bot, chat_id: int, user_id: int, product: str, activated: bool = False) -> None:
    """Что сказать после оплаты (ЮKassa, промокод, ручная выдача). activated — забег стартовал этой оплатой."""
    async with session_scope() as s:
        user = await s.get(User, user_id)
        enr = await current_enrollment(s, user.id)
        view = await load_view(s, user, enr)
        until = user.subscription_until
    if product in ("month", "year") and until:
        await bot.send_message(chat_id, texts.pay_ok_subscription(until))
    book_buttons = kb([
        [{"text": "У меня бумажная книга", "callback": "book:paper"}],
        [{"text": "Добавить книгу в приложении", "webapp": "book"}],
    ])
    if activated and view.state in ("to_read", "clarify"):
        from bot.handlers_day import today_kb, today_text

        await bot.send_message(chat_id, texts.pay_ok_started_today())
        await bot.send_message(chat_id, today_text(view), reply_markup=today_kb(view))
    elif activated and view.state == "not_started":
        await bot.send_message(chat_id, texts.pay_ok_started(view.starts_on))
    elif view.state in ("to_read", "clarify", "checking", "done_today", "not_started", "waiting_start"):
        # идёт другой забег (например, спринт) — покупка подождёт следующей книги
        if product == "run":
            await bot.send_message(chat_id, texts.PAY_OK_NEXT)
    elif view.state == "plan_needed":
        await bot.send_message(chat_id, texts.PAY_OK_PLAN, reply_markup=kb([[{"text": "Выбрать срок", "webapp": "book"}]]))
    elif product == "run":
        await bot.send_message(chat_id, texts.PAY_OK_CREDIT, reply_markup=book_buttons)
    elif view.state in ("no_run", "no_book", "finished", "expired", "refunded", "parse_failed"):
        await bot.send_message(chat_id, texts.add_book_prompt(), reply_markup=book_buttons)


@router.callback_query(F.data == "next:run")
async def cb_next_run(call: CallbackQuery, bot: Bot) -> None:
    """Следующая книга после финиша: новое участие, дальше — обычный путь книга → план → доступ."""
    from bot.handlers_start import consent_gate

    await call.answer()
    if not await consent_gate(call.message, call.from_user):
        return
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        enr = await ensure_enrollment(s, user)
        await log_event(s, "next_run", user.id, enr.run_id, via="bot")
        has_book, uid = enr.book_id is not None, user.id
    if has_book:
        from bot.handlers_start import send_status

        await send_status(bot, call.message.chat.id, uid)
        return
    from bot.handlers_start import send_book_prompt

    await send_book_prompt(bot, call.message.chat.id)


# --------------------------------------------------------------------------- промокоды и условия


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject, bot: Bot) -> None:
    code = billing.normalize_code(command.args)
    if not code:
        await message.answer("Напиши код после команды, например: <code>/promo READ20</code>")
        return
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        promo = await billing.valid_promo(s, code)
        if promo is None:
            await message.answer(texts.PROMO_BAD)
            return
        user.promo_code = promo.code
        if not user.source:
            user.source = f"promo:{promo.code}"
        await log_event(s, "promo_applied", user.id, code=promo.code)
        discount, uid = promo.discount_percent, user.id
    await message.answer(texts.promo_applied(code, discount))
    await send_paywall(bot, message.chat.id, uid)


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(texts.terms_text(public_page("/offer"), public_page("/privacy"), public_page("/consent")))


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    await message.answer(texts.paysupport_text())
