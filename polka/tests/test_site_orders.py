"""Оплата на сайте (ЮKassa) и коды активации; отмена автопродления; возвраты за неиспользованную часть."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from core import clock
from db.models import Consent, PromoCode, Purchase
from db.session import session_scope
from services import billing, site_orders
from services.books import add_paper_book, confirm_plan
from services.common import Outbox
from services.runs import ensure_enrollment
from services.users import get_or_create_user
from settings import get_settings
from tests.helpers import install_fake_llm, set_now, setup_db

TODAY = date(2026, 10, 7)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TG_IDS", "900")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "123")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "test_secret")
    get_settings.cache_clear()
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(TODAY, 10)
    yield
    clock.set_fixed(None)
    get_settings.cache_clear()


class FakeYooKassa:
    """Подменяет API ЮKassa: хранит платежи и отдаёт их статус."""

    def __init__(self, monkeypatch):
        self.payments: dict[str, dict] = {}
        self.refunds: list[dict] = []
        self.n = 0

        async def create_payment(p, return_url):
            self.n += 1
            pid = f"pay-{self.n}"
            p.provider_charge_id = pid
            self.payments[pid] = {"id": pid, "status": "pending",
                                  "amount": {"value": f"{p.amount / 100:.2f}", "currency": "RUB"}}
            return f"https://yoomoney.ru/checkout/{pid}"

        async def fetch_payment(pid):
            return self.payments.get(pid)

        async def yk_refund(p, amount_kop=None):
            self.refunds.append({"payment": p.provider_charge_id, "amount": amount_kop})
            return True

        monkeypatch.setattr(site_orders, "create_payment", create_payment)
        monkeypatch.setattr(site_orders, "fetch_payment", fetch_payment)
        monkeypatch.setattr(billing, "yookassa_refund", yk_refund)

    def succeed(self, pid: str) -> None:
        self.payments[pid]["status"] = "succeeded"


async def _order(s, product="run", promo=None, email="reader@example.com"):
    p = await site_orders.create_order(s, product=product, email=email, promo=promo, source="vk", agree_offer=True,
                                       agree_pd=True)
    await site_orders.create_payment(p, "https://x/pay/done")
    return p


async def _user(s, tg_id=800):
    user, _ = await get_or_create_user(s, tg_id, first_name="Ира")
    user.timezone = "Europe/Moscow"
    return user


async def test_order_validation(db):
    async with session_scope() as s:
        for kw, msg in (
            ({"email": "not-an-email"}, "e-mail"),
            ({"agree_pd": False}, "согласие"),
            ({"product": "boat"}, "тариф"),
        ):
            args = {"product": "run", "email": "a@b.ru", "promo": None, "source": None, "agree_offer": True, "agree_pd": True}
            args.update(kw)
            with pytest.raises(site_orders.OrderError, match=msg):
                await site_orders.create_order(s, **args)


async def test_site_payment_code_and_redeem(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    out = Outbox()
    async with session_scope() as s:
        p = await _order(s)
        assert p.status == "pending" and p.amount == get_settings().price_run_rub * 100 and p.source == "site:vk"
        consents = list(await s.scalars(select(Consent).where(Consent.purchase_id == p.id)))
        assert {c.doc_type for c in consents} == {"offer", "pd"}
        # поддельное уведомление «оплачено», пока платёж в ЮKassa не оплачен, ничего не выдаёт
        fake = {"event": "payment.succeeded", "object": {"id": p.provider_charge_id, "status": "succeeded"}}
        assert await site_orders.handle_notification(s, fake, out) is None
        assert p.status == "pending" and p.activation_code is None
        yk.succeed(p.provider_charge_id)
        paid = await site_orders.handle_notification(s, fake, out)
        assert paid is p and p.status == "paid" and len(p.activation_code) == 10
        assert any("заказ с сайта" in m.text for m in out.messages)
        # повторное уведомление — без нового кода
        code = p.activation_code
        assert await site_orders.handle_notification(s, fake, out) is None
        assert p.activation_code == code

        user = await _user(s)
        enr = await ensure_enrollment(s, user)
        await add_paper_book(s, user, enr, "Книга", "Автор", 210)
        await confirm_plan(s, user, enr, 21)
        assert enr.status == "invited"
        status, got = await billing.redeem_code(s, user, f" {code[:5].lower()}-{code[5:]} ")
        assert status == "ok" and got.id == p.id and p.user_id == user.id
        assert enr.status == "paid" and enr.plan_start_date == TODAY
        assert user.source == "site:vk"
        assert (await billing.redeem_code(s, user, code))[0] == "already"
        other = await _user(s, 801)
        assert (await billing.redeem_code(s, other, code))[0] == "used"
        assert (await billing.redeem_code(s, other, "ZZZZZZZZZZ"))[0] == "not_found"


async def test_canceled_and_amount_mismatch(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        p = await _order(s)
        yk.payments[p.provider_charge_id]["status"] = "canceled"
        await site_orders.handle_notification(s, {"event": "payment.canceled", "object": {"id": p.provider_charge_id}})
        assert p.status == "canceled"
        q = await _order(s)
        yk.succeed(q.provider_charge_id)
        yk.payments[q.provider_charge_id]["amount"]["value"] = "1.00"
        await site_orders.handle_notification(s, {"event": "payment.succeeded", "object": {"id": q.provider_charge_id}})
        assert q.status == "pending" and q.activation_code is None


async def test_site_promo_and_expired_code(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        s.add(PromoCode(code="VK30", discount_percent=30, products="run,year", active=True, used=0))
        await s.flush()
        p = await _order(s, "year", promo="vk30")
        assert p.promo_code == "VK30" and p.amount == round(get_settings().price_year_rub * 0.7) * 100
        yk.succeed(p.provider_charge_id)
        await site_orders.refresh_order(s, p.order_id)
        assert p.status == "paid" and (await s.get(PromoCode, "VK30")).used == 1
        code = p.activation_code
    set_now(TODAY + timedelta(days=get_settings().code_valid_days + 1))
    async with session_scope() as s:
        user = await _user(s)
        assert (await billing.redeem_code(s, user, code))[0] == "expired"


async def test_site_refund_by_guarantee_after_activation(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        p = await _order(s, "month")
        yk.succeed(p.provider_charge_id)
        await site_orders.refresh_order(s, p.order_id)
        user = await _user(s)
        await billing.redeem_code(s, user, p.activation_code)
        assert billing.subscription_active(user) and not user.subscription_recurring
        assert await billing.request_refund(s, user, None) == "ok"
        assert yk.refunds == [{"payment": p.provider_charge_id, "amount": None}]
        assert p.status == "refunded" and not billing.subscription_active(user)


async def test_partial_refund_request_and_approval(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    out = Outbox()
    async with session_scope() as s:
        p = await _order(s)
        yk.succeed(p.provider_charge_id)
        await site_orders.refresh_order(s, p.order_id)
        user = await _user(s)
        enr = await ensure_enrollment(s, user)
        await add_paper_book(s, user, enr, "Книга", "Автор", 300)
        await confirm_plan(s, user, enr, 30)
        await billing.redeem_code(s, user, p.activation_code)
        assert enr.status == "paid"
    set_now(TODAY + timedelta(days=10))  # прошло 10 дней из 30 — гарантия закончилась
    async with session_scope() as s:
        user = await _user(s)
        chk = await billing.refund_check(s, user)
        assert not chk.eligible and chk.partial
        assert await billing.request_refund(s, user, None, out) == "requested_partial"
        p = await s.scalar(select(Purchase))
        assert p.status == "refund_requested"
        quote = await billing.refund_quote(s, user, p)
        assert quote == round(p.amount * 20 / 30)
        assert any("/refund_ok" in m.text for m in out.messages)
        assert await billing.approve_refund(s, p.id, None, quote // 100) == "ok"
        assert yk.refunds[-1]["amount"] == (quote // 100) * 100
        assert p.status == "refunded"


async def test_month_stars_subscription_ignores_promo_and_cancels(db):
    class Bot:
        def __init__(self):
            self.calls = []

        async def edit_user_star_subscription(self, **kw):
            self.calls.append(kw)
            return True

    async with session_scope() as s:
        s.add(PromoCode(code="ALL20", discount_percent=20, products="run,month,year", active=True, used=0))
        user = await _user(s)
        user.promo_code = "ALL20"
        await s.flush()
        prices = await billing.prices_for(s, user)
        assert prices["month"].stars == get_settings().price_month_stars  # продлевается по полной цене
        assert prices["run"].stars < get_settings().price_run_stars
        inv = await billing.build_invoice(s, user, "month", "stars")
        assert inv.subscription_period and billing.parse_payload(inv.payload)[2] is None
        assert await billing.check_pre_checkout(s, user.tg_id, inv.payload, "XTR", inv.amount) is None
        await billing.record_payment(s, user, product="month", provider="stars", currency="XTR", amount=inv.amount,
                                     telegram_charge_id="first-charge", is_recurring=True)
        await billing.record_payment(s, user, product="month", provider="stars", currency="XTR", amount=inv.amount,
                                     telegram_charge_id="renew-1", is_recurring=True, renewal=True,
                                     subscription_expiration=clock.now() + timedelta(days=60))
        assert user.star_sub_charge_id == "first-charge"
        bot = Bot()
        assert await billing.cancel_subscription(s, user, bot) == "ok"
        assert bot.calls[0]["telegram_payment_charge_id"] == "first-charge" and bot.calls[0]["is_canceled"]
        assert not user.subscription_recurring and billing.subscription_active(user)
        assert await billing.cancel_subscription(s, user, bot) == "not_recurring"


async def test_star_refund_event_revokes(db):
    async with session_scope() as s:
        user = await _user(s)
        enr = await ensure_enrollment(s, user)
        await add_paper_book(s, user, enr, "Книга", "Автор", 210)
        await confirm_plan(s, user, enr, 21)
        await billing.record_payment(s, user, product="run", provider="stars", currency="XTR", amount=650,
                                     telegram_charge_id="ch-x")
        assert enr.status == "paid"
        await billing.on_star_refunded(s, "ch-x")
        assert enr.status == "refunded"
        assert await billing.on_star_refunded(s, "unknown") is None


async def test_public_pay_routes(db, monkeypatch, tmp_path):
    yk = FakeYooKassa(monkeypatch)
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/pay/create", json={"product": "run", "email": "x@y.ru", "agree_offer": True, "agree_pd": False})
        assert r.status_code == 400
        r = await c.post("/pay/create", json={"product": "run", "email": "x@y.ru", "agree_offer": True, "agree_pd": True,
                                              "src": "vk"})
        assert r.status_code == 200 and r.json()["url"].startswith("https://yoomoney.ru/")
        order = r.json()["order"]
        r = await c.get(f"/pay/status?order={order}")
        assert r.json()["status"] == "pending" and "code" not in r.json()
        async with session_scope() as s:
            p = await s.scalar(select(Purchase).where(Purchase.order_id == order))
            yk.succeed(p.provider_charge_id)
        r = await c.post("/pay/yookassa", json={"event": "payment.succeeded", "object": {"id": p.provider_charge_id}})
        assert r.status_code == 200
        st = (await c.get(f"/pay/status?order={order}")).json()
        assert st["status"] == "paid" and len(st["code"]) == 10 and st["activated"] is False
        assert (await c.get("/pay/status?order=nothex")).status_code == 404
        prices = (await c.get("/pay/prices")).json()
        assert prices["enabled"] is True and prices["prices"]["run"]["rub"] == get_settings().price_run_rub
