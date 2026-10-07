"""Оплата в боте: тарифы, счета (ЮKassa и Telegram Stars), промокоды, гарантия возврата.

Telegram требует от ботов с оплатой команды /terms и /paysupport — они здесь.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BotSubscriptionUpdated, CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import texts
from bot.common import Flow, load_user
from bot.ui import flush, kb
from db.models import Book, User
from db.session import session_scope
from services import billing
from services.common import Outbox, log_event
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
    if not s.payments_enabled and not prices["run"].free:
        await bot.send_message(chat_id, texts.PAY_DISABLED + "\n\n" + texts.e(s.payment_info))
        return
    text = texts.paywall(book.title if book else None, plan_days, prices, card=s.payments_yookassa, stars=s.payments_stars,
                         guarantee_days=s.refund_days)
    await bot.send_message(chat_id, text, reply_markup=kb(texts.paywall_buttons(prices, card=s.payments_yookassa,
                                                                                stars=s.payments_stars)))


@router.message(Command("buy", "tariffs", "pay"))
async def cmd_buy(message: Message, bot: Bot) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        uid = user.id
    await send_paywall(bot, message.chat.id, uid)


async def send_invoice(bot: Bot, chat_id: int, inv: billing.Invoice) -> None:
    prices = [LabeledPrice(label=inv.title[:32], amount=inv.amount)]
    if inv.subscription_period:
        # подписку на Stars Telegram выставляет только ссылкой
        link = await bot.create_invoice_link(
            title=inv.title[:32], description=inv.description[:255], payload=inv.payload, currency=inv.currency,
            prices=prices, provider_token=inv.provider_token or None, subscription_period=inv.subscription_period,
        )
        await bot.send_message(chat_id, "Оформить абонемент на месяц со звёздами:",
                               reply_markup=kb([[{"text": f"Оплатить {inv.amount} ⭐", "url": link}]]))
        return
    await bot.send_invoice(
        chat_id=chat_id, title=inv.title[:32], description=inv.description[:255], payload=inv.payload,
        currency=inv.currency, prices=prices, provider_token=inv.provider_token or None,
        need_email=inv.need_email or None, send_email_to_provider=inv.send_email_to_provider or None,
        provider_data=inv.provider_data, start_parameter="buy",
    )


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, bot: Bot) -> None:
    _, product, method = call.data.split(":", 2)
    outbox = Outbox()
    if product == "free":
        async with session_scope() as s:
            user, _ = await load_user(s, call.from_user)
            p = await billing.apply_free_promo(s, user, method)
            uid = user.id
        await call.answer()
        if p is None:
            await call.message.answer(texts.PROMO_BAD)
            return
        await after_payment_message(bot, call.message.chat.id, uid, p.product)
        return
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        try:
            inv = await billing.build_invoice(s, user, product, method)
        except ValueError:
            inv = None
        await log_event(s, "invoice_opened", user.id, product=product, method=method, via="bot")
    await call.answer()
    if inv is None:
        await call.message.answer(texts.PAY_DISABLED)
        return
    try:
        await send_invoice(bot, call.message.chat.id, inv)
    except Exception as e:
        log.exception("invoice failed")
        await call.message.answer(f"Не получилось выставить счёт: {texts.e(str(e)[:200])}. Попробуй позже или напиши /paysupport.")
    await flush(bot, outbox)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
    async with session_scope() as s:
        err = await billing.check_pre_checkout(s, query.from_user.id, query.invoice_payload, query.currency,
                                               query.total_amount)
    if err:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message=err)
    else:
        await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    parsed = billing.parse_payload(sp.invoice_payload)
    outbox = Outbox()
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        if parsed is None:
            log.error("payment with unknown payload %s", sp.invoice_payload)
            product, promo = "run", None
        else:
            product, _uid, promo = parsed
        exp = None
        if sp.subscription_expiration_date:
            exp = datetime.fromtimestamp(sp.subscription_expiration_date, tz=UTC)
        await billing.record_payment(
            s, user, product=product, provider="stars" if sp.currency == "XTR" else "yookassa", currency=sp.currency,
            amount=sp.total_amount, telegram_charge_id=sp.telegram_payment_charge_id,
            provider_charge_id=sp.provider_payment_charge_id or None, promo=promo,
            is_recurring=bool(sp.is_recurring), renewal=bool(sp.is_recurring) and not sp.is_first_recurring,
            subscription_expiration=exp,
            email=sp.order_info.email if sp.order_info else None, outbox=outbox,
        )
        uid = user.id
    if sp.is_recurring and not sp.is_first_recurring:
        await message.answer(texts.pay_ok_subscription(exp, True) if exp else "Абонемент продлён ✅")
    else:
        await after_payment_message(bot, message.chat.id, uid, product)
    await flush(bot, outbox)


async def after_payment_message(bot: Bot, chat_id: int, user_id: int, product: str) -> None:
    async with session_scope() as s:
        user = await s.get(User, user_id)
        enr = await current_enrollment(s, user.id)
        view = await load_view(s, user, enr)
        until = user.subscription_until
        recurring = user.subscription_recurring
    if product in ("month", "year") and until:
        await bot.send_message(chat_id, texts.pay_ok_subscription(until, recurring))
    if view.state in ("to_read", "clarify"):
        from bot.handlers_day import today_kb, today_text

        msg = texts.pay_ok_started_today() if product == "run" else "Забег стартует сегодня 🔥"
        await bot.send_message(chat_id, msg)
        await bot.send_message(chat_id, today_text(view), reply_markup=today_kb(view))
    elif view.state == "not_started":
        if product == "run":
            await bot.send_message(chat_id, texts.pay_ok_started(view.starts_on))
        else:
            await bot.send_message(chat_id, f"Забег открыт, старт — {texts.d(view.starts_on)}.")
    elif product == "run":
        await bot.send_message(chat_id, texts.PAY_OK_CREDIT, reply_markup=kb([
            [{"text": "📖 У меня бумажная книга", "callback": "book:paper"}],
            [{"text": "Добавить книгу в приложении", "webapp": "book"}],
        ]))


@router.callback_query(F.data == "next:run")
async def cb_next_run(call: CallbackQuery, bot: Bot) -> None:
    """Следующая книга после финиша: новое участие, дальше — обычный путь книга → план → доступ."""
    await call.answer()
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


# --------------------------------------------------------------------------- события Telegram о звёздах


@router.message(F.refunded_payment)
async def refunded_payment(message: Message, bot: Bot) -> None:
    """Звёзды вернулись (возврат ботом или решение Telegram по спору) — доступ по этому платежу закрывается."""
    rp = message.refunded_payment
    async with session_scope() as s:
        p = await billing.on_star_refunded(s, rp.telegram_payment_charge_id)
    if p is not None:
        await message.answer("Возврат звёзд прошёл — доступ по этой оплате закрыт.")


@router.subscription()
async def subscription_update(update: BotSubscriptionUpdated, bot: Bot) -> None:
    async with session_scope() as s:
        from services.users import get_user_by_tg

        user = await get_user_by_tg(s, update.user.id)
        if user is None:
            return
        await billing.on_subscription_state(s, user, update.state)
        tg_id = user.tg_id
    if update.state == "failed":
        await bot.send_message(tg_id, "Не получилось продлить абонемент: на счету не хватило звёзд. Пополни звёзды — "
                                      "или оформи абонемент заново: /buy")


@router.message(Command("cancel_sub", "unsubscribe"))
async def cmd_cancel_sub(message: Message, bot: Bot) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        res = await billing.cancel_subscription(s, user, bot)
        until = user.subscription_until
    await message.answer(texts.sub_cancel_result(res, until))


# --------------------------------------------------------------------------- коды активации (оплата на сайте)


async def redeem_and_reply(bot: Bot, chat_id: int, tg_user, code: str) -> bool:
    outbox = Outbox()
    async with session_scope() as s:
        user, _ = await load_user(s, tg_user)
        status, p = await billing.redeem_code(s, user, code, outbox)
        uid = user.id
    if status != "ok" or p is None:
        await bot.send_message(chat_id, texts.CODE_RESULT.get(status, texts.CODE_RESULT["not_found"]))
        return False
    await bot.send_message(chat_id, texts.code_ok(p.product))
    await after_payment_message(bot, chat_id, uid, p.product)
    await flush(bot, outbox)
    return True


@router.message(Command("code"))
async def cmd_code(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    if command.args:
        await redeem_and_reply(bot, message.chat.id, message.from_user, command.args)
        return
    await state.set_state(Flow.code)
    await message.answer(texts.ASK_CODE)


@router.callback_query(F.data == "code:enter")
async def cb_code(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Flow.code)
    await call.message.answer(texts.ASK_CODE)


@router.message(Flow.code, F.text)
async def msg_code(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text.startswith("/"):
        await state.clear()
        return
    ok = await redeem_and_reply(bot, message.chat.id, message.from_user, message.text)
    if ok:
        await state.clear()


# --------------------------------------------------------------------------- промокоды


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


# --------------------------------------------------------------------------- гарантия и условия


@router.message(Command("money_back", "refund_me"))
async def cmd_money_back(message: Message) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        chk = await billing.refund_check(s, user)
    if chk.partial:
        await message.answer(texts.e(chk.reason), reply_markup=kb([[
            {"text": "Подать заявку", "callback": "mb:yes"}, {"text": "Не надо", "callback": "mb:no"},
        ]]))
        return
    if not chk.eligible:
        await message.answer(texts.e(chk.reason))
        return
    await message.answer(texts.REFUND_CONFIRM, reply_markup=kb([[
        {"text": "Да, вернуть", "callback": "mb:yes"}, {"text": "Нет, остаюсь", "callback": "mb:no"},
    ]]))


@router.callback_query(F.data.startswith("mb:"))
async def cb_money_back(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    if call.data == "mb:no":
        await call.message.answer("Отлично, читаем дальше 🔥")
        return
    outbox = Outbox()
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        res = await billing.request_refund(s, user, bot, outbox)
    await call.message.answer(texts.REFUND_OK if res == "ok" else texts.REFUND_REQUESTED if res == "requested" else texts.e(res))
    await flush(bot, outbox)


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(texts.terms_text(public_page("/offer"), public_page("/privacy"), public_page("/consent")))


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    await message.answer(texts.paysupport_text())
