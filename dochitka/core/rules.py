"""Правила забега: отрезки, лимиты, закрытие дня, заморозки, пары, финиш, доступ.

Чистые функции без Telegram и базы — покрыты тестами в tests/test_core_*.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from core.days import deadline_day, week_index

FREEZES_PER_WEEK = 1
LAST_DAYS_DOUBLE = 3  # в последние N дней плана и в дни отсрочки можно сдавать по два отрезка
MIN_RETELL_CHARS = 50  # текст короче — обычное сообщение, а не попытка сдачи
MAX_CLARIFY = 2  # максимум уточнений на одну сдачу
MAX_ATTEMPTS_PER_HOUR = 10

DONE, FROZEN, MISSED = "done", "frozen", "missed"


# --------------------------------------------------------------------------- отрезки


def next_segment_day(accepted_days: set[int], plan_days: int, current_plan_day: int) -> int | None:
    """Какой отрезок сдаётся: самый ранний несданный с day_number <= текущего дня плана.

    Так работает «догнать вчерашний». В дни отсрочки текущий день плана больше plan_days,
    поэтому доступны все оставшиеся отрезки.
    """
    upto = min(current_plan_day, plan_days)
    for n in range(1, upto + 1):
        if n not in accepted_days:
            return n
    return None


def daily_limit(current_plan_day: int, plan_days: int) -> int:
    """Сколько отрезков можно сдать за день: обычно один, в конце забега — два."""
    if current_plan_day > plan_days - LAST_DAYS_DOUBLE:
        return 2
    return 1


def can_submit_more_today(accepted_today: int, current_plan_day: int, plan_days: int) -> bool:
    return accepted_today < daily_limit(current_plan_day, plan_days)


# --------------------------------------------------------------------------- день и стрик


@dataclass(frozen=True)
class DayClose:
    result: str  # done | frozen | missed
    streak: int
    freezes_left: int
    streak_lost: bool  # стрик был больше нуля и сгорел


def refresh_freezes(
    freezes_left: int, freezes_week: int, plan_day: int, per_week: int = FREEZES_PER_WEEK
) -> tuple[int, int]:
    """Заморозки: per_week на календарную неделю забега, не накапливаются."""
    wk = week_index(plan_day)
    if wk != freezes_week:
        return per_week, wk
    return freezes_left, freezes_week


def close_day(had_accepted: bool, streak: int, freezes_left: int) -> DayClose:
    """Итог дня в 04:00. Стрик за сданный день уже увеличен в момент сдачи."""
    if had_accepted:
        return DayClose(DONE, streak, freezes_left, False)
    if freezes_left > 0:
        return DayClose(FROZEN, streak, freezes_left - 1, False)
    return DayClose(MISSED, 0, freezes_left, streak > 0)


def streak_after_accept(streak: int, best: int, first_accept_today: bool) -> tuple[int, int]:
    """+1 к стрику за первый засчитанный пересказ дня."""
    if not first_accept_today:
        return streak, best
    streak += 1
    return streak, max(best, streak)


def pair_streak_after_day(result_a: str | None, result_b: str | None, streak: int) -> int:
    """Общий стрик пары при закрытии дня.

    оба done → +1; кто-то frozen, второй done/frozen → без изменений; хоть один missed → 0.
    None (у участника в этот день нет обязательств: план ещё не начался) считается как frozen.
    """
    a = result_a or FROZEN
    b = result_b or FROZEN
    if MISSED in (a, b):
        return 0
    if a == DONE and b == DONE:
        return streak + 1
    return streak


# --------------------------------------------------------------------------- финиш


def all_segments_done(accepted_days: set[int], plan_days: int) -> bool:
    return all(n in accepted_days for n in range(1, plan_days + 1))


def finish_in_time(last_accept_day: date, plan_start: date, plan_days: int, grace_days: int) -> bool:
    return last_accept_day <= deadline_day(plan_start, plan_days, grace_days)


def is_past_deadline(today: date, plan_start: date, plan_days: int, grace_days: int) -> bool:
    return today > deadline_day(plan_start, plan_days, grace_days)


# --------------------------------------------------------------------------- «та же книга»

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_text(s: str | None) -> str:
    """Нижний регистр, без знаков препинания, «ё» = «е»."""
    s = (s or "").lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s).replace("_", " ")
    return _SPACES.sub(" ", s).strip()


def normalize_author(s: str | None) -> str:
    """Как normalize_text, но порядок слов не важен: «Булгаков Михаил» = «Михаил Булгаков»."""
    return " ".join(sorted(normalize_text(s).split()))


def same_book(
    a_title_norm: str, a_author_norm: str, a_hash: str | None,
    b_title_norm: str, b_author_norm: str, b_hash: str | None,
) -> bool:
    if a_hash and b_hash and a_hash == b_hash:
        return True
    return bool(a_title_norm) and a_title_norm == b_title_norm and a_author_norm == b_author_norm


# --------------------------------------------------------------------------- доступ


def friendship_key(a: int, b: int) -> tuple[int, int]:
    if a == b:
        raise ValueError("Нельзя дружить с собой")
    return (a, b) if a < b else (b, a)


MAX_SOCIAL_NOTIFICATIONS_PER_DAY = 2  # толчки от друзей и напарника вместе
MAX_INITIATIVE_MESSAGES_PER_DAY = 3  # утро, вечер, уведомление о напарнике


def can_nudge(already_nudged_today: bool, recipient_allows: bool, recipient_done_today: bool) -> tuple[bool, str]:
    if already_nudged_today:
        return False, "already"
    if recipient_done_today:
        return False, "done"
    if not recipient_allows:
        return False, "disabled"
    return True, "ok"


def should_deliver_social(delivered_today: int) -> bool:
    return delivered_today < MAX_SOCIAL_NOTIFICATIONS_PER_DAY


# --------------------------------------------------------------------------- вердикт


def is_retell_attempt(text: str) -> bool:
    return len((text or "").strip()) > MIN_RETELL_CHARS
