"""Часы приложения.

В обычном режиме — реальное время. Для отладки есть ускоренный режим
(FAST_DAY_MINUTES=N): «сутки» проходят за N минут реального времени,
отсчёт идёт от якоря, который сохраняется при первом запуске.
Все правила используют только clock.now(), поэтому ускорение прозрачно.
"""

from __future__ import annotations

from datetime import UTC, datetime

_fast_day_minutes: int = 0
_anchor: datetime | None = None
_offset_seconds: float = 0.0  # сдвиг времени (/timewarp в тестовом режиме)
_fixed: datetime | None = None  # для тестов: замороженное время


def configure(fast_day_minutes: int = 0, anchor: datetime | None = None) -> None:
    global _fast_day_minutes, _anchor
    _fast_day_minutes = max(0, int(fast_day_minutes or 0))
    _anchor = anchor


def is_fast() -> bool:
    return _fast_day_minutes > 0 and _anchor is not None


def speed() -> float:
    return (24 * 60) / _fast_day_minutes if is_fast() else 1.0


def real_now() -> datetime:
    if _fixed is not None:
        return _fixed
    return datetime.now(UTC)


def set_fixed(dt: datetime | None) -> None:
    """Только для тестов: зафиксировать «сейчас»."""
    global _fixed
    _fixed = dt


def now() -> datetime:
    if _fixed is not None:
        return _fixed
    real = real_now()
    if is_fast():
        assert _anchor is not None
        virtual = _anchor + (real - _anchor) * speed()
    else:
        virtual = real
    if _offset_seconds:
        from datetime import timedelta

        virtual = virtual + timedelta(seconds=_offset_seconds)
    return virtual


def shift(seconds: float) -> None:
    """Сдвинуть часы вперёд (админ-команда /timewarp в тестовом режиме)."""
    global _offset_seconds
    _offset_seconds += seconds


def offset_seconds() -> float:
    return _offset_seconds


def set_offset(seconds: float) -> None:
    global _offset_seconds
    _offset_seconds = seconds


def reset_shift() -> None:
    global _offset_seconds
    _offset_seconds = 0.0
