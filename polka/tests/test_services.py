"""Сценарии забега на базе: сдача, лимиты, закрытие дня, заморозки, пары, финиш, ачивки."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from core import clock
from db.models import DayResult, Enrollment, Pair, Retelling, Run, User, UserAchievement
from db.session import session_scope
from services.common import Outbox
from services.progress import close_pending_days, load_view
from services.retell import override
from services.runs import current_enrollment
from services.social import add_friend_by_code, auto_pairs, join_pair_by_code, nudge, partner_feed
from tests.helpers import (
    RETELL_BAD,
    RETELL_GENERAL,
    install_fake_llm,
    make_participant,
    set_now,
    setup_db,
    submit,
)

START = date(2026, 10, 12)


@pytest.fixture
async def world(tmp_path):
    await setup_db(tmp_path)
    fake = install_fake_llm()
    set_now(START - timedelta(days=3))
    async with session_scope() as s:
        run = Run(title="Тест", kind="main", start_date=START, status="open", grace_days=3, price_rub=990)
        s.add(run)
        await s.flush()
        a, ea = await make_participant(s, run, 1001, "Аня")
        b, eb = await make_participant(s, run, 1002, "Борис")
        ids = {"run": run.id, "a": a.id, "b": b.id, "ea": ea.id, "eb": eb.id}
    yield ids, fake
    clock.set_fixed(None)


async def _enr(eid):
    async with session_scope() as s:
        return await s.get(Enrollment, eid)


async def _close_all():
    async with session_scope() as s:
        for e in list(await s.scalars(select(Enrollment))):
            u = await s.get(User, e.user_id)
            await close_pending_days(s, e, u, Outbox())


async def test_plan_starts_on_run_start(world):
    ids, _ = world
    e = await _enr(ids["ea"])
    assert e.plan_start_date == START and e.plan_days == 21


async def test_cannot_submit_before_start(world):
    ids, _ = world
    res, _ = await submit(ids["a"])
    assert res.status == "not_started"


async def test_accept_then_limit_one_per_day(world):
    ids, _ = world
    set_now(START, 10)
    res, _ = await submit(ids["a"])
    assert res.status == "accepted" and res.streak == 1 and res.streak_grew
    assert "first_page" in res.achievements
    res2, _ = await submit(ids["a"])
    assert res2.status == "done_today"


async def test_short_text_is_not_attempt(world):
    ids, _ = world
    set_now(START, 10)
    res, _ = await submit(ids["a"], "прочитал")
    assert res.status == "too_short"


async def test_clarify_then_answer_accepts(world):
    ids, _ = world
    set_now(START, 10)
    res, _ = await submit(ids["a"], RETELL_GENERAL)
    assert res.status == "clarify" and res.question
    res, _ = await submit(ids["a"], "Он уехал к брату")  # короткий ответ на уточнение — допустим
    assert res.status == "accepted"
    async with session_scope() as s:
        r = await s.scalar(select(Retelling).where(Retelling.enrollment_id == ids["ea"]))
        assert r.clarify_count == 1 and r.verdict == "accepted"


async def test_rejected_then_retry(world):
    ids, _ = world
    set_now(START, 10)
    res, _ = await submit(ids["a"], RETELL_BAD)
    # у бумажной книги текста нет → rejected невозможен, будет уточнение
    assert res.status == "clarify"


async def test_freeze_then_missed_resets_streak(world):
    ids, _ = world
    set_now(START, 10)
    await submit(ids["a"])
    # день 2 пропущен → заморозка; день 3 пропущен (та же неделя) → missed
    set_now(START + timedelta(days=3), 10)
    await _close_all()
    async with session_scope() as s:
        rows = {r.user_day: r.result for r in await s.scalars(select(DayResult).where(DayResult.enrollment_id == ids["ea"]))}
        e = await s.get(Enrollment, ids["ea"])
    assert rows[START] == "done"
    assert rows[START + timedelta(days=1)] == "frozen"
    assert rows[START + timedelta(days=2)] == "missed"
    assert e.streak == 0 and e.best_streak == 1


async def test_catch_up_yesterday_and_comeback(world):
    ids, _ = world
    set_now(START, 10)
    await submit(ids["a"])
    set_now(START + timedelta(days=1), 10)
    await submit(ids["a"])
    # пропуск дня 3 и 4: заморозка, затем missed
    set_now(START + timedelta(days=4), 10)
    res, _ = await submit(ids["a"])
    assert res.status == "accepted"
    assert res.day_number == 3  # догоняем самый ранний несданный
    assert "comeback" in res.achievements
    res2, _ = await submit(ids["a"])
    assert res2.status == "done_today"


async def test_day_boundary_4am(world):
    ids, _ = world
    set_now(START + timedelta(days=1), 2, 30)  # 02:30 следующего дня — это ещё день 1
    res, _ = await submit(ids["a"])
    assert res.status == "accepted" and res.day_number == 1
    async with session_scope() as s:
        r = await s.scalar(select(Retelling).where(Retelling.enrollment_id == ids["ea"]))
        assert r.user_day == START


async def test_pair_streak_and_partner_notification(world):
    ids, _ = world
    async with session_scope() as s:
        run = await s.get(Run, ids["run"])
        ea, eb = await s.get(Enrollment, ids["ea"]), await s.get(Enrollment, ids["eb"])
        from services.social import make_pair

        await make_pair(s, run, ea, eb)
    for i in range(8):
        set_now(START + timedelta(days=i), 10)
        res_a, out_a = await submit(ids["a"])
        assert res_a.status == "accepted"
        if i == 0:
            assert any("твоя очередь" in m.text.lower() or "Твоя очередь" in m.text for m in out_a.messages)
        res_b, _ = await submit(ids["b"])
        assert res_b.status == "accepted"
    set_now(START + timedelta(days=8), 10)
    await _close_all()
    async with session_scope() as s:
        pair = await s.scalar(select(Pair))
        assert pair.streak == 8
        codes = set(await s.scalars(select(UserAchievement.achievement_code).where(UserAchievement.user_id == ids["a"])))
    assert {"duet", "week"} <= codes


async def test_pair_streak_resets_on_missed(world):
    ids, _ = world
    async with session_scope() as s:
        run = await s.get(Run, ids["run"])
        from services.social import make_pair

        await make_pair(s, run, await s.get(Enrollment, ids["ea"]), await s.get(Enrollment, ids["eb"]))
    set_now(START, 10)
    await submit(ids["a"])
    await submit(ids["b"])
    # день 2: только A; у B заморозка → общий стрик не меняется
    set_now(START + timedelta(days=1), 10)
    await submit(ids["a"])
    # день 3: только A; у B заморозки уже нет → missed → общий стрик 0
    set_now(START + timedelta(days=2), 10)
    await submit(ids["a"])
    set_now(START + timedelta(days=3), 10)
    await _close_all()
    async with session_scope() as s:
        pair = await s.scalar(select(Pair))
    assert pair.streak == 0 and pair.best_streak == 1


async def test_finish_with_grace_days_and_iron(tmp_path):
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        run = Run(title="Короткий", kind="main", start_date=START, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        u, e = await make_participant(s, run, 2001, "Вера", pages=40, days=7)
        uid, eid = u.id, e.id
    for i in range(7):
        set_now(START + timedelta(days=i), 10)
        res, out = await submit(uid)
        assert res.status == "accepted"
    assert res.finished
    assert {"finish", "iron"} <= set(res.achievements)
    e = await _enr(eid)
    assert e.status == "finished"


async def test_finish_in_grace_two_per_day(tmp_path):
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        run = Run(title="Короткий", kind="main", start_date=START, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        u, e = await make_participant(s, run, 2002, "Глеб", pages=40, days=7)
        uid, _eid = u.id, e.id
    for i in range(5):  # сдаёт 5 дней из 7
        set_now(START + timedelta(days=i), 10)
        await submit(uid)
    set_now(START + timedelta(days=8), 10)  # второй день отсрочки: можно по два
    r1, _ = await submit(uid)
    r2, _ = await submit(uid)
    assert r1.status == "accepted" and r2.status == "accepted" and r2.finished
    assert "iron" not in r2.achievements


async def test_dropped_after_deadline(tmp_path):
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        run = Run(title="Короткий", kind="main", start_date=START, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        u, e = await make_participant(s, run, 2003, "Дина", pages=40, days=7)
        uid, eid = u.id, e.id
    set_now(START + timedelta(days=11), 10)
    res, _ = await submit(uid)
    assert res.status == "expired"
    assert (await _enr(eid)).status == "dropped"


async def test_ai_down_queues_and_day_not_lost(world):
    ids, fake = world
    set_now(START, 23)
    fake.down = True
    res, _ = await submit(ids["a"])
    assert res.status == "queued"
    # пока проверка висит (меньше 6 часов), день не закрывается
    set_now(START + timedelta(days=1), 4, 30)
    await _close_all()
    async with session_scope() as s:
        assert (await s.scalar(select(DayResult).where(DayResult.enrollment_id == ids["ea"]))) is None
    # ИИ вернулся → очередь доделывает
    fake.down = False
    from services.flow import process_pending_queue

    clock.set_fixed(clock.now() + timedelta(minutes=5))
    out = Outbox()
    n = await process_pending_queue(out)
    assert n == 1 and out.messages
    async with session_scope() as s:
        r = await s.scalar(select(Retelling))
        assert r.verdict == "accepted" and r.user_day == START
    await _close_all()
    async with session_scope() as s:
        dr = await s.scalar(select(DayResult).where(DayResult.enrollment_id == ids["ea"]))
        assert dr.result == "done"


async def test_ai_down_for_long_accepts_without_verification(world):
    ids, fake = world
    set_now(START, 10)
    fake.down = True
    await submit(ids["a"])
    set_now(START + timedelta(days=1), 10)  # ИИ молчал почти сутки → засчитано без сверки
    await _close_all()
    async with session_scope() as s:
        r = await s.scalar(select(Retelling))
        dr = await s.scalar(select(DayResult).where(DayResult.enrollment_id == ids["ea"]))
    assert r.verdict == "accepted" and r.verified is False and dr.result == "done"


async def test_override_recomputes_streak(world):
    ids, _ = world
    set_now(START, 10)
    await submit(ids["a"])
    set_now(START + timedelta(days=1), 10)
    await submit(ids["a"], RETELL_GENERAL)  # уточнение, не засчитано
    set_now(START + timedelta(days=3), 10)
    await _close_all()
    async with session_scope() as s:
        r = await s.scalar(select(Retelling).where(Retelling.verdict == "clarify"))
        await override(s, r.id, Outbox())
        await s.get(Enrollment, ids["ea"])
        assert r.verdict == "accepted" and r.overridden


async def test_friends_and_nudge_limit(world):
    ids, _ = world
    set_now(START, 10)
    async with session_scope() as s:
        a, b = await s.get(User, ids["a"]), await s.get(User, ids["b"])
        out = Outbox()
        assert await add_friend_by_code(s, b, a.friend_code, is_new_user=False, outbox=out)
        assert out.messages
        # повторный переход ничего не меняет, с собой дружить нельзя
        assert await add_friend_by_code(s, b, a.friend_code, is_new_user=False, outbox=out) is None
        assert await add_friend_by_code(s, a, a.friend_code, is_new_user=False, outbox=out) is None
        assert await nudge(s, a, b, out) == "ok"
        assert await nudge(s, a, b, out) == "already"


async def test_social_notifications_max_two(world):
    ids, _ = world
    set_now(START, 10)
    async with session_scope() as s:
        run = await s.get(Run, ids["run"])
        target = await s.get(User, ids["b"])
        out = Outbox()
        senders = []
        for k in range(4):
            u, _ = await make_participant(s, run, 3000 + k, f"Друг{k}")
            await add_friend_by_code(s, u, target.friend_code, is_new_user=False, outbox=Outbox())
            senders.append(u)
        for u in senders:
            assert await nudge(s, u, target, out) == "ok"
        delivered = [m for m in out.messages if m.tg_id == target.tg_id and m.kind == "nudge"]
        assert len(delivered) == 2


async def test_pair_invite_and_auto_pairs(world):
    ids, _ = world
    async with session_scope() as s:
        a = await s.get(User, ids["a"])
        b = await s.get(User, ids["b"])
        ea = await s.get(Enrollment, ids["ea"])
        status, inviter = await join_pair_by_code(s, b, ea.pair_code, Outbox())
        assert status == "ok" and inviter.id == a.id
        run = await s.get(Run, ids["run"])
        for k in range(6):
            await make_participant(s, run, 4000 + k, f"Участник{k}")
        pairs = await auto_pairs(s, run, seed=1)
        assert len(pairs) == 1  # 6 без пары → половина (3) → округление до чётного (2) → 1 пара


async def test_partner_feed_same_book_locked(tmp_path):
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        run = Run(title="Т", kind="main", start_date=START, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        a, ea = await make_participant(s, run, 5001, "А", title="Мастер и Маргарита", author="Булгаков")
        b, eb = await make_participant(s, run, 5002, "Б", title="мастер и маргарита!", author="булгаков")
        from services.social import make_pair

        await make_pair(s, run, ea, eb)
        ids = (a.id, b.id, ea.id, eb.id)
    for i in range(3):
        set_now(START + timedelta(days=i), 10)
        await submit(ids[0])
    set_now(START + timedelta(days=2), 12)
    await submit(ids[1])
    async with session_scope() as s:
        b = await s.get(User, ids[1])
        eb = await current_enrollment(s, b.id)
        feed = await partner_feed(s, b, eb)
    assert feed["same_book"]
    assert [i["locked"] for i in feed["items"]] == [False, True, True]
    assert feed["items"][1]["text"] is None


async def test_view_states(world):
    ids, _ = world
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        u = await s.get(User, ids["a"])
        v = await load_view(s, u, await current_enrollment(s, u.id))
        assert v.state == "not_started"
    set_now(START, 10)
    async with session_scope() as s:
        u = await s.get(User, ids["a"])
        v = await load_view(s, u, await current_enrollment(s, u.id))
        assert v.state == "to_read" and v.plan_day == 1 and v.segment.day_number == 1
    await submit(ids["a"])
    async with session_scope() as s:
        u = await s.get(User, ids["a"])
        v = await load_view(s, u, await current_enrollment(s, u.id))
        assert v.state == "done_today" and v.next_segment.day_number == 2


async def test_brought_friend(world):
    ids, _ = world
    set_now(START, 10)
    async with session_scope() as s:
        a = await s.get(User, ids["a"])
        run = await s.get(Run, ids["run"])
        newbie, _e = await make_participant(s, run, 6001, "Новичок")
        await add_friend_by_code(s, newbie, a.friend_code, is_new_user=True, outbox=Outbox())
        nid = newbie.id
    set_now(START + timedelta(days=1), 10)  # оплатил в день старта → план с завтрашнего дня
    res, out = await submit(nid)
    assert res.status == "accepted"
    async with session_scope() as s:
        codes = set(await s.scalars(select(UserAchievement.achievement_code).where(UserAchievement.user_id == ids["a"])))
    assert "brought_friend" in codes
