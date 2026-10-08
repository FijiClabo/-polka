import pytest

from core.achievements import (
    ACHIEVEMENTS,
    TRIGGER_ACCEPTED,
    TRIGGER_DAY_CLOSED,
    TRIGGER_FINISH,
    TRIGGER_FRIEND_FIRST,
    AchievementContext,
    check_achievements,
)


def test_nine_achievements():
    assert len(ACHIEVEMENTS) == 9
    assert len({a[0] for a in ACHIEVEMENTS}) == 9


def test_first_page():
    assert check_achievements(AchievementContext(TRIGGER_ACCEPTED, total_accepted=1, streak=1), set()) == [
        "first_page"
    ]


@pytest.mark.parametrize("streak,expected", [(6, []), (7, ["week"]), (14, ["week", "two_weeks"])])
def test_streak_achievements(streak, expected):
    got = check_achievements(AchievementContext(TRIGGER_ACCEPTED, total_accepted=20, streak=streak), {"first_page"})
    assert got == expected


def test_iron_and_finish():
    ctx = AchievementContext(TRIGGER_FINISH, finished=True, finished_iron=True)
    assert check_achievements(ctx, set()) == ["iron", "finish"]
    ctx = AchievementContext(TRIGGER_FINISH, finished=True, finished_iron=False)
    assert check_achievements(ctx, set()) == ["finish"]


def test_duet_and_kept_word():
    assert check_achievements(AchievementContext(TRIGGER_DAY_CLOSED, pair_streak=7), set()) == ["duet"]
    assert check_achievements(AchievementContext(TRIGGER_DAY_CLOSED, pair_streak=30), {"duet"}) == ["kept_word"]
    assert check_achievements(AchievementContext(TRIGGER_DAY_CLOSED, pair_streak=6), set()) == []
    assert check_achievements(AchievementContext(TRIGGER_DAY_CLOSED, pair_streak=None), set()) == []


def test_comeback_after_missed_day():
    ctx = AchievementContext(TRIGGER_ACCEPTED, total_accepted=5, streak=1, prev_day_result="missed")
    assert check_achievements(ctx, {"first_page"}) == ["comeback"]
    ctx = AchievementContext(TRIGGER_ACCEPTED, total_accepted=5, streak=1, prev_day_result="frozen")
    assert check_achievements(ctx, {"first_page"}) == []


def test_brought_friend():
    ctx = AchievementContext(TRIGGER_FRIEND_FIRST, invited_friend_first_accept=True)
    assert check_achievements(ctx, set()) == ["brought_friend"]


def test_no_repeated_awards():
    ctx = AchievementContext(TRIGGER_ACCEPTED, total_accepted=30, streak=14, prev_day_result="missed")
    already = {"first_page", "week", "two_weeks", "comeback"}
    assert check_achievements(ctx, already) == []
    ctx = AchievementContext(TRIGGER_FINISH, finished=True, finished_iron=True)
    assert check_achievements(ctx, {"finish", "iron"}) == []
