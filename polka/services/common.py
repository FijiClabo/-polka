"""Общие вещи сервисного слоя: исходящие сообщения, лимиты уведомлений, события."""

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core import clock
from core.days import user_day
from core.rules import MAX_INITIATIVE_MESSAGES_PER_DAY, MAX_SOCIAL_NOTIFICATIONS_PER_DAY
from db.models import Event, Notification, User

log = logging.getLogger(__name__)

INITIATIVE_KINDS = ("morning", "evening", "partner")


@dataclass
class OutMsg:
    """Сообщение, которое нужно отправить после коммита транзакции."""

    tg_id: int
    text: str
    # ряды кнопок: {"text", "webapp": "screen?params"} | {"text", "callback"} | {"text", "url"}
    buttons: list[list[dict]] | None = None
    photo: bytes | None = None
    kind: str = "reply"


@dataclass
class Outbox:
    messages: list[OutMsg] = field(default_factory=list)

    def add(self, msg: OutMsg) -> None:
        self.messages.append(msg)

    def extend(self, other: Outbox) -> None:
        self.messages.extend(other.messages)

    def __len__(self) -> int:
        return len(self.messages)


def now() -> datetime:
    return clock.now()


def age(dt: datetime) -> timedelta:
    """Сколько реального времени прошло с момента записи в базе."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return clock.real_now() - dt


def today_for(user: User, at: datetime | None = None) -> date:
    return user_day(at or clock.now(), user.timezone)


def random_code(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


async def log_event(
    session: AsyncSession, type_: str, user_id: int | None = None, run_id: int | None = None, **payload
) -> None:
    session.add(Event(user_id=user_id, run_id=run_id, type=type_, payload=payload or None))


async def _try_insert_notification(session: AsyncSession, user: User, day: date, kind: str, key: str) -> bool:
    exists = await session.scalar(
        select(Notification.id).where(
            Notification.user_id == user.id, Notification.user_day == day, Notification.key == key
        )
    )
    if exists:
        return False
    try:
        async with session.begin_nested():
            session.add(Notification(user_id=user.id, user_day=day, kind=kind, key=key))
    except IntegrityError:
        return False
    return True


async def allow_initiative(session: AsyncSession, user: User, kind: str, key: str | None = None) -> bool:
    """Не больше трёх сообщений в день по инициативе бота (утро, вечер, напарник) и каждое — один раз."""
    if user.bot_blocked:
        return False
    day = today_for(user)
    key = key or kind
    if kind in INITIATIVE_KINDS:
        count = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user.id,
                Notification.user_day == day,
                Notification.kind.in_(INITIATIVE_KINDS),
            )
        )
        if (count or 0) >= MAX_INITIATIVE_MESSAGES_PER_DAY:
            return False
    return await _try_insert_notification(session, user, day, kind, key)


async def allow_social(session: AsyncSession, user: User, from_user_id: int) -> bool:
    """Толчки от друзей и напарника вместе — не больше двух в день, остальные молча отбрасываются."""
    if user.bot_blocked or not user.nudges_enabled:
        return False
    day = today_for(user)
    count = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user.id, Notification.user_day == day, Notification.kind == "nudge"
        )
    )
    if (count or 0) >= MAX_SOCIAL_NOTIFICATIONS_PER_DAY:
        return False
    return await _try_insert_notification(session, user, day, "nudge", f"nudge:{from_user_id}")


# --------------------------------------------------------------------------- часы (ускоренное время / timewarp)


async def init_clock(fast_day_minutes: int) -> None:
    """Восстановить якорь ускоренного времени и сдвиг часов после перезапуска."""
    from db.models import AppState
    from db.session import session_scope

    async with session_scope() as s:
        anchor = None
        if fast_day_minutes > 0:
            row = await s.get(AppState, "clock_anchor")
            if row is None:
                anchor = clock.real_now()
                s.add(AppState(key="clock_anchor", value=anchor.isoformat()))
            else:
                anchor = datetime.fromisoformat(row.value)
        clock.configure(fast_day_minutes, anchor)
        off = await s.get(AppState, "clock_offset")
        clock.set_offset(float(off.value) if off else 0.0)


async def save_clock_offset() -> None:
    from db.models import AppState
    from db.session import session_scope

    async with session_scope() as s:
        row = await s.get(AppState, "clock_offset")
        if row is None:
            s.add(AppState(key="clock_offset", value=str(clock.offset_seconds())))
        else:
            row.value = str(clock.offset_seconds())
