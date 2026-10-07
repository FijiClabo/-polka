"""Сборка бота: роутеры, команды, кнопка меню, обработка ошибок."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ErrorEvent,
    MenuButtonWebApp,
    WebAppInfo,
)

from bot import (
    handlers_admin,
    handlers_books,
    handlers_day,
    handlers_misc,
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
    BotCommand(command="pair", description="Напарник"),
    BotCommand(command="friends", description="Друзья и личная ссылка"),
    BotCommand(command="settings", description="Время и часовой пояс"),
    BotCommand(command="buy", description="Тарифы и оплата"),
    BotCommand(command="help", description="Как это работает"),
    BotCommand(command="terms", description="Условия и оферта"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
    BotCommand(command="delete_me", description="Отозвать согласие и удалить данные"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="admin", description="Команды ведущего"),
    BotCommand(command="stats", description="Сводка по забегу"),
    BotCommand(command="user", description="Участник: статус и пересказы"),
    BotCommand(command="grant", description="Выдать доступ вручную"),
    BotCommand(command="sales", description="Продажи и воронка"),
    BotCommand(command="promos", description="Промокоды"),
    BotCommand(command="export", description="Выгрузка CSV"),
]

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
        handlers_social.router, handlers_books.router,
        handlers_day.router,  # последним: ловит любой текст как пересказ
    ]
    for r in routers:
        r._parent_router = None  # повторная сборка (тесты) — отвязываем от прошлого диспетчера
        dp.include_router(r)
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
        await bot.set_my_short_description(f"{s.project_name}: дочитай книгу за 30 дней. 15 минут чтения и минута пересказа в день.")
    except Exception as e:
        log.warning("bot setup partially failed: %s", e)
