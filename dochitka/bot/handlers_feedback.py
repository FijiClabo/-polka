"""Опросы и отзывы в чате: ответ кнопкой, пара слов текстом или голосом, /feedback в любой момент."""

from __future__ import annotations

import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import texts
from ai.stt import STTError, transcribe_audio
from bot.common import Flow, load_user
from bot.ui import flush, kb
from db.session import session_scope
from services.common import Outbox
from services.feedback import SKIP_TEXT, SURVEYS, add_text, answer

log = logging.getLogger(__name__)
router = Router(name="feedback")

SKIP_KB = [[{"text": SKIP_TEXT, "callback": "fb:skip"}]]
DROP_TIPS = {"time": texts.FEEDBACK_DROP_TIME, "retell": texts.FEEDBACK_DROP_RETELL,
             "forget": texts.FEEDBACK_DROP_FORGET, "book": texts.FEEDBACK_DROP_BOOK}


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext) -> None:
    await state.set_state(Flow.feedback)
    await state.update_data(fb_kind="free", fb_id=None)
    await message.answer(texts.FEEDBACK_ASK, reply_markup=kb(SKIP_KB))


@router.callback_query(F.data.regexp(r"^fb:(day3|pmf|finish|drop):[a-z0-9]+$"))
async def cb_survey(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("Спасибо!")
    _, kind, value = call.data.split(":")
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        fb = await answer(s, user, kind, value)
        fb_id = fb.id if fb else None
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if fb_id is None:
        return
    tip = DROP_TIPS.get(value) if kind == "drop" else None
    if tip:
        await call.message.answer(tip)
    await state.set_state(Flow.feedback)
    await state.update_data(fb_kind=kind, fb_id=fb_id)
    await call.message.answer(SURVEYS[kind].follow_up, reply_markup=kb(SKIP_KB))


@router.callback_query(F.data == "fb:skip")
async def cb_skip(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(texts.FEEDBACK_SKIPPED)


async def _save(message: Message, bot: Bot, state: FSMContext, text: str) -> None:
    data = await state.get_data()
    await state.clear()
    out = Outbox()
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        await add_text(s, user, data.get("fb_kind") or "free", text, data.get("fb_id"), out)
    await message.answer(texts.FEEDBACK_THANKS)
    await flush(bot, out)


@router.message(Flow.feedback, F.text)
async def msg_feedback_text(message: Message, bot: Bot, state: FSMContext) -> None:
    if message.text.startswith("/"):
        await state.clear()
        await message.answer("Отзыв отложен. Команду можно повторить ещё раз.")
        return
    await _save(message, bot, state, message.text)


@router.message(Flow.feedback, F.voice | F.audio)
async def msg_feedback_voice(message: Message, bot: Bot, state: FSMContext) -> None:
    media = message.voice or message.audio
    buf = io.BytesIO()
    try:
        await bot.download(media, destination=buf)
        text, _ = await transcribe_audio(buf.getvalue())
    except STTError as e:
        log.warning("STT failed for feedback: %s", e)
        await message.answer("Не получилось разобрать голосовое — напиши, пожалуйста, текстом.")
        return
    finally:
        buf.close()  # голос не храним, только текст отзыва
    if not text.strip():
        await message.answer("Не получилось разобрать голосовое — напиши, пожалуйста, текстом.")
        return
    await _save(message, bot, state, text)
