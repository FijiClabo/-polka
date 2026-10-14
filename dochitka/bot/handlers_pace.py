"""Свой темп в чате: /pace, «Прочитано не всё», «Перескажу ещё один», пауза."""

from __future__ import annotations

from datetime import date, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import texts
from bot.common import load_user
from bot.handlers_day import seg_text
from bot.ui import kb
from db.session import session_scope
from services.pace import PaceError, ask_more, pace_info, partial_options, pause, replan, resume, split_today
from services.progress import load_view
from services.runs import current_enrollment

router = Router(name="pace")


# --------------------------------------------------------------------------- /pace


def pace_kb(info: dict) -> object:
    rows = []
    for o in info.get("minute_options", []):
        rows.append([{"text": f"{o['minutes']} мин в день · финиш {texts.d(date.fromisoformat(o['finish']))}",
                      "callback": f"pace:m:{o['minutes']}"}])
    ch = info.get("chapters")
    if ch:
        rows.append([{"text": f"Глава в день · финиш {texts.d(date.fromisoformat(ch['finish']))}", "callback": "pace:ch"}])
    pauses = info.get("pause_options", [])
    if pauses:
        rows.append([{"text": f"Пауза {texts.days_word(o['days'])}", "callback": f"pace:p:{o['days']}"} for o in pauses])
    if info.get("paused_until"):
        rows.append([{"text": "Закончить паузу и читать", "callback": "pace:resume"}])
    rows.append([{"text": "Настроить в приложении", "webapp": "pace"}])
    return kb(rows)


async def _send_pace(bot: Bot, chat_id: int, tg_user) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, tg_user)
        enr = await current_enrollment(s, user.id)
        try:
            info = await pace_info(s, user, enr)
        except PaceError as e:
            await bot.send_message(chat_id, texts.e(str(e)))
            return
    await bot.send_message(chat_id, texts.pace_text(info), reply_markup=pace_kb(info) if info.get("left") else None)


@router.message(Command("pace"))
async def cmd_pace(message: Message, bot: Bot) -> None:
    await _send_pace(bot, message.chat.id, message.from_user)


@router.callback_query(F.data == "pace:menu")
async def cb_pace_menu(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await _send_pace(bot, call.message.chat.id, call.from_user)


@router.callback_query(F.data.startswith("pace:m:") | (F.data == "pace:ch") | F.data.startswith("pace:p:"))
async def cb_pace_set(call: CallbackQuery) -> None:
    from bot.handlers_start import consent_gate

    await call.answer()
    if not await consent_gate(call.message, call.from_user):
        return
    parts = call.data.split(":")
    try:
        async with session_scope() as s:
            user, _ = await load_user(s, call.from_user)
            enr = await current_enrollment(s, user.id)
            if parts[1] == "p":
                days = int(parts[2])
                finish = await pause(s, user, enr, days)
                text = texts.paused(days, finish)
            else:
                await replan(s, user, enr, minutes=int(parts[2]) if parts[1] == "m" else None, by_chapter=parts[1] == "ch")
                text = texts.pace_changed(await pace_info(s, user, enr))
    except PaceError as e:  # откат: план остаётся прежним
        await call.message.answer(f"Не получилось: {texts.e(str(e))}")
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(text, reply_markup=kb([[{"text": "Отрезок на сегодня", "callback": "day:show"}]]))


@router.callback_query(F.data == "pace:resume")
async def cb_pace_resume(call: CallbackQuery) -> None:
    await call.answer()
    try:
        async with session_scope() as s:
            user, _ = await load_user(s, call.from_user)
            finish = await resume(s, user, await current_enrollment(s, user.id))
    except PaceError as e:
        await call.message.answer(texts.e(str(e)))
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(texts.resumed(finish), reply_markup=kb([[{"text": "Отрезок на сегодня", "callback": "day:show"}]]))


@router.callback_query(F.data == "day:show")
async def cb_day_show(call: CallbackQuery) -> None:
    from bot.handlers_day import today_kb, today_text

    await call.answer()
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        v = await load_view(s, user, await current_enrollment(s, user.id))
        text, markup = today_text(v), today_kb(v)
    await call.message.answer(text, reply_markup=markup)


# --------------------------------------------------------------------------- прочитано не всё


@router.callback_query(F.data == "part:menu")
async def cb_partial_menu(call: CallbackQuery) -> None:
    await call.answer()
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        v = await load_view(s, user, await current_enrollment(s, user.id))
        if v.state != "to_read" or v.extra or v.segment is None:
            await call.message.answer("Сейчас делить нечего: сегодняшний отрезок уже сдан или ещё не открыт.")
            return
        opts = partial_options(v.segment, v.book.source == "paper")
    if not opts:
        await call.message.answer("Этот отрезок слишком короткий, чтобы делить его на части.")
        return
    await call.message.answer(texts.PARTIAL_ASK, reply_markup=kb(
        [[{"text": o["label"].capitalize() if not o.get("page") else o["label"], "callback": f"part:q:{o['quarter']}"}]
         for o in opts]
    ))


@router.callback_query(F.data.startswith("part:q:"))
async def cb_partial_quarter(call: CallbackQuery) -> None:
    await call.answer()
    q = int(call.data.split(":")[2])
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        enr = await current_enrollment(s, user.id)
        v = await load_view(s, user, enr)
        if v.state != "to_read" or v.extra or v.segment is None:
            await call.message.answer("Сегодняшний отрезок уже сдан — делить нечего.")
            return
        has_next = v.segment.day_number < enr.plan_days
    rows = []
    if has_next:
        rows.append([{"text": "Остаток — к следующему отрезку", "callback": f"part:{q}:t"}])
    rows.append([{"text": "Остаток — отдельным днём", "callback": f"part:{q}:d"}])
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(texts.partial_rest_ask(has_next), reply_markup=kb(rows))


@router.callback_query(F.data.regexp(r"^part:[123]:[td]$"))
async def cb_partial_apply(call: CallbackQuery) -> None:
    from bot.handlers_start import consent_gate

    await call.answer()
    if not await consent_gate(call.message, call.from_user):
        return
    _, q, r = call.data.split(":")
    try:
        async with session_scope() as s:
            user, _ = await load_user(s, call.from_user)
            enr = await current_enrollment(s, user.id)
            p = await split_today(s, user, enr, quarter=int(q), rest="tomorrow" if r == "t" else "day")
            v = await load_view(s, user, enr)
            paper = v.book.source == "paper"
            finish = enr.plan_start_date + timedelta(days=enr.plan_days - 1) if enr.plan_start_date else None
            text = texts.partial_done(seg_text(p.segment, v.book), p.rest, finish, paper)
            rows = []
            if v.book.has_text and p.segment.text:
                rows.append([{"text": "Читать отрезок", "webapp": f"read?d={p.segment.day_number}"}])
            rows.append([{"text": "Открыть приложение", "webapp": "today"}])
    except PaceError as e:
        await call.message.answer(f"Не получилось: {texts.e(str(e))}")
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(text, reply_markup=kb(rows))


# --------------------------------------------------------------------------- прочитано больше


async def _open_more(call: CallbackQuery) -> tuple[bool, str, object | None]:
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        enr = await current_enrollment(s, user.id)
        seg = await ask_more(s, user, enr) if enr else None
        if seg is None:
            return False, texts.MORE_CLOSED, None
        v = await load_view(s, user, enr)
        paper = v.book.source == "paper"
        rows = []
        if v.book.has_text and seg.text:
            rows.append([{"text": "Читать отрезок", "webapp": f"read?d={seg.day_number}"}])
        return True, texts.more_opened(seg_text(seg, v.book), paper), kb(rows) if rows else None


@router.callback_query(F.data == "more:on")
async def cb_more(call: CallbackQuery) -> None:
    await call.answer()
    _, text, markup = await _open_more(call)
    await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "more:yes")
async def cb_more_yes(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    from bot.handlers_day import _retell

    await call.answer()
    data = await state.get_data()
    await state.update_data(more_text=None, more_source=None, more_voice=None, more_duration=None)
    await call.message.edit_reply_markup(reply_markup=None)
    ok, text, markup = await _open_more(call)
    stored = data.get("more_text")
    if not ok or not stored:
        await call.message.answer(text if not ok else "Пришли пересказ ещё раз — засчитаю заранее.", reply_markup=markup)
        return
    await _retell(call.message, bot, stored, source=data.get("more_source") or "text", voice_id=data.get("more_voice"),
                  duration=data.get("more_duration"), tg_user=call.from_user)


@router.callback_query(F.data == "more:no")
async def cb_more_no(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.update_data(more_text=None, more_source=None, more_voice=None, more_duration=None)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Хорошо. До завтра.")
