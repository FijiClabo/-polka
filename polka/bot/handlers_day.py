"""Ежедневный цикл в чате: /today, пересказ текстом и голосом."""

from __future__ import annotations

import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import texts
from ai.stt import STTError, transcribe_audio
from books.plan import segment_minutes
from bot.common import Flow, load_user
from bot.ui import app_kb, flush, kb
from db.session import session_scope
from services.common import Outbox
from services.flow import submit_retelling, verdict_text
from services.progress import DayView, load_view
from services.retell import RetellOutcome
from services.runs import current_enrollment
from settings import get_settings

log = logging.getLogger(__name__)
router = Router(name="day")


def today_text(v: DayView) -> str:
    seg = v.segment if v.state in ("to_read", "clarify", "checking") else v.next_segment
    if v.state == "done_today":
        return texts.state_message("done_today", next_title=seg.title if seg else None)
    if seg is None:
        return texts.state_message(v.state, start=v.starts_on)
    paper = v.book is not None and v.book.source == "paper"
    pages = f"стр. {seg.page_from}–{seg.page_to}"
    minutes = segment_minutes(seg.word_count, seg.page_to - seg.page_from + 1, paper)
    head = f"День {v.plan_day} из {v.plan_days}"
    if v.catching_up:
        head += " · догоняем вчерашний отрезок"
    text = f"{head}\n<b>{texts.e(seg.title)}</b>\n{pages} · ≈ {minutes} мин"
    if v.state == "clarify" and v.open_retelling is not None:
        text += f"\n\nЖду ответ на вопрос: <b>{texts.e(v.open_retelling.ai_question or '')}</b>"
    elif v.state == "checking":
        text += "\n\n" + texts.CHECKING
    else:
        text += "\n\nПрочитаешь — перескажи мне голосом или текстом."
    return text


def today_kb(v: DayView):
    seg = v.segment
    rows = []
    if seg is not None and v.book is not None and v.book.has_text and v.state in ("to_read", "clarify"):
        rows.append([{"text": "Читать отрезок", "webapp": f"read?d={seg.day_number}"}])
    rows.append([{"text": "Открыть приложение", "webapp": "today"}])
    return kb(rows)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        enr = await current_enrollment(s, user.id)
        v = await load_view(s, user, enr)
        text, markup = today_text(v), today_kb(v)
    await message.answer(text, reply_markup=markup)


async def reply_outcome(message: Message, out: RetellOutcome, bot: Bot, user_id: int) -> None:
    if out.status == "awaiting_payment":
        from bot.handlers_pay import send_paywall

        await send_paywall(bot, message.chat.id, user_id)
        return
    if out.status == "no_run":
        from bot.handlers_start import send_book_prompt

        await send_book_prompt(bot, message.chat.id)
        return
    text = verdict_text(out)
    markup = None
    if out.status == "accepted" and not out.finished:
        markup = app_kb("Открыть приложение")
    elif out.status in ("rejected",):
        markup = None
    await message.answer(text, reply_markup=markup)
    if out.finished:
        from bot.handlers_social import send_finish

        await send_finish(bot, message.chat.id, user_id)


async def _retell(message: Message, bot: Bot, text: str, *, source: str, voice_id: str | None = None,
                  duration: int | None = None) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        uid = user.id
    await bot.send_chat_action(message.chat.id, "typing")
    outbox = Outbox()
    out = await submit_retelling(uid, text, source=source, via="bot", voice_file_id=voice_id,
                                 voice_duration=duration, outbox=outbox)
    if out.status == "too_short" and source == "text":
        await message.answer(texts.too_short_hint(out.segment_title))
    else:
        await reply_outcome(message, out, bot, uid)
    await flush(bot, outbox)


@router.message(StateFilter(None), F.voice | F.audio | F.video_note)
async def msg_voice(message: Message, bot: Bot, state: FSMContext) -> None:
    await handle_voice(message, bot, state)


@router.message(Flow.trial, F.voice | F.audio | F.video_note)
async def msg_voice_trial(message: Message, bot: Bot, state: FSMContext) -> None:
    await handle_voice(message, bot, state, trial=True)


async def handle_voice(message: Message, bot: Bot, state: FSMContext, trial: bool = False) -> None:
    media = message.voice or message.audio or message.video_note
    duration = int(getattr(media, "duration", 0) or 0)
    if duration > get_settings().voice_max_sec:
        await message.answer(texts.VOICE_TOO_LONG)
        return
    await bot.send_chat_action(message.chat.id, "typing")
    buf = io.BytesIO()
    try:
        await bot.download(media, destination=buf)
        text, _dur = await transcribe_audio(buf.getvalue())
    except STTError as e:
        log.warning("STT failed: %s", e)
        await message.answer(texts.STT_OFF if "Не настроен" in str(e) else texts.VOICE_FAILED)
        return
    finally:
        buf.close()  # голосовые не храним
    if not text.strip():
        await message.answer(texts.VOICE_FAILED)
        return
    await message.answer(texts.heard(text))
    if trial:
        from bot.handlers_start import trial_answer

        await trial_answer(message, state, text)
        return
    await _retell(message, bot, text, source="voice", voice_id=media.file_id, duration=duration)


@router.message(Flow.trial, F.text)
async def msg_trial_text(message: Message, state: FSMContext) -> None:
    from bot.handlers_start import trial_answer

    if message.text.startswith("/"):
        return
    await trial_answer(message, state, message.text)


@router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def msg_text(message: Message, bot: Bot) -> None:
    """Любой текст длиннее 50 знаков в активном забеге — попытка сдачи; ответ на уточнение — любой длины."""
    await _retell(message, bot, message.text, source="text")
