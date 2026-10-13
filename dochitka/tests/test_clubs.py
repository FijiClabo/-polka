"""Книжные клубы, «читать ту же книгу» у друга и запрос в друзья с подтверждением."""

from sqlalchemy import select

from core import clock
from db.models import Book, Club, Enrollment, User
from db.session import session_scope
from services.common import Outbox
from services.social import accept_friend, add_friend_by_code
from services.users import get_or_create_user
from tests.helpers import set_now
from tests.test_api import H, client  # noqa: F401 — фикстура клиента API
from tests.test_together import A_TG, B_TG, _reader_with_plan

C_TG = 303


async def _consent(client, tg, name):  # noqa: F811
    h = H(tg, name)
    await client.get("/api/me", headers=h)
    await client.post("/api/consent", headers=h)
    return h


async def test_club_flow(client):  # noqa: F811
    set_now(clock.real_now().date(), 10)
    await _reader_with_plan()
    ha = await _consent(client, A_TG, "Аня")
    hb = await _consent(client, B_TG, "Боря")
    hc = await _consent(client, C_TG, "Вера")

    # ведущий создаёт клуб на свою книгу и получает ссылку
    assert (await client.get("/api/club", headers=ha)).json()["can_create"] is True
    r = await client.post("/api/club", json={"title": "Клуб по средам", "meet_date": None}, headers=ha)
    assert r.status_code == 200, r.text
    club = r.json()["club"]
    assert club["is_host"] and "?start=c_" in club["link"] and "Клуб по средам" in club["invite_text"]
    code = club["link"].split("?start=c_")[1]

    # админ привязывает промокод клуба
    async with session_scope() as s:
        from db.models import PromoCode

        s.add(PromoCode(code="CLUB100", discount_percent=100, products="run", used=0, active=True))
    async with session_scope() as s:
        c = await s.scalar(select(Club).where(Club.code == code))
        c.promo_code = "CLUB100"

    # участники вступают по ссылке: план ведущего без текста, промокод подставлен
    from services.clubs import join_club

    async with session_scope() as s:
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        out = Outbox()
        assert (await join_club(s, b, code, out))[0] == "ok"
        assert any("теперь в клубе" in m.text for m in out.messages)
        assert b.promo_code == "CLUB100" and b.source == f"club:{code}"
        assert (await join_club(s, b, code, Outbox()))[0] == "already"
        c_user = await s.scalar(select(User).where(User.tg_id == C_TG))
        assert (await join_club(s, c_user, code, Outbox()))[0] == "ok"
        a = await s.scalar(select(User).where(User.tg_id == A_TG))
        assert (await join_club(s, a, code, Outbox()))[0] == "host"
        b_enr = await s.scalar(select(Enrollment).where(Enrollment.user_id == b.id, Enrollment.club_id.is_not(None)))
        b_book = await s.get(Book, b_enr.book_id)
        assert b_book.source == "shared" and not b_book.has_text and b_enr.plan_days == 21

    # все видят список участников с прогрессом; ведущий может напомнить
    j = (await client.get("/api/club", headers=hb)).json()["club"]
    assert j["count"] == 3 and not j["is_host"] and {m["name"] for m in j["members"]} == {"Аня", "Боря", "Вера"}
    assert all(set(m) >= {"progress", "today", "is_friend"} for m in j["members"])
    assert (await client.post("/api/club/nudge", headers=hb)).status_code == 403
    assert (await client.post("/api/club/nudge", headers=ha)).status_code == 200

    # запрос в друзья внутри клуба — с подтверждением
    vera_id = next(m["id"] for m in j["members"] if m["name"] == "Вера")
    r = await client.post("/api/friends/request", json={"user_id": vera_id}, headers=hb)
    assert r.json()["result"] == "ok"
    assert (await client.post("/api/friends/request", json={"user_id": vera_id}, headers=hb)).json()["result"] == "requested"
    async with session_scope() as s:
        vera = await s.get(User, vera_id)
        b = await s.scalar(select(User).where(User.tg_id == B_TG))
        assert await accept_friend(s, vera, b.id, Outbox()) == "ok"
        assert await accept_friend(s, vera, b.id, Outbox()) == "already"

    # участник выходит, ведущий закрывает клуб
    assert (await client.post("/api/club/leave", headers=hc)).status_code == 200
    assert (await client.get("/api/club", headers=hc)).json()["club"] is None
    assert (await client.post("/api/club/leave", headers=ha)).status_code == 200
    assert (await client.get("/api/club", headers=hb)).json()["club"] is None


async def test_friend_request_needs_club_or_pair(client):  # noqa: F811
    hb = await _consent(client, B_TG, "Боря")
    await _consent(client, C_TG, "Вера")
    async with session_scope() as s:
        c = await s.scalar(select(User).where(User.tg_id == C_TG))
    r = await client.post("/api/friends/request", json={"user_id": c.id}, headers=hb)
    assert r.status_code == 403


async def test_friend_same_book(client):  # noqa: F811
    set_now(clock.real_now().date(), 10)
    await _reader_with_plan()
    ha = await _consent(client, A_TG, "Аня")
    hb = await _consent(client, B_TG, "Боря")
    async with session_scope() as s:
        a = await s.scalar(select(User).where(User.tg_id == A_TG))
        b, _ = await get_or_create_user(s, B_TG, first_name="Боря")
        await add_friend_by_code(s, b, a.friend_code, is_new_user=False, outbox=Outbox())
        a_id = a.id
    card = (await client.get(f"/api/friends/{a_id}", headers=hb)).json()
    assert card["can_join_book"] is True
    r = await client.post(f"/api/friends/{a_id}/same-book", headers=hb)
    assert r.status_code == 200 and r.json()["book"]["source"] == "shared"
    assert (await client.get(f"/api/friends/{a_id}", headers=hb)).json()["can_join_book"] is False
    assert (await client.post(f"/api/friends/{a_id}/same-book", headers=hb)).status_code == 409
    assert ha  # ведущий книги не меняется
