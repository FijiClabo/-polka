"""Свой темп: прочитано больше, прочитано не всё, другое время в день, глава в день, пауза."""

from datetime import timedelta

from sqlalchemy import select

from core import clock
from db.models import Book, DayResult, Enrollment, Run, Segment, User
from db.session import session_scope
from services.books import attach_book, confirm_plan, save_parsed
from services.common import Outbox
from services.pace import PaceError, ask_more, pace_info, pause, replan, split_today
from services.progress import close_pending_days, load_view
from services.runs import ensure_enrollment, grant
from services.users import get_or_create_user
from tests.helpers import install_fake_llm, make_participant, set_now, setup_db, submit
from tests.test_api import client  # noqa: F401 — фикстура клиента API

START = clock.real_now().date() + timedelta(days=3)


async def _paper(tmp_path, pages=210, days=21):
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        run = Run(title="Забег", kind="main", start_date=START, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        u, e = await make_participant(s, run, 7001, "Вера", pages=pages, days=days)
        return u.id, e.id


async def _text(tmp_path, sample="01_clean_with_toc.epub", days=21):
    from books.parse import parse_book
    from books.samples import sample_set

    await setup_db(tmp_path)
    install_fake_llm()
    set_now(START, 10)
    async with session_scope() as s:
        u, _ = await get_or_create_user(s, 7002, first_name="Тим")
        u.timezone = "Europe/Moscow"
        enr = await ensure_enrollment(s, u)
        await grant(s, u, enr.run)
        book = Book(owner_user_id=u.id, source="epub", title="x", parse_status="pending")
        s.add(book)
        await s.flush()
        await save_parsed(s, book, parse_book("b.epub", sample_set()[sample]), "h-" + sample)
        await attach_book(s, enr, book)
        await confirm_plan(s, u, enr, days)
        return u.id, enr.id


async def _ctx(s, uid, eid):
    return await s.get(User, uid), await s.get(Enrollment, eid)


async def _segs(s, eid) -> list[Segment]:
    enr = await s.get(Enrollment, eid)
    return list(await s.scalars(select(Segment).where(Segment.book_id == enr.book_id).order_by(Segment.day_number)))


async def _results(s, eid) -> dict:
    return {r.user_day: r.result for r in await s.scalars(select(DayResult).where(DayResult.enrollment_id == eid))}


# ------------------------------------------------------------------ прочитано больше


async def test_read_ahead_then_rest_day_keeps_streak(tmp_path):
    uid, eid = await _paper(tmp_path)
    set_now(START, 10)
    res, _ = await submit(uid)
    assert res.status == "accepted" and res.can_read_more and not res.can_submit_more
    res, _ = await submit(uid)
    assert res.status == "done_today"  # без просьбы следующий не открываем: вдруг это просто сообщение
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        seg = await ask_more(s, u, e)
        assert seg.day_number == 2
    res, _ = await submit(uid)
    assert res.status == "accepted" and res.day_number == 2 and not res.streak_grew and res.streak == 1

    # день 2: по плану ничего — можно отдохнуть или читать дальше
    set_now(START + timedelta(days=1), 10)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        v = await load_view(s, u, e)
        assert v.state == "to_read" and v.extra == "ahead" and v.segment.day_number == 3 and v.behind == 0
    # день 2 без пересказа закрывается как «заранее»: стрик и заморозка целы
    set_now(START + timedelta(days=2), 10)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        await close_pending_days(s, e, u, Outbox())
        assert (await _results(s, eid))[START + timedelta(days=1)] == "ahead"
        assert e.streak == 1 and e.freezes_left == 1
        v = await load_view(s, u, e)
        assert v.state == "to_read" and v.extra == "" and v.segment.day_number == 3


# ------------------------------------------------------------------ прочитано не всё


async def test_partial_paper_rest_tomorrow_and_new_day(tmp_path):
    uid, eid = await _paper(tmp_path)  # 210 стр. на 21 день — по 10 страниц
    set_now(START, 10)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        p = await split_today(s, u, e, quarter=2, rest="tomorrow")
        assert (p.segment.page_from, p.segment.page_to, p.segment.title) == (1, 5, "стр. 1–5")
        segs = await _segs(s, eid)
        assert (segs[1].page_from, segs[1].page_to, segs[1].title) == (6, 20, "стр. 6–20")
        assert e.plan_days == 21
    res, _ = await submit(uid)
    assert res.status == "accepted" and res.day_number == 1

    set_now(START + timedelta(days=1), 10)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        p = await split_today(s, u, e, page=12, rest="day")
        assert p.plan_days == 22 and (p.segment.page_from, p.segment.page_to) == (6, 12)
        segs = await _segs(s, eid)
        assert [x.day_number for x in segs] == list(range(1, 23))
        assert (segs[2].page_from, segs[2].page_to) == (13, 20) and (segs[3].page_from, segs[3].page_to) == (21, 30)
        assert segs[-1].pos_to == 1.0


async def test_partial_text_keeps_every_paragraph(tmp_path):
    uid, eid = await _text(tmp_path)
    async with session_scope() as s:
        before = "\n".join(x.text for x in await _segs(s, eid))
        u, e = await _ctx(s, uid, eid)
        p = await split_today(s, u, e, quarter=1, rest="day")
        segs = await _segs(s, eid)
        assert "\n".join(x.text for x in segs) == before  # ни слова не потеряно и не повторено
        assert segs[0].pos_to == segs[1].pos_from and segs[1].title.endswith("(продолжение)")
        assert p.segment.text and len(p.segment.text) < len(segs[1].text) * 2
    try:
        async with session_scope() as s:
            u, e = await _ctx(s, uid, eid)
            await split_today(s, u, e, quarter=5)
        raise AssertionError("ожидали отказ")
    except PaceError:
        pass


# ------------------------------------------------------------------ другой темп в пути


async def test_replan_minutes_keeps_done_and_moves_start(tmp_path):
    uid, eid = await _text(tmp_path, days=21)
    res, _ = await submit(uid)
    assert res.status == "accepted"
    async with session_scope() as s:
        before = "\n".join(x.text for x in await _segs(s, eid))
        first = (await _segs(s, eid))[0]
        first_text = first.text
        u, e = await _ctx(s, uid, eid)
        info = await pace_info(s, u, e)
        assert info["done"] == 1 and info["left"] == 20 and info["minute_options"]
        r = await replan(s, u, e, minutes=60)
        segs = await _segs(s, eid)
        assert segs[0].text == first_text  # сданный отрезок не трогаем
        assert "\n".join(x.text for x in segs) == before
        assert e.plan_days == 1 + len(r.segments) < 21
        assert segs[-1].pos_to == 1.0 and abs(segs[1].pos_from - segs[0].pos_to) < 0.01
        # сегодня уже сдано — следующий отрезок завтра, вчерашний день не появился
        v = await load_view(s, u, e)
        assert v.state == "done_today" and v.next_segment.day_number == 2 and v.behind == 0
    set_now(START + timedelta(days=1), 10)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        v = await load_view(s, u, e)
        assert v.state == "to_read" and v.segment.day_number == 2 and v.plan_day == 2


async def test_replan_when_behind_puts_plan_back_on_track(tmp_path):
    uid, eid = await _paper(tmp_path)
    set_now(START, 10)
    await submit(uid)
    set_now(START + timedelta(days=4), 10)  # пропустил три дня
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        await close_pending_days(s, e, u, Outbox())
        v = await load_view(s, u, e)
        assert v.behind == 4
        info = await pace_info(s, u, e)
        assert info["pause_options"] and info["behind"] == 4
        await replan(s, u, e, finish=START + timedelta(days=4 + 29))
        assert e.plan_days == 1 + 30
        v = await load_view(s, u, e)
        assert v.state == "to_read" and v.segment.day_number == 2 and v.behind == 1 and not v.catching_up
        segs = await _segs(s, eid)
        assert segs[1].page_from == 11 and segs[-1].page_to == 210


async def test_chapter_per_day(tmp_path):
    uid, eid = await _text(tmp_path)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        info = await pace_info(s, u, e)
        assert info["chapters"] and info["chapters"]["days"] >= 2
        await replan(s, u, e, by_chapter=True)
        segs = await _segs(s, eid)
        assert len(segs) == info["chapters"]["days"] == e.plan_days
        assert all("часть" not in x.title for x in segs)


async def test_confirm_plan_by_minutes_and_chapters(tmp_path):
    uid, eid = await _text(tmp_path)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        e.plan_start_date = None  # план ещё не начался
        await confirm_plan(s, u, e, minutes=10)
        by_minutes = e.plan_days
        await confirm_plan(s, u, e, by_chapter=True)
        assert e.plan_days != by_minutes and e.plan_days == len(await _segs(s, eid))


async def test_pause_shifts_plan_without_losing_streak(tmp_path):
    uid, eid = await _paper(tmp_path)
    set_now(START, 10)
    await submit(uid)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        finish = await pause(s, u, e, 3)
        assert finish == START + timedelta(days=3 + 20)
    set_now(START + timedelta(days=4), 10)
    async with session_scope() as s:
        u, e = await _ctx(s, uid, eid)
        await close_pending_days(s, e, u, Outbox())
        rows = await _results(s, eid)
        # дни паузы до нового старта не закрываются вовсе, первый день нового отсчёта сдан заранее
        assert START + timedelta(days=1) not in rows and rows[START + timedelta(days=3)] == "ahead"
        assert e.streak == 1 and e.freezes_left == 1
        v = await load_view(s, u, e)
        assert v.state == "to_read" and v.segment.day_number == 2


# ------------------------------------------------------------------ API и копия плана «читаем вместе»


async def test_pace_api_and_shared_copy(client):  # noqa: F811
    from services.retell import build_check_input
    from services.runs import current_enrollment
    from services.social import join_pair_by_code
    from tests.helpers import RETELL_OK
    from tests.test_api import H
    from tests.test_together import A_TG, B_TG, _reader_with_plan

    set_now(clock.real_now().date(), 10)
    _, code = await _reader_with_plan()
    ha, hb = H(A_TG, "Аня"), H(B_TG, "Боря")
    for h in (ha, hb):
        await client.get("/api/me", headers=h)
        await client.post("/api/consent", headers=h)
    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        assert (await join_pair_by_code(s, b, code, Outbox(), together=True))[0] == "ok"
    assert (await client.post("/api/book/together", headers=hb)).status_code == 200
    async with session_scope() as s:
        for tg in (A_TG, B_TG):
            u = await s.scalar(select(User).where(User.tg_id == tg))
            enr = await current_enrollment(s, u.id)
            await grant(s, u, enr.run)

    # копия плана: свой темп собирается по тексту исходной книги, но текст другу не попадает
    info = (await client.get("/api/pace", headers=hb)).json()
    assert info["left"] == 21 and info["minute_options"] and info["pair"]["name"] == "Аня"
    r = await client.post("/api/pace", json={"minutes": 60}, headers=hb)
    assert r.status_code == 200, r.text
    assert r.json()["left"] < 21
    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        enr = await current_enrollment(s, b.id)
        segs = await _segs(s, enr.id)
        assert all(x.text is None and x.start_words and x.end_words for x in segs) and segs[-1].pos_to == 1.0
        # краткие содержания для проверки — по месту в книге, а не по номеру дня
        src = list(await s.scalars(select(Segment).where(Segment.book_id == (await s.get(Book, enr.book_id)).shared_from_id)))
        for x in src:
            x.summary = f"Содержание {x.day_number}."
        await s.flush()
        from db.models import Retelling

        r0 = Retelling(enrollment_id=enr.id, segment_id=segs[0].id, user_day=clock.now().date(), source="text",
                       raw_text="x", verdict="pending", attempt_no=1, clarify_count=0, verified=False)
        s.add(r0)
        await s.flush()
        check = await build_check_input(s, r0)
        assert check.segment_text is None and check.segment_summary.count("Содержание") >= 2  # отрезок стал длиннее

    # прочитано не всё: остаток — отдельным днём
    t = (await client.get("/api/today", headers=ha)).json()
    assert t["state"] == "to_read" and t["can_split"]
    pace = (await client.get("/api/pace", headers=ha)).json()
    assert [o["quarter"] for o in pace["today"]["partial"]] == [1, 2, 3]
    r = await client.post("/api/pace/partial", json={"quarter": 2, "rest": "day"}, headers=ha)
    assert r.status_code == 200 and r.json()["plan_days"] == 22

    # сдал прочитанное → «перескажу ещё один» открывает следующий
    r = (await client.post("/api/retell", json={"text": RETELL_OK}, headers=ha)).json()
    assert r["status"] == "accepted" and r["can_read_more"] and not r["can_submit_more"]
    t = (await client.get("/api/today", headers=ha)).json()
    assert t["state"] == "done_today" and t["can_read_more"]
    r = await client.post("/api/today/more", headers=ha)
    assert r.status_code == 200 and r.json()["segment"]["day_number"] == 2
    t = (await client.get("/api/today", headers=ha)).json()
    assert t["state"] == "to_read" and t["extra"] == "more" and t["segment"]["day_number"] == 2
    assert (await client.get("/api/read/2", headers=ha)).status_code == 200  # следующий можно читать заранее
    assert (await client.get("/api/read/3", headers=ha)).status_code == 403
    run = (await client.get("/api/run", headers=ha)).json()
    assert run["stats"]["segments_done"] == 1 and run["days"][0]["plan_day"] == 1
