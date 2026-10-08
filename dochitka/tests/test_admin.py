"""Админ-панель: доступ только админам, аналитика, промокоды с пробным доступом, выдача доступа, расходы на ИИ."""

from datetime import timedelta

import httpx
import pytest

from core import clock
from db.session import session_scope
from tests.helpers import RETELL_OK, install_fake_llm, set_now, setup_db
from tests.test_api import TOKEN, H, _two_readers_same_book

ADMIN = 900


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_TG_IDS", str(ADMIN))
    from settings import get_settings

    get_settings.cache_clear()
    await setup_db(tmp_path)
    install_fake_llm()
    from main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    clock.set_fixed(None)
    get_settings.cache_clear()


async def test_admin_only(client):
    assert (await client.get("/api/admin/overview", headers=H(555))).status_code == 403
    r = await client.get("/api/admin/overview", headers=H(ADMIN, "Админ"))
    assert r.status_code == 200
    j = r.json()
    assert j["prices"]["month"] == 490 and j["funnel"][0]["title"] == "Нажали /start"
    assert len(j["daily"]) == 14 and "ai" in j and "retention" in j


async def test_trial_promo_and_grant(client):
    h = H(ADMIN, "Админ")
    await client.get("/api/me", headers=h)
    r = await client.post("/api/admin/promos", json={"code": "try14", "discount": 100, "trial_days": 14,
                                                     "products": ["run", "month"], "max_uses": 5}, headers=h)
    assert r.status_code == 200
    row = r.json()
    assert row["code"] == "TRY14" and row["days"] == 14 and row["products"] == ["month"] and "promo_TRY14" in row["link"]
    assert (await client.post("/api/admin/promos", json={"code": "плохой", "discount": 10}, headers=h)).status_code == 400

    # человек применяет код и получает пробный абонемент ровно на 14 дней
    u = H(701, "Тестер")
    await client.get("/api/me", headers=u)
    await client.post("/api/consent", headers=u)
    b = (await client.post("/api/billing/promo", json={"code": "try14"}, headers=u)).json()
    assert b["prices"]["month"]["free"] and b["prices"]["month"]["trial_days"] == 14
    b = (await client.post("/api/billing/free", json={"product": "month"}, headers=u)).json()
    until = clock.real_now().date() + timedelta(days=14)
    assert b["subscription"]["active"] and b["subscription"]["until"][:10] in (until.isoformat(),
                                                                          (until - timedelta(days=1)).isoformat())

    # выключенный код больше не применяется
    assert (await client.post("/api/admin/promos/TRY14/toggle", headers=h)).json()["active"] is False
    await client.get("/api/me", headers=H(702, "Другой"))
    await client.post("/api/consent", headers=H(702, "Другой"))
    assert (await client.post("/api/billing/promo", json={"code": "try14"}, headers=H(702, "Другой"))).status_code == 404

    # выдача доступа вручную на N дней
    r = await client.post("/api/admin/grant", json={"user": "702", "product": "days", "days": 10}, headers=h)
    assert r.status_code == 200 and r.json()["until"]
    r = await client.post("/api/admin/grant", json={"user": "703", "product": "days", "days": 10}, headers=h)
    assert r.status_code == 404  # 703 ещё не заходил
    r = await client.post("/api/admin/grant", json={"user": "702", "product": "days", "days": 10}, headers=h)
    assert r.status_code == 200 and r.json()["until"]
    r = await client.post("/api/admin/grant", json={"user": "702", "product": "run"}, headers=H(702, "Другой"))
    assert r.status_code == 403  # не админ


async def test_ai_usage_is_counted(client):
    from ai.usage import ai_context, flush, record_llm, record_stt
    from services.analytics import ai_costs

    set_now(clock.real_now().date())
    start, ids, (ta, tb, tc) = await _two_readers_same_book()
    with ai_context(ids["A"], "check"):
        record_llm("yandex", "yandexgpt/latest", False, 5000, 200)  # 5,2 тыс. токенов × 0,8 ₽
        record_stt("yandex", 40)
    record_llm("yandex", "yandexgpt-lite/latest", True, 5000, 300)  # краткое содержание × 0,2 ₽
    assert await flush() == 3
    async with session_scope() as s:
        c = await ai_costs(s)
    assert c["by_kind"]["check"] == pytest.approx(4.16) and c["by_kind"]["summary"] == pytest.approx(1.06)
    assert c["voice_min"] == pytest.approx(0.7)
    j = (await client.get("/api/admin/overview", headers=H(ADMIN, "Админ"))).json()
    assert j["ai"]["total"]["rub"] == pytest.approx(5.22)


async def test_fake_llm_check_records_usage(client):
    """Проверка пересказа через цепочку ИИ кладёт расход в журнал."""
    from ai.usage import take
    from tests.helpers import submit

    take()
    set_now(clock.real_now().date())
    start, ids, _ = await _two_readers_same_book()
    set_now(start, 10)
    await submit(ids["A"], RETELL_OK)
    rows = take()
    assert rows and rows[0]["user_id"] == ids["A"] and rows[0]["kind"] == "check"
