"""Авторизация мини-приложения: проверка подписи initData от Telegram на каждом запросе."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from db.session import sessionmaker
from services.users import get_or_create_user
from settings import get_settings

# подпись initData живёт сутки: утёкшая строка (скриншот, лог) перестаёт работать на следующий день,
# а кто держит приложение открытым дольше — просто откроет его заново
MAX_AGE_SEC = 24 * 3600
CLOCK_SKEW_SEC = 300


class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SEC, now: float | None = None) -> dict:
    """Проверка по документации Telegram: HMAC-SHA256 с ключом HMAC("WebAppData", bot_token)."""
    if not bot_token:
        raise InitDataError("no bot token")  # с пустым токеном подпись может подделать кто угодно
    if not init_data:
        raise InitDataError("empty")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("no hash")
    # поле signature (Ed25519, Bot API 8.0) остаётся в строке проверки — так требует документация
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise InitDataError("bad hash")
    try:
        auth_date = int(pairs.get("auth_date", "0") or 0)
    except ValueError as e:
        raise InitDataError("bad auth_date") from e
    now = now or time.time()
    if auth_date > now + CLOCK_SKEW_SEC:
        raise InitDataError("from the future")
    if max_age and now - auth_date > max_age:
        raise InitDataError("expired")
    try:
        user = json.loads(pairs.get("user", "{}") or "{}")
    except ValueError as e:
        raise InitDataError("bad user") from e
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("no user")
    pairs["user"] = user
    return pairs


def dev_bypass_allowed(s) -> bool:
    """DEV_AUTH_BYPASS работает только на своём компьютере: без HTTPS-адреса и без вебхука."""
    return not s.public_url.startswith("https://") and s.bot_mode != "webhook"


async def get_session():
    async with sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def current_user(
    authorization: str = Header(default=""), session: AsyncSession = Depends(get_session)
) -> User:
    s = get_settings()
    raw = authorization[4:] if authorization.lower().startswith("tma ") else authorization
    if s.dev_auth_bypass and dev_bypass_allowed(s) and raw.startswith("dev:"):
        tg_user = {"id": int(raw[4:] or 1) if raw[4:].isdigit() else 1}  # только локально: профиль не перезаписываем
    else:
        try:
            data = validate_init_data(raw, s.bot_token)
        except InitDataError as e:
            raise HTTPException(status_code=401, detail="Нужно открыть приложение из Telegram") from e
        tg_user = data["user"]
    user, _ = await get_or_create_user(
        session, int(tg_user["id"]), first_name=tg_user.get("first_name"), last_name=tg_user.get("last_name"),
        username=tg_user.get("username"), language_code=tg_user.get("language_code"), photo_url=tg_user.get("photo_url"),
    )
    return user
