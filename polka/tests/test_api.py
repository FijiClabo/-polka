"""API: подпись initData и приватность текстов и пересказов."""

import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import pytest

from api.auth import InitDataError, validate_init_data
from core import clock
from db.models import Run, User
from db.session import session_scope
from services.books import attach_book, confirm_plan, save_parsed
from services.common import Outbox
from services.runs import enroll, grant
from services.social import add_friend_by_code, make_pair
from services.users import get_or_create_user
from tests.helpers import RETELL_OK, install_fake_llm, set_now, setup_db, submit

TOKEN = "123456:TEST-TOKEN"


def make_init_data(user: dict, token: str = TOKEN, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAE", "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_valid_and_tampered():
    data = make_init_data({"id": 42, "first_name": "Аня"})
    assert validate_init_data(data, TOKEN)["user"]["id"] == 42
    with pytest.raises(InitDataError):
        validate_init_data(data.replace("42", "43"), TOKEN)
    with pytest.raises(InitDataError):
        validate_init_data(data, "999:OTHER")
    old = make_init_data({"id": 42}, auth_date=int(time.time()) - 30 * 24 * 3600)
    with pytest.raises(InitDataError):
        validate_init_data(old, TOKEN)


def H(tg_id: int, name: str = "U") -> dict:
    return {"Authorization": "tma " + make_init_data({"id": tg_id, "first_name": name})}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    from settings import get_settings

    get_settings.cache_clear()
    await setup_db(tmp_path)
    install_fake_llm()
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    clock.set_fixed(None)


async def _two_readers_same_book():
    """A и B — напарники, читают одну и ту же книгу из файла. C — друг A."""
    from books.parse import parse_book
    from books.samples import sample_set

    data = sample_set()["01_clean_with_toc.epub"]
    start = clock.now().date() + timedelta(days=1)
    async with session_scope() as s:
        run = Run(title="T", kind="main", start_date=start, status="open", grace_days=3)
        s.add(run)
        await s.flush()
        ids = {}
        for tg, name in ((101, "A"), (102, "B"), (103, "C")):
            u, _ = await get_or_create_user(s, tg, first_name=name)
            enr = await enroll(s, u, run, "invited")
            await grant(s, u, run)
            if tg in (101, 102):
                from db.models import Book

                book = Book(owner_user_id=u.id, source="epub", title="x", parse_status="pending")
                s.add(book)
                await s.flush()
                await save_parsed(s, book, parse_book("b.epub", data), "samehash")
                await attach_book(s, enr, book)
                await confirm_plan(s, u, enr, 21)
            ids[name] = (u.id, enr.id)
        a = await s.get(User, ids["A"][0])
        b = await s.get(User, ids["B"][0])
        c = await s.get(User, ids["C"][0])
        from db.models import Enrollment

        await make_pair(s, run, await s.get(Enrollment, ids["A"][1]), await s.get(Enrollment, ids["B"][1]))
        await add_friend_by_code(s, c, a.friend_code, is_new_user=False, outbox=Outbox())
        return start, {k: v[0] for k, v in ids.items()}, (a.tg_id, b.tg_id, c.tg_id)


async def test_requires_auth(client):
    r = await client.get("/api/me")
    assert r.status_code == 401
    r = await client.get("/api/me", headers={"Authorization": "tma garbage"})
    assert r.status_code == 401


async def test_me_and_today(client):
    r = await client.get("/api/me", headers=H(555, "Новый"))
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Новый"
    r = await client.get("/api/today", headers=H(555))
    assert r.status_code == 200 and r.json()["state"] == "no_run"


async def test_privacy_rules(client):
    set_now(clock.real_now().date())
    start, ids, (ta, tb, tc) = await _two_readers_same_book()
    set_now(start, 10)
    # A сдаёт 2 дня, B — 1 день
    await submit(ids["A"], RETELL_OK)
    await submit(ids["B"], RETELL_OK)
    set_now(start + timedelta(days=1), 10)
    await submit(ids["A"], RETELL_OK)

    # текст книги: свой — можно, только до текущего дня
    r = await client.get("/api/read/1", headers=H(ta))
    assert r.status_code == 200 and r.json()["paragraphs"]
    r = await client.get("/api/read/5", headers=H(ta))
    assert r.status_code == 403
    # у C нет книги — чужой текст не получить никак
    r = await client.get("/api/read/1", headers=H(tc))
    assert r.status_code == 404

    # друг видит статус, но не пересказы
    r = await client.get(f"/api/friends/{ids['A']}", headers=H(tc))
    assert r.status_code == 200
    body = json.dumps(r.json(), ensure_ascii=False)
    assert "уехал из города" not in body
    assert "retelling" not in body.lower()
    # конспект чужой книги недоступен
    async with session_scope() as s:
        from db.models import Enrollment

        a_book = (await s.get(Enrollment, (await s.get(User, ids["A"])).id)).book_id
    r = await client.get(f"/api/shelf/{a_book}", headers=H(tc))
    assert r.status_code == 404
    r = await client.get(f"/api/shelf/{a_book}", headers=H(tb))
    assert r.status_code == 404
    # не друзья — карточка не отдаётся
    r = await client.get(f"/api/friends/{ids['C']}", headers=H(tb))
    assert r.status_code == 404

    # напарник: та же книга → видит пересказ A за день 1, но не за день 2 (сам сдал только день 1)
    r = await client.get("/api/pair", headers=H(tb))
    assert r.status_code == 200
    feed = r.json()["feed"]
    assert r.json()["same_book"] is True
    assert feed[0]["locked"] is False and "уехал" in feed[0]["text"]
    assert feed[1]["locked"] is True and feed[1]["text"] is None


async def test_retell_via_api(client):
    set_now(clock.real_now().date())
    start, ids, (ta, tb, tc) = await _two_readers_same_book()
    set_now(start, 10)
    r = await client.post("/api/retell", json={"text": "коротко"}, headers=H(ta))
    assert r.json()["status"] == "too_short"
    r = await client.post("/api/retell", json={"text": RETELL_OK}, headers=H(ta))
    j = r.json()
    assert j["status"] == "accepted" and j["streak"] == 1
    r = await client.get("/api/today", headers=H(ta))
    assert r.json()["state"] == "done_today"
    r = await client.get("/api/run", headers=H(ta))
    assert r.json()["days"][0]["state"] == "done"
    r = await client.get("/api/run/day/1", headers=H(ta))
    assert r.json()["retelling"]["verdict"] == "accepted"


async def test_paper_book_and_plan_options(client):
    set_now(clock.real_now().date())
    async with session_scope() as s:
        s.add(Run(title="T", kind="main", start_date=clock.now().date() + timedelta(days=3), status="open"))
    h = H(777, "Бумага")
    await client.get("/api/me", headers=h)
    r = await client.post("/api/book/paper", json={"title": "Война и мир", "author": "Толстой", "pages": 1300}, headers=h)
    assert r.status_code == 200
    r = await client.get("/api/book/plan-options", headers=h)
    opts = r.json()["options"]
    assert [o["days"] for o in opts] == [60] and opts[0]["warning"]
    r = await client.post("/api/book/plan", json={"days": 30}, headers=h)
    assert r.status_code == 400
    r = await client.post("/api/book/plan", json={"days": 60}, headers=h)
    assert r.status_code == 200 and r.json()["awaiting_payment"] is True


class _InvoiceBot:
    def __init__(self):
        self.links: list[dict] = []

    async def create_invoice_link(self, **kw):
        self.links.append(kw)
        return "https://t.me/$invoice-test"


async def test_billing_api_paywall_promo_invoice(client):
    from api.routes import set_bot
    from db.models import PromoCode

    set_now(clock.real_now().date(), 10)
    h = H(888, "Покупатель")
    await client.get("/api/me", headers=h)
    r = await client.post("/api/book/paper", json={"title": "Идиот", "author": "Достоевский", "pages": 640}, headers=h)
    assert r.status_code == 200
    days = (await client.get("/api/book/plan-options", headers=h)).json()["options"][0]["days"]
    r = await client.post("/api/book/plan", json={"days": days}, headers=h)
    assert r.json()["awaiting_payment"] is True
    assert (await client.get("/api/today", headers=h)).json()["state"] == "awaiting_payment"

    b = (await client.get("/api/billing", headers=h)).json()
    assert b["needs_access"] is True and b["plan_days"] == days and b["book"]["title"] == "Идиот"
    assert b["methods"]["stars"] is True and b["prices"]["run"]["stars"] > 0

    r = await client.post("/api/billing/promo", json={"code": "nope"}, headers=h)
    assert r.status_code == 404
    async with session_scope() as s:
        s.add(PromoCode(code="READ30", discount_percent=30, products="run,month", active=True, used=0))
    b = (await client.post("/api/billing/promo", json={"code": "read30"}, headers=h)).json()
    assert b["promo"] == "READ30" and b["prices"]["run"]["discount"] == 30

    fake = _InvoiceBot()
    set_bot(fake)
    try:
        r = await client.post("/api/billing/invoice", json={"product": "run", "method": "stars"}, headers=h)
        assert r.status_code == 200 and r.json()["url"].startswith("https://t.me/")
        assert fake.links[-1]["currency"] == "XTR" and fake.links[-1]["prices"][0].amount == b["prices"]["run"]["stars"]
        r = await client.post("/api/billing/invoice", json={"product": "run", "method": "card"}, headers=h)
        assert r.status_code == 400  # ЮKassa не подключена
        r = await client.post("/api/billing/invoice", json={"product": "boat", "method": "stars"}, headers=h)
        assert r.status_code == 422
    finally:
        set_bot(None)

    # чужой не видит ни покупок, ни промокода
    other = (await client.get("/api/billing", headers=H(889, "Другой"))).json()
    assert other["promo"] is None and other["purchases"] == []


async def test_public_pages(client):
    r = await client.get("/robots.txt")
    assert r.status_code == 200 and "Disallow: /api/" in r.text
    r = await client.get("/offer")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "{{" not in r.text
