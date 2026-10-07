"""Кнопки, ссылки на мини-приложение и отправка исходящих сообщений."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy import update

from db.models import User
from db.session import session_scope
from services.common import Outbox, OutMsg
from settings import get_settings

log = logging.getLogger(__name__)

_bot_username: str = ""


def set_bot_username(name: str) -> None:
    global _bot_username
    _bot_username = name


def bot_username() -> str:
    return _bot_username


def webapp_url(screen: str = "today", **params) -> str | None:
    base = get_settings().resolved_webapp_url
    if not base.startswith("https://"):
        return None  # Telegram принимает мини-приложения только по HTTPS
    q = {"s": screen, **{k: v for k, v in params.items() if v is not None}}
    return f"{base}/?{urlencode(q)}"


def deep_link(payload: str) -> str:
    return f"https://t.me/{_bot_username}?start={payload}"


def share_link(url: str, text: str) -> str:
    return "https://t.me/share/url?" + urlencode({"url": url, "text": text})


def button(spec: dict) -> InlineKeyboardButton | None:
    text = spec["text"]
    if "webapp" in spec:
        screen, _, query = spec["webapp"].partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        url = webapp_url(screen, **params)
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url)) if url else None
    if "url" in spec:
        return InlineKeyboardButton(text=text, url=spec["url"])
    if "callback" in spec:
        return InlineKeyboardButton(text=text, callback_data=spec["callback"])
    return None


def kb(rows: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    out = []
    for row in rows:
        btns = [b for b in (button(x) for x in row) if b is not None]
        if btns:
            out.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=out) if out else None


def app_kb(text: str = "Открыть приложение", screen: str = "today", **params) -> InlineKeyboardMarkup | None:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return kb([[{"text": text, "webapp": f"{screen}?{q}" if q else screen}]])


def location_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


REMOVE_KB = ReplyKeyboardRemove()


async def _mark_blocked(tg_id: int) -> None:
    async with session_scope() as s:
        await s.execute(update(User).where(User.tg_id == tg_id).values(bot_blocked=True))


async def send_one(bot: Bot, m: OutMsg) -> bool:
    for attempt in range(3):
        try:
            markup = kb(m.buttons)
            if m.photo:
                await bot.send_photo(m.tg_id, BufferedInputFile(m.photo, "card.png"), caption=m.text[:1024] or None,
                                     reply_markup=markup)
            else:
                await bot.send_message(m.tg_id, m.text, reply_markup=markup, disable_web_page_preview=True)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramForbiddenError:
            await _mark_blocked(m.tg_id)
            return False
        except TelegramBadRequest as e:
            log.warning("send to %s failed: %s", m.tg_id, e)
            if "chat not found" in str(e).lower():
                return False
            if m.buttons and attempt == 0:
                m = OutMsg(m.tg_id, m.text, None, m.photo, m.kind)  # частая причина — кнопка; пробуем без неё
                continue
            return False
        except Exception as e:  # сеть и т.п.
            log.warning("send to %s error: %s", m.tg_id, e)
            await asyncio.sleep(1 + attempt)
    return False


async def flush(bot: Bot, outbox: Outbox) -> None:
    for m in outbox.messages:
        await send_one(bot, m)
        await asyncio.sleep(0.05)
    outbox.messages.clear()
