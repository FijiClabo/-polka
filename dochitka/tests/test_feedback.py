"""Опросы, отзывы, учёт действий и отчёт для разбора."""

import io
import zipfile
from datetime import timedelta

from sqlalchemy import select

from core import clock
from db.models import Event, Feedback, Run, User
from db.session import session_scope
from services.common import Outbox
from services.feedback import add_text, answer
from services.report import build_report, mask, people_pattern
from tests.helpers import install_fake_llm, make_participant, set_now, setup_db, submit
from tests.test_api import H, client  # noqa: F401 — фикстура клиента API
from tests.test_bot_flow import env  # noqa: F401 — фикстура бота

START = clock.real_now().date() + timedelta(days=3)


async def _reader(tmp_path):
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        run = Run(title="Забег", kind="main", start_date=START, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        u, e = await make_participant(s, run, 9001, "Нина", pages=210, days=21)
        u.tg_username = "nina_reads"
        return u.id, e.id


async def test_surveys_after_day3_and_week(tmp_path, monkeypatch):
    from settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_tg_ids", "777")
    uid, _ = await _reader(tmp_path)
    texts_sent = []
    for i in range(7):
        set_now(START + timedelta(days=i), 10)
        res, out = await submit(uid)
        assert res.status == "accepted"
        texts_sent.append([m.text for m in out.messages if m.kind == "survey"])
    assert any("Три дня позади" in t for t in texts_sent[2])  # после третьего сданного дня
    assert any("Неделя с Дочиткой" in t for t in texts_sent[6])
    assert not any(texts_sent[i] for i in (0, 1, 3, 4, 5))
    # повторно не спрашиваем
    set_now(START + timedelta(days=7), 10)
    _, out = await submit(uid)
    assert not [m for m in out.messages if m.kind == "survey"]

    # ответ кнопкой и пара слов — уходит ведущему вместе с @username
    async with session_scope() as s:
        u = await s.get(User, uid)
        fb = await answer(s, u, "day3", "4")
        assert fb.score == 4
        assert await answer(s, u, "day3", "9") is None  # такого варианта нет
        out = Outbox()
        await add_text(s, u, "day3", "Хочу напоминание позже, мой номер +7 999 123-45-67", fb.id, out)
        assert out.messages[0].tg_id == 777 and "Нина (@nina_reads)" in out.messages[0].text
        assert "оценка 4" in out.messages[0].text
        rows = list(await s.scalars(select(Feedback).where(Feedback.user_id == uid)))
        assert len(rows) == 1 and rows[0].text.startswith("Хочу")


def test_mask_hides_contacts():
    t = mask("пиши @nina_reads или nina@mail.ru, +7 (999) 123-45-67, https://t.me/x")
    assert "nina" not in t and "999" not in t and "t.me" not in t


def test_mask_hides_participant_names_in_any_case():
    users = [User(tg_id=900111222, first_name="Нина", last_name="Иванова", tg_username="nina_reads"),
             User(tg_id=900333444, first_name="Пётр")]
    t = mask("Я Нина Иванова, тг nina_reads, id 900111222. Передайте Ниной Ивановой привет, и Петру тоже", people_pattern(users))
    for leak in ("Нин", "Иванов", "nina", "900111222", "Петр"):
        assert leak not in t, t
    assert "привет" in t and "Передайте" in t


async def test_report_is_pseudonymous(tmp_path):
    uid, _ = await _reader(tmp_path)
    for i in range(3):
        set_now(START + timedelta(days=i), 10)
        await submit(uid)
    async with session_scope() as s:
        u = await s.get(User, uid)
        fb = await answer(s, u, "day3", "5")
        await add_text(s, u, "day3", "Всё отлично, напишите мне @nina_reads", fb.id, None)
        s.add(Event(user_id=uid, type="screen", payload={"screen": "pace"}))
        s.add(Event(user_id=uid, type="bot_command", payload={"name": "pace"}))
        data, caption = await build_report(s)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert set(zf.namelist()) == {"README.txt", "report.md", "users.csv", "events.csv", "feedback.csv"}
    everything = "".join(zf.read(n).decode("utf-8-sig") for n in zf.namelist())
    assert "Нина" not in everything and "nina_reads" not in everything and "9001" not in everything
    md = zf.read("report.md").decode()
    assert "## 1. Воронка" in md and "## 10. Пути участников" in md and "сдано ×3" in md
    assert "Первые 3 дня (1–5): 1 ответов, среднее 5" in md and "@[ник]" in md
    assert "pace 1" in md and "/pace 1" in md
    assert "отзывов" in caption


async def test_events_feedback_and_report_api(client, monkeypatch):  # noqa: F811
    set_now(clock.real_now().date(), 10)
    h = H(9101, "Оля")
    await client.get("/api/me", headers=h)
    assert (await client.post("/api/events", json={"type": "screen", "screen": "pace"}, headers=h)).status_code == 200
    assert (await client.post("/api/events", json={"type": "tap", "screen": "today", "target": "partial_link"},
                              headers=h)).status_code == 200
    assert (await client.post("/api/events", json={"type": "day_done"}, headers=h)).status_code == 422  # серверные — нельзя
    assert (await client.post("/api/events", json={"type": "tap", "target": "<script>"}, headers=h)).status_code == 422
    assert (await client.post("/api/feedback", json={"text": "Классно"}, headers=h)).status_code == 403  # без согласия
    await client.post("/api/consent", headers=h)
    assert (await client.post("/api/feedback", json={"text": "Классно, но хочу тёмную тему"}, headers=h)).status_code == 200
    assert (await client.post("/api/admin/report", headers=h)).status_code == 403  # только ведущему
    async with session_scope() as s:
        types = [e.type for e in await s.scalars(select(Event).order_by(Event.id))]
        assert "screen" in types and "tap" in types and "feedback" in types


async def test_retelling_after_an_unanswered_survey_is_not_swallowed(env):  # noqa: F811
    """Опрос остался без ответа, а вечером приходит пересказ — он засчитывается, а не уходит в отзывы."""
    from db.models import Retelling
    from services.consent import give_consent
    from tests.helpers import RETELL_OK
    from tests.test_bot_flow import press, send_text

    bot, dp, session = env
    set_now(START - timedelta(days=1))
    async with session_scope() as s:
        run = Run(title="Забег", kind="main", start_date=START, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        u, _ = await make_participant(s, run, 701, "Саша", pages=210, days=21)
        u.onboarding_step = "done"
        await give_consent(s, u, "bot")
    set_now(START, 10)
    await press(bot, dp, 701, "fb:day3:4")
    set_now(START, 20)
    await send_text(bot, dp, 701, RETELL_OK)
    async with session_scope() as s:
        assert len(list(await s.scalars(select(Retelling)))) == 1
        assert all(f.text is None for f in await s.scalars(select(Feedback)))
    # а в первые полчаса ответ — это отзыв
    await press(bot, dp, 701, "fb:pmf:very")
    await send_text(bot, dp, 701, "Удобно, что можно голосом")
    async with session_scope() as s:
        assert any(f.text == "Удобно, что можно голосом" for f in await s.scalars(select(Feedback)))
    # команда во время вопроса работает как команда
    await press(bot, dp, 701, "fb:finish:9")
    await send_text(bot, dp, 701, "/today")
    assert "День 1 из 21" in session.texts()[-1] or "сдано" in session.texts()[-1]
