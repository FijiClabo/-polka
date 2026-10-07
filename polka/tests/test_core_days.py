from datetime import UTC, date, datetime, time

import pytest

from core.days import (
    clock_minutes_in_user_day,
    day_start_utc,
    deadline_day,
    last_plan_day,
    plan_day_number,
    user_day,
    week_index,
)

RU_ZONES = [
    ("Europe/Kaliningrad", 2),
    ("Europe/Moscow", 3),
    ("Europe/Samara", 4),
    ("Asia/Yekaterinburg", 5),
    ("Asia/Omsk", 6),
    ("Asia/Novosibirsk", 7),
    ("Asia/Krasnoyarsk", 7),
    ("Asia/Irkutsk", 8),
    ("Asia/Yakutsk", 9),
    ("Asia/Vladivostok", 10),
    ("Asia/Magadan", 11),
    ("Asia/Kamchatka", 12),
]


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


@pytest.mark.parametrize("tz,offset", RU_ZONES)
def test_day_boundary_at_4am_in_all_russian_zones(tz, offset):
    # 03:59 местного → ещё вчерашний день, 04:00 → новый
    local_359_utc = utc(2026, 10, 7, 3, 59)
    local_400_utc = utc(2026, 10, 7, 4, 0)
    from datetime import timedelta

    before = local_359_utc - timedelta(hours=offset)
    after = local_400_utc - timedelta(hours=offset)
    assert user_day(before, tz) == date(2026, 10, 6)
    assert user_day(after, tz) == date(2026, 10, 7)


def test_late_night_belongs_to_previous_day():
    # 01:30 по Москве 8 октября — это ещё 7 октября
    assert user_day(utc(2026, 10, 7, 22, 30), "Europe/Moscow") == date(2026, 10, 7)
    # 23:00 по Москве — тот же день
    assert user_day(utc(2026, 10, 7, 20, 0), "Europe/Moscow") == date(2026, 10, 7)


def test_same_utc_moment_different_days_in_different_zones():
    moment = utc(2026, 10, 7, 20, 0)  # 23:00 Мск, 07:00 следующего дня во Владивостоке
    assert user_day(moment, "Europe/Moscow") == date(2026, 10, 7)
    assert user_day(moment, "Asia/Vladivostok") == date(2026, 10, 8)


def test_unknown_timezone_falls_back_to_moscow():
    assert user_day(utc(2026, 10, 7, 0, 30), "Mars/Olympus") == date(2026, 10, 6)


def test_day_start_utc_roundtrip():
    start = day_start_utc(date(2026, 10, 7), "Asia/Yekaterinburg")
    assert start == utc(2026, 10, 6, 23, 0)
    assert user_day(start, "Asia/Yekaterinburg") == date(2026, 10, 7)


def test_plan_calendar():
    start = date(2026, 10, 1)
    assert plan_day_number(start, date(2026, 10, 1)) == 1
    assert plan_day_number(start, date(2026, 9, 30)) == 0
    assert plan_day_number(start, date(2026, 10, 30)) == 30
    assert last_plan_day(start, 30) == date(2026, 10, 30)
    assert deadline_day(start, 30, 3) == date(2026, 11, 2)


def test_week_index():
    assert [week_index(n) for n in (1, 7, 8, 14, 15, 29, 30)] == [0, 0, 1, 1, 2, 4, 4]


def test_clock_minutes_in_user_day():
    assert clock_minutes_in_user_day(time(4, 0)) == 0
    assert clock_minutes_in_user_day(time(9, 0)) == 300
    assert clock_minutes_in_user_day(time(3, 59)) == 1439
