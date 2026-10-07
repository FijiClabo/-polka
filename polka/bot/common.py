"""Общие помощники обработчиков."""

from __future__ import annotations

import re
from datetime import time

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import User as TgUser

from db.models import User
from services.users import get_or_create_user
from settings import get_settings

TZ_BUTTONS = [
    ("Калининград (МСК−1)", "Europe/Kaliningrad"),
    ("Москва", "Europe/Moscow"),
    ("Самара (+1)", "Europe/Samara"),
    ("Екатеринбург (+2)", "Asia/Yekaterinburg"),
    ("Омск (+3)", "Asia/Omsk"),
    ("Новосибирск (+4)", "Asia/Novosibirsk"),
    ("Красноярск (+4)", "Asia/Krasnoyarsk"),
    ("Иркутск (+5)", "Asia/Irkutsk"),
    ("Якутск (+6)", "Asia/Yakutsk"),
    ("Владивосток (+7)", "Asia/Vladivostok"),
    ("Магадан (+8)", "Asia/Magadan"),
    ("Камчатка (+9)", "Asia/Kamchatka"),
]
MSK_OFFSET_TZ = {
    -1: "Europe/Kaliningrad", 0: "Europe/Moscow", 1: "Europe/Samara", 2: "Asia/Yekaterinburg", 3: "Asia/Omsk",
    4: "Asia/Krasnoyarsk", 5: "Asia/Irkutsk", 6: "Asia/Yakutsk", 7: "Asia/Vladivostok", 8: "Asia/Magadan",
    9: "Asia/Kamchatka",
}
TZ_LABEL = {tz: label for label, tz in TZ_BUTTONS}


class Flow(StatesGroup):
    tz_input = State()
    morning_input = State()
    evening_input = State()
    trial = State()
    paper_title = State()
    paper_author = State()
    paper_pages = State()


async def load_user(session, tg: TgUser) -> tuple[User, bool]:
    return await get_or_create_user(
        session, tg.id, first_name=tg.first_name or "", last_name=tg.last_name, username=tg.username,
        language_code=tg.language_code,
    )


def parse_tz(text: str) -> str | None:
    """«+4» — от Москвы, «UTC+5» / «GMT-3» — от UTC, или название IANA."""
    t = text.strip().replace("−", "-").replace(" ", "")
    m = re.fullmatch(r"(?i)(utc|gmt)([+-]\d{1,2})(?::?00)?", t)
    if m:
        off = int(m.group(2))
        if -12 <= off <= 14:
            return "Etc/UTC" if off == 0 else f"Etc/GMT{'-' if off > 0 else '+'}{abs(off)}"
        return None
    m = re.fullmatch(r"(?i)(?:мск)?([+-]?\d{1,2})", t)
    if m:
        off = int(m.group(1))
        if off in MSK_OFFSET_TZ:
            return MSK_OFFSET_TZ[off]
        utc = off + 3
        if -12 <= utc <= 14:
            return f"Etc/GMT{'-' if utc > 0 else '+'}{abs(utc)}" if utc else "Etc/UTC"
        return None
    if "/" in t:
        from services.users import valid_timezone

        return t if valid_timezone(t) else None
    return None


def parse_time(text: str) -> time | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.\s]?(\d{2})?\s*", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return time(h, mi)
    return None


def tz_label(tz: str) -> str:
    return TZ_LABEL.get(tz, tz)


_tf = None


def tz_from_location(lat: float, lon: float) -> str | None:
    global _tf
    try:
        if _tf is None:
            from timezonefinder import TimezoneFinder

            _tf = TimezoneFinder()
        return _tf.timezone_at(lng=lon, lat=lat)
    except Exception:
        return None


def is_admin(tg_id: int) -> bool:
    return tg_id in get_settings().admin_ids
