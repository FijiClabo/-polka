"""/start, онбординг, часовой пояс и время, пробный пересказ, статус."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import texts
from ai import prompts
from ai.checker import CheckInput, check_retelling
from ai.llm import LLMUnavailable
from bot.common import Flow, load_user, parse_time, parse_tz, tz_from_location, tz_label
from bot.ui import REMOVE_KB, app_kb, flush, kb, location_kb
from db.models import User
from db.session import session_scope
from services.common import Outbox, log_event, now
from services.progress import load_view
from services.runs import current_enrollment, join_cohort, sprint_block, start_sprint
from services.social import add_friend_by_code, join_pair_by_code
from settings import get_settings

log = logging.getLogger(__name__)
router = Router(name="start")


# --------------------------------------------------------------------------- /start


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    payload = (command.args or "").strip()
    outbox = Outbox()
    async with session_scope() as s:
        user, created = await load_user(s, message.from_user)
        inviter_name = None
        if payload.startswith("f_"):
            inviter = await add_friend_by_code(s, user, payload[2:], is_new_user=created or user.onboarding_step != "done",
                                               outbox=outbox)
            if inviter:
                inviter_name = inviter.display_name
                if not user.source:
                    user.source = "friend"
                if not created and user.onboarding_step == "done":
                    outbox.add(_reply(user, texts.friends_now(inviter.display_name)))
        elif payload.startswith("promo_") or payload.startswith("src_"):
            # метка источника (реклама, блогер) и промокод из ссылки: t.me/<бот>?start=promo_READ20
            from services.billing import normalize_code, valid_promo

            if payload.startswith("promo_"):
                promo = await valid_promo(s, payload[6:])
                if promo is not None:
                    user.promo_code = promo.code
                    outbox.add(_reply(user, texts.promo_applied(promo.code, promo.discount_percent)))
                if not user.source:
                    user.source = f"promo:{normalize_code(payload[6:])}"
            elif not user.source:
                user.source = payload[4:36]
        elif payload == "group":
            # групповой забег ведущего — только по его ссылке
            status, _enr = await join_cohort(s, user)
            outbox.add(_reply(user, texts.COHORT_JOIN[status]))
        elif payload.startswith("p_"):
            status, inviter = await join_pair_by_code(s, user, payload[2:], outbox)
            if status == "ok" and inviter:
                outbox.add(_reply(user, texts.pair_joined(inviter.display_name)))
            elif status == "taken":
                outbox.add(_reply(user, "У этого участника уже есть напарник."))
            elif status == "already_paired":
                outbox.add(_reply(user, "У тебя уже есть напарник в этом забеге."))
        if inviter_name:
            await s.flush()
        onboarded = user.onboarding_step == "done"
        uid = user.id
        await state.update_data(inviter_name=inviter_name)
    await flush(bot, outbox)
    if onboarded:
        await send_status(bot, message.chat.id, uid)
        return
    await send_welcome(message, 0)


def _reply(user: User, text: str):
    from services.common import OutMsg

    return OutMsg(user.tg_id, text)


async def send_welcome(message: Message, idx: int) -> None:
    parts = texts.welcome()
    btn = "Дальше →" if idx < len(parts) - 1 else "Поехали"
    cb = f"ob:{idx + 1}" if idx < len(parts) - 1 else "ob:go"
    await message.answer(parts[idx], reply_markup=kb([[{"text": btn, "callback": cb}]]))


@router.callback_query(F.data.startswith("ob:"))
async def cb_onboarding(call: CallbackQuery) -> None:
    await call.answer()
    arg = call.data.split(":", 1)[1]
    await call.message.edit_reply_markup(reply_markup=None)
    if arg == "go":
        await ask_consent(call.message)
        return
    await send_welcome(call.message, int(arg))


async def ask_consent(message: Message) -> None:
    from bot.handlers_pay import public_page

    await message.answer(texts.consent_text(public_page("/consent"), public_page("/privacy")),
                         reply_markup=kb([[{"text": texts.CONSENT_BTN, "callback": "consent:pd"}]]),
                         disable_web_page_preview=True)


@router.callback_query(F.data == "consent:pd")
async def cb_consent(call: CallbackQuery) -> None:
    from services.consent import give_consent

    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        await give_consent(s, user, "bot")
        onboarded = user.onboarding_step == "done"
    if onboarded:
        await call.message.answer("Спасибо! Можно продолжать.")
        return
    await ask_tz(call.message)


async def consent_gate(message: Message, tg_user) -> bool:
    """Проверка согласия в отдельной сессии — для обработчиков книг, пересказов и оплаты."""
    async with session_scope() as s:
        user, _ = await load_user(s, tg_user)
        return await require_consent(message, user, s)


async def require_consent(message: Message, user: User, session) -> bool:
    """Книги, пересказы и оплата — только после согласия на обработку данных. False — показали экран согласия."""
    from services.consent import has_consent

    if await has_consent(session, user):
        return True
    await ask_consent(message)
    return False


# --------------------------------------------------------------------------- часовой пояс и время


async def ask_tz(message: Message, settings_mode: bool = False) -> None:
    from bot.common import TZ_BUTTONS

    rows, row = [], []
    prefix = "stz" if settings_mode else "tz"
    for label, tz in TZ_BUTTONS:
        row.append({"text": label, "callback": f"{prefix}:{tz}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Другой пояс", "callback": f"{prefix}:other"}])
    await message.answer(texts.ASK_TZ, reply_markup=kb(rows))
    await message.answer("Или отправь геопозицию — определю сам.", reply_markup=location_kb())


async def _save_tz(tg_id: int, tz: str) -> None:
    async with session_scope() as s:
        from services.users import get_user_by_tg

        u = await get_user_by_tg(s, tg_id)
        if u:
            u.timezone = tz


@router.callback_query(F.data.startswith("tz:") | F.data.startswith("stz:"))
async def cb_tz(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    prefix, tz = call.data.split(":", 1)
    settings_mode = prefix == "stz"
    if tz == "other":
        await state.set_state(Flow.tz_input)
        await state.update_data(settings_mode=settings_mode)
        await call.message.answer(texts.ASK_TZ_OTHER)
        return
    await _save_tz(call.from_user.id, tz)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(texts.tz_set(tz_label(tz)), reply_markup=REMOVE_KB)
    if not settings_mode:
        await ask_morning(call.message)


@router.message(Flow.tz_input, F.text)
async def msg_tz_input(message: Message, state: FSMContext) -> None:
    tz = parse_tz(message.text)
    if not tz:
        await message.answer(texts.TZ_BAD)
        return
    data = await state.get_data()
    await state.set_state(None)
    await _save_tz(message.from_user.id, tz)
    await message.answer(texts.tz_set(tz_label(tz)), reply_markup=REMOVE_KB)
    if not data.get("settings_mode"):
        await ask_morning(message)


@router.message(F.location)
async def msg_location(message: Message, state: FSMContext) -> None:
    tz = tz_from_location(message.location.latitude, message.location.longitude)
    if not tz:
        await message.answer("Не получилось определить пояс по геопозиции. Выбери кнопкой.", reply_markup=REMOVE_KB)
        return
    await _save_tz(message.from_user.id, tz)
    await state.set_state(None)
    await message.answer(texts.tz_set(tz_label(tz)), reply_markup=REMOVE_KB)
    async with session_scope() as s:
        from services.users import get_user_by_tg

        u = await get_user_by_tg(s, message.from_user.id)
        onboarded = u and u.onboarding_step == "done"
    if not onboarded:
        await ask_morning(message)


async def ask_morning(message: Message, settings_mode: bool = False) -> None:
    p = "smt" if settings_mode else "mt"
    rows = [[{"text": t, "callback": f"{p}:{t}"} for t in ("07:00", "08:00", "09:00", "10:00")],
            [{"text": "Другое время", "callback": f"{p}:other"}]]
    await message.answer(texts.ASK_MORNING, reply_markup=kb(rows))


async def ask_evening(message: Message, settings_mode: bool = False) -> None:
    p = "set" if settings_mode else "et"
    rows = [[{"text": t, "callback": f"{p}:{t}"} for t in ("20:00", "21:00", "22:00", "23:00")],
            [{"text": "Другое время", "callback": f"{p}:other"}]]
    await message.answer(texts.ASK_EVENING, reply_markup=kb(rows))


async def _save_time(tg_id: int, which: str, value) -> None:
    async with session_scope() as s:
        from services.users import get_user_by_tg

        u = await get_user_by_tg(s, tg_id)
        if u:
            setattr(u, "morning_time" if which == "m" else "evening_time", value)


@router.callback_query(F.data.regexp(r"^(mt|et|smt|set):"))
async def cb_time(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    prefix, val = call.data.split(":", 1)
    which = "m" if prefix in ("mt", "smt") else "e"
    settings_mode = prefix.startswith("s")
    if val == "other":
        await state.set_state(Flow.morning_input if which == "m" else Flow.evening_input)
        await state.update_data(settings_mode=settings_mode)
        await call.message.answer("Напиши время в формате <code>08:30</code>.")
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await _after_time(call.message, call.from_user.id, which, parse_time(val), settings_mode, state)


@router.message(Flow.morning_input, F.text)
@router.message(Flow.evening_input, F.text)
async def msg_time_input(message: Message, state: FSMContext) -> None:
    t = parse_time(message.text)
    if not t:
        await message.answer(texts.TIME_BAD)
        return
    cur = await state.get_state()
    data = await state.get_data()
    await state.set_state(None)
    which = "m" if cur == Flow.morning_input.state else "e"
    await _after_time(message, message.from_user.id, which, t, bool(data.get("settings_mode")), state)


async def _after_time(message: Message, tg_id: int, which: str, t, settings_mode: bool, state: FSMContext) -> None:
    await _save_time(tg_id, which, t)
    await message.answer(f"Записано: {t.strftime('%H:%M')}.")
    if settings_mode:
        return
    if which == "m":
        await ask_evening(message)
    else:
        await start_trial(message, state)


# --------------------------------------------------------------------------- пробный пересказ


async def start_trial(message: Message, state: FSMContext) -> None:
    await state.set_state(Flow.trial)
    await state.update_data(trial_dialog=[], trial_clarify=0)
    await message.answer(
        texts.trial_intro(prompts.TRIAL_TITLE, prompts.TRIAL_AUTHOR, prompts.TRIAL_TEXT),
        reply_markup=kb([[{"text": texts.TRIAL_SKIP, "callback": "trial:skip"}]]),
    )


async def trial_answer(message: Message, state: FSMContext, text: str) -> None:
    data = await state.get_data()
    dialog = data.get("trial_dialog", [])
    clar = data.get("trial_clarify", 0)
    inp = CheckInput(
        title=prompts.TRIAL_TITLE, author=prompts.TRIAL_AUTHOR, segment_title="Пробный отрывок", day_number=1,
        pages="1 страница", segment_text=prompts.TRIAL_TEXT, retelling=text, dialog=[tuple(x) for x in dialog],
        clarify_count=clar,
    )
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        v = await check_retelling(inp)
    except LLMUnavailable:
        await message.answer(texts.TRIAL_AI_OFF)
        await finish_trial(message, state)
        return
    if v.verdict == "clarify" and clar < 1:
        dialog += [["user", text], ["ai", f"{v.reply} {v.question or ''}"]]
        await state.update_data(trial_dialog=dialog, trial_clarify=clar + 1)
        await message.answer(texts.clarify(v.reply, v.question))
        return
    if v.verdict == "rejected":
        await message.answer(texts.rejected(v.reply), reply_markup=kb([[{"text": texts.TRIAL_SKIP, "callback": "trial:skip"}]]))
        return
    if v.verdict == "accepted":
        await message.answer(f"<b>Засчитано.</b> {texts.e(v.reply)}")
    else:
        await message.answer(texts.e(v.reply))
    await finish_trial(message, state)


@router.callback_query(F.data == "trial:skip")
async def cb_trial_skip(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await finish_trial(call.message, state, user_tg=call.from_user)


async def finish_trial(message: Message, state: FSMContext, user_tg=None) -> None:
    data = await state.get_data()
    await state.clear()
    tg = user_tg or message.from_user
    if tg.is_bot:  # сообщение от бота (кнопка под сообщением бота) — берём чат
        tg_id = message.chat.id
    else:
        tg_id = tg.id
    async with session_scope() as s:
        from services.users import get_user_by_tg

        user = await get_user_by_tg(s, tg_id)
        if user and user.onboarding_step != "done":
            user.onboarding_step = "done"
            user.onboarding_done_at = now()
            await log_event(s, "onboarding_done", user.id)
        uid = user.id if user else None
    await message.answer(texts.TRIAL_DONE)
    if uid:
        await after_onboarding(message.bot, message.chat.id, uid, data.get("inviter_name"))


async def after_onboarding(bot: Bot, chat_id: int, user_id: int, inviter_name: str | None = None) -> None:
    """После онбординга: книга (личный забег стартует в любой день) и бесплатный спринт как проба."""
    s_ = get_settings()
    async with session_scope() as s:
        user = await s.get(User, user_id)
        sprint_ok = not await sprint_block(s, user.id) and (bool(user.invited_by_id) or s_.sprint_for_everyone)
    await send_book_prompt(bot, chat_id)
    if sprint_ok:
        await bot.send_message(chat_id, texts.sprint_offer(inviter_name),
                               reply_markup=kb([[{"text": "Начать спринт (бесплатно)", "callback": "sprint:start"}]]))


async def send_book_prompt(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id, texts.add_book_prompt(),
        reply_markup=kb([[{"text": "У меня бумажная книга", "callback": "book:paper"}],
                         [{"text": "Добавить в приложении", "webapp": "book"}]]),
    )


@router.callback_query(F.data == "sprint:start")
async def cb_sprint(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        block = await sprint_block(s, user.id)
        enr = await start_sprint(s, user)
    if enr is None:
        await call.message.answer(texts.SPRINT_BUSY if block == "running" else texts.SPRINT_USED)
        return
    await call.message.answer(texts.SPRINT_STARTED)
    await send_book_prompt(bot, call.message.chat.id)


# --------------------------------------------------------------------------- статус


async def send_status(bot: Bot, chat_id: int, user_id: int) -> None:
    async with session_scope() as s:
        user = await s.get(User, user_id)
        from services.billing import activate_pending

        await activate_pending(s, user)  # оплачено заранее (кредит, абонемент) — стартуем ждущий план
        enr = await current_enrollment(s, user.id)
        view = await load_view(s, user, enr)
    st = view.state
    if st == "awaiting_payment":
        from bot.handlers_pay import send_paywall

        await send_paywall(bot, chat_id, user_id)
        return
    if st in ("no_run", "no_book"):
        await send_book_prompt(bot, chat_id)
        return
    elif st == "waiting_start":
        text = texts.status_in_list(view.starts_on)
    elif st == "not_started":
        text = texts.status_in_list(view.starts_on) + "\nПлан готов, первый отрезок пришлю утром в день старта."
    elif st in ("to_read", "clarify", "checking", "done_today"):
        from bot.handlers_day import today_text

        text = today_text(view)
    else:
        text = texts.state_message(st, start=view.starts_on)
    await bot.send_message(chat_id, text, reply_markup=app_kb())
