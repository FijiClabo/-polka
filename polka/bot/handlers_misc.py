"""/help, /settings, /book, /delete_me."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import texts
from bot.common import load_user, tz_label
from bot.ui import app_kb, kb
from db.models import Book
from db.session import session_scope
from services.runs import current_enrollment
from services.users import delete_user_data, get_user_by_tg

router = Router(name="misc")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.help_text(), reply_markup=app_kb())


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        text = (
            "<b>Настройки</b>\n"
            f"Пояс: {texts.e(tz_label(user.timezone))}\n"
            f"Утренний отрезок: {user.morning_time.strftime('%H:%M')}\n"
            f"Вечернее напоминание: {user.evening_time.strftime('%H:%M')}\n"
            f"Толчки от друзей: {'включены' if user.nudges_enabled else 'выключены'}"
        )
        nudges = user.nudges_enabled
    await message.answer(text, reply_markup=kb([
        [{"text": "Пояс", "callback": "cfg:tz"}, {"text": "Утро", "callback": "cfg:morning"},
         {"text": "Вечер", "callback": "cfg:evening"}],
        [{"text": "Выключить толчки" if nudges else "Включить толчки", "callback": "cfg:nudges"}],
        [{"text": "Все настройки в приложении", "webapp": "profile"}],
    ]))


@router.callback_query(F.data.startswith("cfg:"))
async def cb_settings(call: CallbackQuery) -> None:
    await call.answer()
    what = call.data.split(":", 1)[1]
    from bot.handlers_start import ask_evening, ask_morning, ask_tz

    if what == "tz":
        await ask_tz(call.message, settings_mode=True)
    elif what == "morning":
        await ask_morning(call.message, settings_mode=True)
    elif what == "evening":
        await ask_evening(call.message, settings_mode=True)
    elif what == "nudges":
        async with session_scope() as s:
            u = await get_user_by_tg(s, call.from_user.id)
            u.nudges_enabled = not u.nudges_enabled
            on = u.nudges_enabled
        await call.message.answer("Толчки включены." if on else "Толчки выключены — никто не будет тебя торопить.")


@router.message(Command("book"))
async def cmd_book(message: Message) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        enr = await current_enrollment(s, user.id)
        book = await s.get(Book, enr.book_id) if enr and enr.book_id else None
        if book is None:
            text = texts.add_book_prompt()
            markup = kb([[{"text": "📖 У меня бумажная книга", "callback": "book:paper"}],
                         [{"text": "Добавить в приложении", "webapp": "book"}]])
        else:
            kind = {"paper": "бумажная", "epub": "epub", "fb2": "fb2"}.get(book.source, book.source)
            plan = f"план на {enr.plan_days} дн." if enr.plan_days else "план не выбран"
            text = (f"<b>{texts.e(book.title)}</b>{(' — ' + texts.e(book.author)) if book.author else ''}\n"
                    f"{book.total_pages} стр. · {kind} · {plan}\n\n"
                    "Чтобы заменить книгу, просто пришли новый файл.")
            markup = app_kb("Книга и план", "book")
    await message.answer(text, reply_markup=markup)


@router.message(Command("delete_me"))
async def cmd_delete_me(message: Message) -> None:
    from services.billing import subscription_active

    async with session_scope() as s:
        u = await get_user_by_tg(s, message.from_user.id)
        sub_until = u.subscription_until if u and subscription_active(u) else None
        credits = u.run_credits if u else 0
    await message.answer(texts.delete_confirm(sub_until, credits), reply_markup=kb([[
        {"text": "Да, удалить всё", "callback": "del:yes"}, {"text": "Отмена", "callback": "del:no"},
    ]]))


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    if call.data == "del:no":
        await call.message.answer("Ничего не удалено.")
        return
    async with session_scope() as s:
        u = await get_user_by_tg(s, call.from_user.id)
        if u:
            from services.billing import cancel_subscription

            await cancel_subscription(s, u, call.bot)  # автопродление звёзд не должно пережить удаление
            await delete_user_data(s, u)
    await state.clear()
    await call.message.answer(texts.DELETED)
