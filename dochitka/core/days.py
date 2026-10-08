"""День пользователя и календарь плана. Без зависимостей от Telegram и базы."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAY_START_HOUR = 4  # день пользователя длится с 04:00 до 04:00 по его поясу
DEFAULT_TZ = "Europe/Moscow"


def get_tz(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def ensure_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def local_now(now_utc: datetime, tz: str | None) -> datetime:
    return ensure_utc(now_utc).astimezone(get_tz(tz))


def user_day(now_utc: datetime, tz: str | None) -> date:
    """Дата «дня пользователя»: всё, что до 04:00 местного времени, относится к предыдущему дню."""
    return (local_now(now_utc, tz) - timedelta(hours=DAY_START_HOUR)).date()


def day_start_utc(day: date, tz: str | None) -> datetime:
    """Начало дня пользователя (04:00 местного) в UTC."""
    local = datetime.combine(day, time(DAY_START_HOUR), tzinfo=get_tz(tz))
    return local.astimezone(UTC)


def day_end_utc(day: date, tz: str | None) -> datetime:
    return day_start_utc(day + timedelta(days=1), tz)


def local_clock(now_utc: datetime, tz: str | None) -> time:
    """Местное время суток. Для сравнения с утренним/вечерним временем рассылок."""
    return local_now(now_utc, tz).time().replace(tzinfo=None)


def minutes_since_day_start(now_utc: datetime, tz: str | None) -> int:
    """Сколько минут прошло с 04:00 текущего дня пользователя (0..1439)."""
    loc = local_now(now_utc, tz)
    m = loc.hour * 60 + loc.minute - DAY_START_HOUR * 60
    return m % (24 * 60)


def clock_minutes_in_user_day(t: time) -> int:
    """Положение времени суток внутри дня пользователя (04:00 → 0, 03:59 → 1439)."""
    return (t.hour * 60 + t.minute - DAY_START_HOUR * 60) % (24 * 60)


def plan_day_number(plan_start: date, day: date) -> int:
    """Номер дня плана (1 — день старта). 0 и меньше — план ещё не начался."""
    return (day - plan_start).days + 1


def last_plan_day(plan_start: date, plan_days: int) -> date:
    return plan_start + timedelta(days=plan_days - 1)


def deadline_day(plan_start: date, plan_days: int, grace_days: int) -> date:
    """Последний день, в который ещё можно сдать отрезки и пройти забег."""
    return last_plan_day(plan_start, plan_days) + timedelta(days=grace_days)


def week_index(plan_day: int) -> int:
    """Неделя забега: дни 1–7 → 0, 8–14 → 1 и т.д."""
    return max(plan_day - 1, 0) // 7


def daterange(start: date, end_inclusive: date):
    d = start
    while d <= end_inclusive:
        yield d
        d += timedelta(days=1)
