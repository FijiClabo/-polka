"""Ачивки: небольшой фиксированный набор, каждая за реальное действие и один раз."""

from __future__ import annotations

from dataclasses import dataclass, field

# code, title, description, sort_order
ACHIEVEMENTS: list[tuple[str, str, str, int]] = [
    ("first_page", "Первая страница", "Первый засчитанный пересказ", 1),
    ("week", "Неделя", "Стрик 7 дней", 2),
    ("two_weeks", "Две недели", "Стрик 14 дней", 3),
    ("iron", "Железный", "Весь план без единой заморозки и пропуска", 4),
    ("duet", "Дуэт", "Общий стрик пары 7 дней", 5),
    ("kept_word", "Месяц вдвоём", "Общий стрик пары 30 дней", 6),
    ("comeback", "Возвращение", "Пересказ на следующий день после сгоревшего стрика", 7),
    ("brought_friend", "Друг в деле", "Друг по твоей ссылке сдал первый пересказ", 8),
    ("finish", "Финиш", "Первая дочитанная книга", 9),
]

ACH_BY_CODE = {a[0]: a for a in ACHIEVEMENTS}

TRIGGER_ACCEPTED = "accepted"
TRIGGER_DAY_CLOSED = "day_closed"
TRIGGER_FINISH = "finish"
TRIGGER_FRIEND_FIRST = "friend_first_accept"


@dataclass
class AchievementContext:
    trigger: str
    total_accepted: int = 0
    streak: int = 0
    pair_streak: int | None = None
    prev_day_result: str | None = None  # итог дня перед днём сдачи
    finished: bool = False
    finished_iron: bool = False
    invited_friend_first_accept: bool = False
    extra: dict = field(default_factory=dict)


def check_achievements(ctx: AchievementContext, already: set[str]) -> list[str]:
    """Какие ачивки выдать сейчас. Уже полученные не выдаются повторно."""
    new: list[str] = []

    def give(code: str) -> None:
        if code not in already and code not in new:
            new.append(code)

    if ctx.trigger == TRIGGER_ACCEPTED:
        if ctx.total_accepted >= 1:
            give("first_page")
        if ctx.streak >= 7:
            give("week")
        if ctx.streak >= 14:
            give("two_weeks")
        if ctx.prev_day_result == "missed":
            give("comeback")

    if ctx.trigger in (TRIGGER_DAY_CLOSED, TRIGGER_ACCEPTED) and ctx.pair_streak is not None:
        if ctx.pair_streak >= 7:
            give("duet")
        if ctx.pair_streak >= 30:
            give("kept_word")

    if ctx.trigger == TRIGGER_FINISH:
        if ctx.finished:
            give("finish")
        if ctx.finished_iron:
            give("iron")

    if ctx.trigger == TRIGGER_FRIEND_FIRST and ctx.invited_friend_first_accept:
        give("brought_friend")

    order = {a[0]: a[3] for a in ACHIEVEMENTS}
    return sorted(new, key=lambda c: order.get(c, 99))
