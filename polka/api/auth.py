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

MAX_AGE_SEC = 7 * 24 * 3600  # мини-приложение может быть открыто долго; данные подписаны Telegram


class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SEC, now: float | None = None) -> dict:
    """Проверка по документации Telegram: HMAC-SHA256 с ключом HMAC("WebAppData", bot_token)."""
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
    auth_date = int(pairs.get("auth_date", "0") or 0)
    if max_age and (now or time.time()) - auth_date > max_age:
        raise InitDataError("expired")
    user = json.loads(pairs.get("user", "{}") or "{}")
    if not user.get("id"):
        raise InitDataError("no user")
    pairs["user"] = user
    return pairs


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
    if s.dev_auth_bypass and raw.startswith("dev:"):
        tg_user = {"id": int(raw[4:] or 1), "first_name": "Dev"}
    else:
        try:
            data = validate_init_data(raw, s.bot_token)
        except InitDataError as e:
            raise HTTPException(status_code=401, detail="Нужно открыть приложение из Telegram") from e
        tg_user = data["user"]
    user, _ = await get_or_create_user(
        session, int(tg_user["id"]), first_name=tg_user.get("first_name", ""), last_name=tg_user.get("last_name"),
        username=tg_user.get("username"), language_code=tg_user.get("language_code"), photo_url=tg_user.get("photo_url"),
    )
    return user
