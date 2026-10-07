from datetime import date

import pytest

from core.rules import (
    DONE,
    FROZEN,
    MISSED,
    all_segments_done,
    can_nudge,
    can_submit_more_today,
    can_view_partner_retelling,
    close_day,
    daily_limit,
    finish_in_time,
    friendship_key,
    is_past_deadline,
    is_retell_attempt,
    next_segment_day,
    normalize_author,
    normalize_text,
    pair_streak_after_day,
    refresh_freezes,
    same_book,
    should_deliver_social,
    streak_after_accept,
)

# ------------------------------------------------------------------ какой отрезок сдаётся


def test_next_segment_is_todays_when_on_track():
    assert next_segment_day({1, 2, 3}, 30, 4) == 4


def test_catch_up_yesterday_first():
    # вчера (день 4) не сдан — сегодня (день 5) сначала сдаётся 4-й
    assert next_segment_day({1, 2, 3}, 30, 5) == 4


def test_no_segment_ahead_of_plan():
    assert next_segment_day({1, 2, 3, 4}, 30, 4) is None


def test_grace_days_open_all_remaining():
    assert next_segment_day(set(range(1, 29)), 30, 32) == 29


def test_nothing_before_start():
    assert next_segment_day(set(), 30, 0) is None


# ------------------------------------------------------------------ лимит в день


def test_one_segment_per_day_normally():
    assert daily_limit(10, 30) == 1
    assert can_submit_more_today(0, 10, 30)
    assert not can_submit_more_today(1, 10, 30)


def test_two_segments_in_last_days_and_grace():
    assert daily_limit(28, 30) == 2
    assert daily_limit(30, 30) == 2
    assert daily_limit(32, 30) == 2
    assert can_submit_more_today(1, 31, 30)
    assert not can_submit_more_today(2, 31, 30)


# ------------------------------------------------------------------ закрытие дня


def test_close_done_day_keeps_streak():
    r = close_day(True, 5, 1)
    assert (r.result, r.streak, r.freezes_left, r.streak_lost) == (DONE, 5, 1, False)


def test_close_missed_day_uses_freeze():
    r = close_day(False, 5, 1)
    assert (r.result, r.streak, r.freezes_left) == (FROZEN, 5, 0)


def test_close_missed_day_without_freeze_resets_streak():
    r = close_day(False, 5, 0)
    assert (r.result, r.streak, r.freezes_left, r.streak_lost) == (MISSED, 0, 0, True)


def test_missed_with_zero_streak_is_not_streak_lost():
    r = close_day(False, 0, 0)
    assert r.result == MISSED and not r.streak_lost


def test_streak_increment_only_once_per_day():
    assert streak_after_accept(3, 3, True) == (4, 4)
    assert streak_after_accept(4, 4, False) == (4, 4)
    assert streak_after_accept(1, 9, True) == (2, 9)


# ------------------------------------------------------------------ заморозки


def test_one_freeze_per_week_not_accumulating():
    # неделя 0, заморозка не тратилась → на неделе 1 снова одна, а не две
    assert refresh_freezes(1, 0, 8) == (1, 1)
    # потратил на неделе 0 → на неделе 1 восстановилась
    assert refresh_freezes(0, 0, 8) == (1, 1)
    # внутри недели не восстанавливается
    assert refresh_freezes(0, 1, 10) == (0, 1)


# ------------------------------------------------------------------ общий стрик пары


@pytest.mark.parametrize(
    "a,b,before,after",
    [
        (DONE, DONE, 3, 4),
        (DONE, FROZEN, 3, 3),
        (FROZEN, FROZEN, 3, 3),
        (FROZEN, DONE, 3, 3),
        (DONE, MISSED, 3, 0),
        (MISSED, FROZEN, 3, 0),
        (MISSED, MISSED, 3, 0),
        (None, DONE, 3, 3),  # у напарника план ещё не начался
    ],
)
def test_pair_streak(a, b, before, after):
    assert pair_streak_after_day(a, b, before) == after


# ------------------------------------------------------------------ финиш


def test_finish_requires_all_segments():
    assert all_segments_done(set(range(1, 31)), 30)
    assert not all_segments_done(set(range(1, 30)), 30)


def test_finish_with_grace():
    start = date(2026, 10, 1)
    assert finish_in_time(date(2026, 10, 30), start, 30, 3)
    assert finish_in_time(date(2026, 11, 2), start, 30, 3)  # третий день отсрочки
    assert not finish_in_time(date(2026, 11, 3), start, 30, 3)
    assert not is_past_deadline(date(2026, 11, 2), start, 30, 3)
    assert is_past_deadline(date(2026, 11, 3), start, 30, 3)


# ------------------------------------------------------------------ та же книга


def test_normalization():
    assert normalize_text("Мастер и Маргарита!") == "мастер и маргарита"
    assert normalize_text("Ёлка — «зелёная»") == "елка зеленая"
    assert normalize_author("Булгаков, Михаил") == normalize_author("Михаил Булгаков")


def test_same_book_by_names_or_hash():
    t, a = normalize_text("Мастер и Маргарита"), normalize_author("М. Булгаков")
    assert same_book(t, a, None, t, a, None)
    assert same_book("x", "y", "h1", "z", "w", "h1")
    assert not same_book(t, a, None, normalize_text("Собачье сердце"), a, None)
    assert not same_book("", "", None, "", "", None)


# ------------------------------------------------------------------ доступ к пересказам напарника


def test_partner_retelling_visibility():
    assert can_view_partner_retelling(True, 0.30, 0.30)
    assert can_view_partner_retelling(True, 0.20, 0.35)
    assert not can_view_partner_retelling(True, 0.40, 0.35)
    assert not can_view_partner_retelling(False, 0.10, 0.90)


# ------------------------------------------------------------------ друзья и толчки


def test_friendship_key():
    assert friendship_key(5, 3) == (3, 5)
    with pytest.raises(ValueError):
        friendship_key(4, 4)


def test_nudge_limit_once_per_day():
    assert can_nudge(False, True, False) == (True, "ok")
    assert can_nudge(True, True, False) == (False, "already")
    assert can_nudge(False, False, False) == (False, "disabled")
    assert can_nudge(False, True, True) == (False, "done")


def test_social_notifications_limit():
    assert should_deliver_social(0) and should_deliver_social(1)
    assert not should_deliver_social(2)


def test_retell_attempt_length():
    assert not is_retell_attempt("привет")
    assert not is_retell_attempt("а" * 50)
    assert is_retell_attempt("а" * 51)
