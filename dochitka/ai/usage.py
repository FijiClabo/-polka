"""Учёт расходов на ИИ: сколько токенов и секунд голоса ушло и во что это примерно обошлось.

Вызовы ИИ складывают расход в память, планировщик раз в тик пишет его в журнал событий (ai_usage).
Так запись не открывает вложенных транзакций посреди проверки пересказа. Цены — оценка из настроек.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from settings import get_settings

log = logging.getLogger(__name__)

_user: ContextVar[int | None] = ContextVar("ai_user", default=None)
_kind: ContextVar[str] = ContextVar("ai_kind", default="check")
_buffer: list[dict] = []

# Claude: $ за 1 млн токенов (вход, выход). Самый длинный префикс — первым.
ANTHROPIC_USD_PER_1M: list[tuple[str, tuple[float, float]]] = [
    ("claude-opus-5-5", (4.0, 20.0)),
    ("claude-opus-5", (5.0, 25.0)),
    ("claude-sonnet-5-5", (2.0, 10.0)),
    ("claude-sonnet-5", (2.0, 10.0)),
    ("claude-haiku-5-5", (0.10, 0.50)),
    ("claude-haiku-4-5", (1.0, 5.0)),
]


@contextmanager
def ai_context(user_id: int | None, kind: str) -> Iterator[None]:
    """Чей это расход и на что: check — проверка пересказа, trial — пробный пересказ, summary — краткое содержание."""
    t1, t2 = _user.set(user_id), _kind.set(kind)
    try:
        yield
    finally:
        _user.reset(t1)
        _kind.reset(t2)


def cost_rub(provider: str, model: str, cheap: bool, tokens_in: int, tokens_out: int) -> float:
    s = get_settings()
    if provider == "yandex":
        per_1k = s.yandexgpt_cheap_rub_per_1k if cheap else s.yandexgpt_rub_per_1k
        return (tokens_in + tokens_out) / 1000 * per_1k
    if provider == "anthropic":
        p_in, p_out = next((v for k, v in ANTHROPIC_USD_PER_1M if model.startswith(k)), (4.0, 20.0))
        return (tokens_in * p_in + tokens_out * p_out) / 1_000_000 * s.usd_rub
    return 0.0


def record_llm(provider: str, model: str, cheap: bool, tokens_in: int, tokens_out: int) -> None:
    if provider == "demo":
        return
    _buffer.append({
        "user_id": _user.get(), "kind": "summary" if cheap else _kind.get(), "provider": provider, "model": model,
        "in": int(tokens_in), "out": int(tokens_out),
        "rub": round(cost_rub(provider, model, cheap, tokens_in, tokens_out), 4),
    })


def record_stt(provider: str, seconds: float) -> None:
    units = max(1, -(-int(seconds) // 15))  # распознавание считается 15-секундными единицами
    _buffer.append({
        "user_id": _user.get(), "kind": "voice", "provider": provider, "sec": round(seconds, 1),
        "rub": round(units * get_settings().speechkit_rub_per_15s, 4),
    })


def take() -> list[dict]:
    rows = list(_buffer)
    _buffer.clear()
    return rows


async def flush() -> int:
    """Записать накопленный расход в журнал событий. Вызывает планировщик."""
    rows = take()
    if not rows:
        return 0
    from db.models import Event
    from db.session import session_scope

    try:
        async with session_scope() as s:
            for r in rows:
                uid = r.pop("user_id")
                s.add(Event(user_id=uid, type="ai_usage", payload=r))
    except Exception:
        log.exception("ai usage flush failed")
        _buffer.extend(rows)  # не потеряем — попробуем в следующий тик
        return 0
    return len(rows)
