"""Добавление книги: файл epub/fb2, бумажная книга, выбор срока."""

from __future__ import annotations

import asyncio
import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import texts
from books.parse import detect_format, file_hash, is_other_book_format
from books.plan import PlanError
from books.types import BookParseError
from bot.common import Flow, load_user
from bot.ui import flush, kb
from db.models import Book, User
from db.session import session_scope
from services.books import (
    add_paper_book,
    attach_book,
    confirm_plan,
    create_pending_book,
    has_progress,
    mark_failed,
    options_for,
    parse_file,
    save_parsed,
    store_upload,
)
from services.common import Outbox, log_event
from services.runs import current_enrollment, ensure_enrollment
from settings import get_settings

log = logging.getLogger(__name__)
router = Router(name="books")

PARSE_TIMEOUT = 120


async def _enrollment_for_book(s, user: User):
    """Текущее участие или новое: групповой забег ведущего, иначе личный забег (стартует в любой день)."""
    return await ensure_enrollment(s, user)


@router.message(F.document)
async def msg_document(message: Message, bot: Bot, state: FSMContext) -> None:
    doc = message.document
    name = doc.file_name or "book"
    fmt = detect_format(name)
    if fmt is None:
        text = texts.BOOK_UNSUPPORTED if is_other_book_format(name) else texts.BOOK_NOT_A_BOOK
        await message.answer(text, reply_markup=kb([[{"text": "📖 Добавить как бумажную", "callback": "book:paper"}]]))
        return
    max_mb = get_settings().max_book_mb
    if (doc.file_size or 0) > max_mb * 1024 * 1024:
        await message.answer(texts.BOOK_TOO_BIG.format(mb=max_mb),
                             reply_markup=kb([[{"text": "📖 Добавить как бумажную", "callback": "book:paper"}]]))
        return
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        enr = await _enrollment_for_book(s, user)
        progress = await has_progress(s, enr)
        cur_title = (await s.get(Book, enr.book_id)).title if enr.book_id else ""
    if progress:
        await state.update_data(pending_file_id=doc.file_id, pending_file_name=name, pending_size=doc.file_size)
        await message.answer(texts.book_replace_confirm(cur_title), reply_markup=kb([[
            {"text": "Заменить", "callback": "book:replace:yes"}, {"text": "Оставить", "callback": "book:replace:no"},
        ]]))
        return
    await process_upload(message, bot, doc.file_id, name, doc.file_size or 0)


@router.callback_query(F.data.startswith("book:replace:"))
async def cb_replace(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    if call.data.endswith(":no") or not data.get("pending_file_id"):
        await call.message.answer("Оставляем текущую книгу.")
        return
    await state.update_data(pending_file_id=None)
    await process_upload(call.message, bot, data["pending_file_id"], data["pending_file_name"], data.get("pending_size") or 0,
                         tg_user=call.from_user)


async def process_upload(message: Message, bot: Bot, file_id: str, name: str, size: int, tg_user=None) -> None:
    tg_user = tg_user or message.from_user
    await message.answer(texts.BOOK_RECEIVED)
    buf = io.BytesIO()
    try:
        await bot.download(file_id, destination=buf)
    except Exception as e:
        log.warning("download failed: %s", e)
        await message.answer(texts.BOOK_TOO_BIG.format(mb=get_settings().max_book_mb))
        return
    data = buf.getvalue()
    async with session_scope() as s:
        user, _ = await load_user(s, tg_user)
        book = await create_pending_book(s, user, name, len(data))
        book.file_path = store_upload(data, ".fb2.zip" if name.lower().endswith(".zip") else "." + name.rsplit(".", 1)[-1])
        await log_event(s, "book_uploaded", user.id, format=detect_format(name), size=len(data))
        book_id, uid = book.id, user.id
    await handle_parse(bot, message.chat.id, uid, book_id, name, data)


async def handle_parse(bot: Bot, chat_id: int, user_id: int, book_id: int, name: str, data: bytes) -> None:
    """Разбор в отдельном потоке. Общий для бота и загрузки из мини-приложения."""
    try:
        parsed = await asyncio.wait_for(asyncio.to_thread(parse_file, name, data), timeout=PARSE_TIMEOUT)
        err = None
    except BookParseError as e:
        parsed, err = None, e
    except TimeoutError:
        parsed, err = None, BookParseError("broken", "timeout")
    except Exception as e:  # неожиданное — тоже честно говорим, что не вышло
        log.exception("parse crashed")
        parsed, err = None, BookParseError("broken", str(e))
    async with session_scope() as s:
        user = await s.get(User, user_id)
        book = await s.get(Book, book_id)
        if parsed is not None:
            await save_parsed(s, book, parsed, file_hash(data))
            enr = await _enrollment_for_book(s, user)
            if enr is not None:
                await attach_book(s, enr, book)
                opts = options_for(book, enr)
            else:
                opts = []
            text = texts.book_parsed(book.title, book.author, book.total_pages, book.chapters_count,
                                     parsed.reading_minutes)
        else:
            await mark_failed(s, book, err)
            text = texts.book_failed(err.human)
            opts = None
    if parsed is not None:
        await bot.send_message(chat_id, text, reply_markup=plan_kb(opts))
    else:
        await bot.send_message(chat_id, text, reply_markup=kb([[{"text": "📖 Добавить как бумажную", "callback": "book:paper"}]]))


def plan_kb(opts) -> object:
    rows = []
    for o in opts or []:
        star = " ⭐" if o.recommended else ""
        ppd = f"{o.pages_per_day:g}".replace(".", ",")
        rows.append([{"text": f"{texts.days_word(o.days)} · {ppd} стр. · {o.minutes_per_day} мин{star}",
                      "callback": f"plan:{o.days}"}])
    rows.append([{"text": "Настроить план в приложении", "webapp": "book"}])
    return kb(rows)


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    days = int(call.data.split(":")[1])
    outbox = Outbox()
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        enr = await current_enrollment(s, user.id)
        if enr is None or enr.book_id is None:
            await call.message.answer("Сначала добавь книгу.")
            return
        try:
            await confirm_plan(s, user, enr, days)
        except PlanError as e:
            await call.message.answer(f"Не получилось: {texts.e(str(e))}")
            return
        book = await s.get(Book, enr.book_id)
        opt = next((o for o in options_for(book, enr) if o.days == days), None)
        text = texts.plan_ready(book.title, days, enr.plan_start_date, opt.pages_per_day if opt else 0,
                                opt.minutes_per_day if opt else 0, run_start=enr.run.start_date,
                                awaiting_payment=enr.status == "invited")
        uid = user.id
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(text)
    from bot.handlers_start import send_status

    await send_status(bot, call.message.chat.id, uid)
    await flush(bot, outbox)


# --------------------------------------------------------------------------- бумажная книга


@router.callback_query(F.data == "book:paper")
async def cb_paper(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Flow.paper_title)
    await call.message.answer(texts.PAPER_ASK_TITLE)


@router.message(Command("paper"))
async def cmd_paper(message: Message, state: FSMContext) -> None:
    await state.set_state(Flow.paper_title)
    await message.answer(texts.PAPER_ASK_TITLE)


@router.message(Flow.paper_title, F.text)
async def msg_paper_title(message: Message, state: FSMContext) -> None:
    await state.update_data(paper_title=message.text.strip()[:300])
    await state.set_state(Flow.paper_author)
    await message.answer(texts.PAPER_ASK_AUTHOR)


@router.message(Flow.paper_author, F.text)
async def msg_paper_author(message: Message, state: FSMContext) -> None:
    author = message.text.strip()
    await state.update_data(paper_author="" if author == "-" else author[:300])
    await state.set_state(Flow.paper_pages)
    await message.answer(texts.PAPER_ASK_PAGES)


@router.message(Flow.paper_pages, F.text)
async def msg_paper_pages(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "")
    if not raw.isdigit() or not 20 <= int(raw) <= 3000:
        await message.answer(texts.PAPER_BAD_PAGES)
        return
    data = await state.get_data()
    await state.clear()
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        enr = await _enrollment_for_book(s, user)
        book = await add_paper_book(s, user, enr, data.get("paper_title", "Книга"), data.get("paper_author", ""), int(raw))
        opts = options_for(book, enr)
        title = book.title
    await message.answer(f"Записано: «{texts.e(title)}», {raw} стр. Выбери срок:", reply_markup=plan_kb(opts))
