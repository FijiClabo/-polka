"""Сборка бота: роутеры, команды, кнопка меню, обработка ошибок."""

from __future__ import annotations

import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    ErrorEvent,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from bot import (
    handlers_admin,
    handlers_books,
    handlers_day,
    handlers_feedback,
    handlers_misc,
    handlers_pace,
    handlers_pay,
    handlers_social,
    handlers_start,
)
from bot.ui import set_bot_username, webapp_url
from settings import get_settings

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="today", description="Отрезок на сегодня"),
    BotCommand(command="book", description="Моя книга"),
    BotCommand(command="pace", description="Свой темп: минуты в день, пауза"),
    BotCommand(command="pair", description="Напарник и «читаем вместе»"),
    BotCommand(command="club", description="Книжный клуб"),
    BotCommand(command="friends", description="Друзья и личная ссылка"),
    BotCommand(command="settings", description="Время и часовой пояс"),
    BotCommand(command="buy", description="Тарифы и оплата"),
    BotCommand(command="help", description="Как это работает"),
    BotCommand(command="feedback", description="Написать отзыв"),
    BotCommand(command="terms", description="Условия и оферта"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
    BotCommand(command="delete_me", description="Отозвать согласие и удалить данные"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="admin", description="Команды ведущего"),
    BotCommand(command="stats", description="Сводка по забегу"),
    BotCommand(command="user", description="Участник: статус и вердикты"),
    BotCommand(command="grant", description="Выдать доступ вручную"),
    BotCommand(command="sales", description="Продажи и воронка"),
    BotCommand(command="promos", description="Промокоды"),
    BotCommand(command="export", description="Выгрузка CSV"),
    BotCommand(command="report", description="Отчёт для разбора: поведение и отзывы"),
]

class UsageMiddleware(BaseMiddleware):
    """Какие команды и кнопки бота нажимают — для разбора, чем пользуются, а чем нет. Тексты сообщений не пишем."""

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        try:
            await log_usage(event)
        except Exception as e:  # учёт не должен ломать ответ
            log.debug("usage log failed: %s", e)
        return result


async def log_usage(event) -> None:
    from sqlalchemy import select

    from db.models import User
    from db.session import session_scope
    from services.common import log_event

    if isinstance(event, Message):
        text = event.text or ""
        if not text.startswith("/"):
            return
        kind, value = "bot_command", text.split()[0][1:].split("@")[0][:32]
    elif isinstance(event, CallbackQuery):
        parts = (event.data or "").split(":")
        value = parts[0] + (f":{parts[1]}" if len(parts) > 2 and parts[1].isalpha() else "")
        kind, value = "bot_button", value[:32]
    else:
        return
    if not event.from_user:
        return
    async with session_scope() as s:
        uid = await s.scalar(select(User.id).where(User.tg_id == event.from_user.id))
        if uid:
            await log_event(s, kind, uid, name=value)


errors_router = Router(name="errors")


@errors_router.error()
async def on_error(event: ErrorEvent) -> bool:
    log.exception("handler error: %s", event.exception, exc_info=event.exception)
    upd = event.update
    chat_id = None
    if upd.message:
        chat_id = upd.message.chat.id
    elif upd.callback_query and upd.callback_query.message:
        chat_id = upd.callback_query.message.chat.id
    if chat_id:
        try:
            await event.update.bot.send_message(chat_id, "Что-то пошло не так — уже разбираюсь. Попробуй ещё раз чуть позже.")
        except Exception:
            pass
    return True


def create_bot() -> Bot:
    s = get_settings()
    session = None
    if s.telegram_api_base:
        # свой адрес Bot API: например, прокси к api.telegram.org, если сервер с данными стоит в РФ
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        session = AiohttpSession(api=TelegramAPIServer.from_base(s.telegram_api_base))
    return Bot(token=s.bot_token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    routers = [
        errors_router, handlers_admin.router, handlers_pay.router, handlers_start.router, handlers_misc.router,
        handlers_social.router, handlers_books.router, handlers_pace.router, handlers_feedback.router,
        handlers_day.router,  # последним: ловит любой текст как пересказ
    ]
    for r in routers:
        r._parent_router = None  # повторная сборка (тесты) — отвязываем от прошлого диспетчера
        dp.include_router(r)
    # бот работает только в личке: в группе команда админа или /report показали бы данные всем участникам
    dp.message.filter(F.chat.type == ChatType.PRIVATE)
    dp.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    dp.message.middleware(UsageMiddleware())
    dp.callback_query.middleware(UsageMiddleware())
    return dp


async def setup_bot(bot: Bot) -> None:
    s = get_settings()
    me = await bot.get_me()
    set_bot_username(me.username or "")
    log.info("bot @%s ready", me.username)
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        for admin in s.admin_ids:
            try:
                await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin))
            except Exception:
                pass  # админ ещё не писал боту
        url = webapp_url("today")
        if url:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text=s.project_name, web_app=WebAppInfo(url=url)))
        await bot.set_my_short_description(f"{s.project_name}: дочитай свою книгу по плану на 21–60 дней. 15 минут чтения и минута пересказа в день.")
    except Exception as e:
        log.warning("bot setup partially failed: %s", e)
