"""Проверка безопасности: вход, вебхук, промокоды, ссылки «читать вместе», утечки текста, ручная оплата."""

import time

import pytest

from api.auth import InitDataError, validate_init_data
from core import clock
from db.models import Book, PromoCode, User
from db.session import session_scope
from services.common import Outbox
from tests.helpers import set_now
from tests.test_api import TOKEN, H, client, make_init_data  # noqa: F401 — фикстура клиента API


async def _consented(c, tg, name="U"):
    await c.get("/api/me", headers=H(tg, name))
    assert (await c.post("/api/consent", headers=H(tg, name))).status_code == 200


# ------------------------------------------------------------------ вход в мини-приложение


def test_empty_bot_token_never_validates():
    """С пустым BOT_TOKEN подпись может посчитать кто угодно — такой вход не принимаем вовсе."""
    forged = make_init_data({"id": 1, "first_name": "Админ"}, token="")
    with pytest.raises(InitDataError):
        validate_init_data(forged, "")


def test_init_data_lives_a_day_and_not_from_the_future():
    now = int(time.time())
    with pytest.raises(InitDataError):
        validate_init_data(make_init_data({"id": 42}, auth_date=now - 25 * 3600), TOKEN)
    with pytest.raises(InitDataError):
        validate_init_data(make_init_data({"id": 42}, auth_date=now + 3600), TOKEN)
    assert validate_init_data(make_init_data({"id": 42}, auth_date=now - 23 * 3600), TOKEN)["user"]["id"] == 42


def test_dev_bypass_and_missing_token_refuse_to_start_on_server(monkeypatch):
    from main import check_startup
    from settings import Settings

    with pytest.raises(RuntimeError):
        check_startup(Settings(bot_token="1:x", public_url="https://x.ru", dev_auth_bypass=True))
    with pytest.raises(RuntimeError):
        check_startup(Settings(bot_token="", public_url="https://x.ru"))
    check_startup(Settings(bot_token="", public_url="", dev_auth_bypass=True))  # локальная вёрстка — можно


async def test_dev_bypass_header_ignored_on_server(client, monkeypatch):  # noqa: F811
    from settings import get_settings

    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv("PUBLIC_URL", "https://dochitka.example.ru")
    get_settings.cache_clear()
    try:
        r = await client.get("/api/me", headers={"Authorization": "tma dev:777"})
        assert r.status_code == 401
    finally:
        get_settings.cache_clear()


async def test_webhook_route_is_closed_in_polling_mode(client, monkeypatch):  # noqa: F811
    from settings import get_settings

    monkeypatch.setenv("WEBHOOK_SECRET", "change-me")
    get_settings.cache_clear()
    try:
        update = {"update_id": 1, "message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
                                              "from": {"id": 1, "is_bot": False, "first_name": "x"}, "text": "/report"}}
        r = await client.post("/tg/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "change-me"})
        assert r.status_code == 404
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ промокоды


async def test_promo_works_once_per_person(client):  # noqa: F811
    set_now(clock.real_now().date(), 10)
    async with session_scope() as s:
        s.add(PromoCode(code="PILOT", discount_percent=100, products="month", trial_days=30, max_uses=30,
                        active=True, used=0))
    h = H(5001, "Тестер")
    await _consented(client, 5001, "Тестер")
    assert (await client.post("/api/billing/promo", json={"code": "pilot"}, headers=h)).status_code == 200
    assert (await client.post("/api/billing/free", json={"product": "month"}, headers=h)).status_code == 200
    # второй раз тот же код тому же человеку — нет
    assert (await client.post("/api/billing/promo", json={"code": "pilot"}, headers=h)).status_code == 404
    assert (await client.post("/api/billing/free", json={"product": "month"}, headers=h)).status_code == 400
    async with session_scope() as s:
        assert (await s.get(PromoCode, "PILOT")).used == 1
    # другому тестеру код по-прежнему доступен
    await _consented(client, 5002, "Второй")
    assert (await client.post("/api/billing/promo", json={"code": "pilot"}, headers=H(5002, "Второй"))).status_code == 200


async def test_promo_guessing_is_throttled(client):  # noqa: F811
    set_now(clock.real_now().date(), 10)
    async with session_scope() as s:
        s.add(PromoCode(code="KNIGA20", discount_percent=20, products="run", active=True, used=0))
    h = H(5004, "Угадайка")
    await _consented(client, 5004, "Угадайка")
    codes = [(await client.post("/api/billing/promo", json={"code": f"GUESS{i}"}, headers=h)).status_code for i in range(12)]
    assert codes.count(404) == 10 and codes[-1] == 429
    assert (await client.post("/api/billing/promo", json={"code": "KNIGA20"}, headers=h)).status_code == 429


# ------------------------------------------------------------------ напарник и текст чужой книги


async def test_future_days_do_not_reveal_text_anchors(client):  # noqa: F811
    from books.parse import parse_book
    from books.samples import sample_set
    from services.books import attach_book, confirm_plan, save_parsed
    from services.runs import ensure_enrollment, grant
    from services.users import get_or_create_user

    set_now(clock.real_now().date(), 10)
    await _consented(client, 7001, "Аня")
    async with session_scope() as s:
        a, _ = await get_or_create_user(s, 7001, first_name="Аня")
        enr = await ensure_enrollment(s, a)
        book = Book(owner_user_id=a.id, source="epub", title="x", parse_status="pending")
        s.add(book)
        await s.flush()
        await save_parsed(s, book, parse_book("b.epub", sample_set()["01_clean_with_toc.epub"]), "h1")
        await attach_book(s, enr, book)
        await confirm_plan(s, a, enr, 21)
        await grant(s, a, enr.run)
    h = H(7001, "Аня")
    today = (await client.get("/api/run/day/1", headers=h)).json()
    assert today["segment"]["anchors"]
    assert (await client.get("/api/run/day/5", headers=h)).json()["segment"]["anchors"] is None
    assert (await client.get("/api/today", headers=h)).json()["next_segment"]["anchors"] is None


async def test_pace_changes_are_limited_per_day(client):  # noqa: F811
    set_now(clock.real_now().date(), 10)
    await _consented(client, 7005, "Темп")
    from tests.helpers import make_participant

    async with session_scope() as s:
        from db.models import Run

        run = Run(title="Забег", kind="solo", status="active", grace_days=3)
        s.add(run)
        await s.flush()
        await make_participant(s, run, 7005, "Темп", pages=300, days=30)
    h = H(7005, "Темп")
    codes = [(await client.post("/api/pace", json={"minutes": m}, headers=h)).status_code for m in (10, 15, 20, 25, 30, 40, 10, 15, 20)]
    assert codes[:8] == [200] * 8 and codes[8] == 400


# ------------------------------------------------------------------ ручная оплата


async def test_manual_payment_request_notifies_admin(client, monkeypatch):  # noqa: F811
    import api.routes as routes
    from settings import get_settings

    monkeypatch.setenv("ADMIN_TG_IDS", "900")
    monkeypatch.setenv("PAYMENT_INFO", "Перевод по СБП на номер 8 900 000-00-00, Иван И.")
    monkeypatch.setenv("SUPPORT_CONTACT", "@dochitka_help")
    get_settings.cache_clear()
    sent: list = []

    async def fake_flush(out: Outbox):
        sent.extend(out.messages)

    monkeypatch.setattr(routes, "_flush", fake_flush)
    try:
        await _consented(client, 7010, "Оля")
        async with session_scope() as s:
            u = await s.scalar(__import__("sqlalchemy").select(User).where(User.tg_id == 7010))
            u.tg_username = "olya"
        r = await client.post("/api/billing/manual", json={"product": "run"}, headers=H(7010, "Оля"))
        assert r.status_code == 200
        body = r.json()
        assert body["amount"] == 490 and "СБП" in body["info"] and body["contact_url"] == "https://t.me/dochitka_help"
        assert [m.tg_id for m in sent] == [900] and "/grant @olya run" in sent[0].text
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ документы говорят правду о том, где данные


async def test_documents_say_where_the_data_is(client, monkeypatch):  # noqa: F811
    from settings import get_settings

    get_settings.cache_clear()
    ru = (await client.get("/privacy")).text
    assert "находящихся на территории Российской Федерации" in ru and "тестовый запуск" not in ru
    monkeypatch.setenv("SERVER_COUNTRY", "Нидерланды")
    get_settings.cache_clear()
    try:
        nl = (await client.get("/privacy")).text
        assert "находятся на сервере в стране: Нидерланды" in nl
        assert "находящихся на территории Российской Федерации" not in nl
        consent = (await client.get("/consent")).text
        assert consent.count("Нидерланды") >= 2 and "трансграничную передачу" in consent
        landing = (await client.get("/")).text
        assert "&amp;nbsp;" not in landing and "Оплатить" in landing
    finally:
        get_settings.cache_clear()
