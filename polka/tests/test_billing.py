"""Продажи: цены и промокоды, оплата, активация забега, абонемент, гарантия возврата."""

from __future__ import annotations

import json
from datetime import date, time, timedelta

import pytest
from sqlalchemy import func, select

from core import clock
from db.models import PromoCode, Purchase
from db.session import session_scope
from services import billing
from services.books import add_paper_book, confirm_plan
from services.common import Outbox
from services.progress import load_view
from services.runs import ensure_enrollment
from services.users import get_or_create_user
from settings import get_settings
from tests.helpers import install_fake_llm, set_now, setup_db

TODAY = date(2026, 10, 7)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TG_IDS", "900")
    get_settings.cache_clear()
    await setup_db(tmp_path)
    install_fake_llm()
    set_now(TODAY, 10)
    yield
    clock.set_fixed(None)
    get_settings.cache_clear()


async def _user(s, tg_id: int = 700):
    user, _ = await get_or_create_user(s, tg_id, first_name="Оля")
    user.timezone = "Europe/Moscow"
    return user


async def _with_plan(s, user, days: int = 21):
    enr = await ensure_enrollment(s, user)
    await add_paper_book(s, user, enr, "Книга", "Автор", 210)
    await confirm_plan(s, user, enr, days)
    return enr


async def _pay(s, user, product="run", *, charge="ch1", provider="stars", promo=None, **kw):
    prices = await billing.prices_for(s, user)
    pr = prices[product]
    currency = "XTR" if provider == "stars" else "RUB"
    amount = pr.stars if provider == "stars" else pr.rub * 100
    return await billing.record_payment(
        s, user, product=product, provider=provider, currency=currency, amount=amount, telegram_charge_id=charge,
        provider_charge_id="yk-1" if provider == "yookassa" else None, promo=promo, outbox=kw.pop("outbox", None), **kw,
    )


class FakeBot:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.refunds: list[str] = []
        self.cancelled: list[str] = []

    async def refund_star_payment(self, user_id, telegram_payment_charge_id):
        self.refunds.append(telegram_payment_charge_id)
        return self.ok

    async def edit_user_star_subscription(self, user_id, telegram_payment_charge_id, is_canceled):
        self.cancelled.append(telegram_payment_charge_id)
        return True


# --------------------------------------------------------------------------- путь «книга → план → оплата»


async def test_plan_waits_for_payment(db):
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        assert enr.status == "invited" and enr.plan_start_date is None
        assert (await load_view(s, user, enr)).state == "awaiting_payment"
        assert enr.run.kind == "solo"


async def test_payment_starts_today_and_is_idempotent(db):
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        p = await _pay(s, user)
        assert enr.status == "paid" and enr.access == "credit" and enr.purchase_id == p.id
        assert enr.plan_start_date == TODAY
        assert user.run_credits == 0
        again = await _pay(s, user)  # Telegram может прислать то же событие ещё раз
        assert again.id == p.id
        assert user.run_credits == 0
        assert await s.scalar(select(func.count(Purchase.id))) == 1
        assert (await load_view(s, user, enr)).state == "to_read"


async def test_evening_payment_starts_tomorrow(db):
    set_now(TODAY, 20)
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        await _pay(s, user)
        assert enr.plan_start_date == TODAY + timedelta(days=1)


async def test_credit_bought_before_book(db):
    async with session_scope() as s:
        user = await _user(s)
        await _pay(s, user)
        assert user.run_credits == 1
        enr = await _with_plan(s, user)
        assert enr.status == "paid" and enr.access == "credit"
        assert user.run_credits == 0


async def test_subscription_covers_next_runs(db):
    async with session_scope() as s:
        user = await _user(s)
        await _pay(s, user, "month", charge="m1")
        assert billing.subscription_active(user)
        until = user.subscription_until
        enr = await _with_plan(s, user)
        assert enr.status == "paid" and enr.access == "subscription"
        assert enr.freezes_per_week == get_settings().sub_freezes_per_week
        # год поверх месяца продлевает, а не обнуляет
        await _pay(s, user, "year", charge="y1")
        assert user.subscription_until >= until + timedelta(days=364)


async def test_star_subscription_renewal_keeps_promo_count(db):
    async with session_scope() as s:
        s.add(PromoCode(code="BLOG20", discount_percent=20, products="run,month,year", active=True, used=0))
        user = await _user(s)
        user.promo_code = "BLOG20"
        await _pay(s, user, "month", charge="m1", promo="BLOG20", is_recurring=True)
        await _pay(s, user, "month", charge="m2", promo="BLOG20", is_recurring=True, renewal=True)
        code = await s.get(PromoCode, "BLOG20")
        assert code.used == 1
        assert user.subscription_recurring


# --------------------------------------------------------------------------- промокоды и проверка перед оплатой


async def test_promo_prices_and_pre_checkout(db):
    st = get_settings()
    async with session_scope() as s:
        s.add(PromoCode(code="BLOG20", discount_percent=20, products="run", active=True, used=0))
        user = await _user(s)
        user.promo_code = "blog20"
        prices = await billing.prices_for(s, user)
        assert prices["run"].rub == round(st.price_run_rub * 0.8) and prices["run"].promo == "BLOG20"
        assert prices["month"].rub == st.price_month_rub and prices["month"].promo is None
        inv = await billing.build_invoice(s, user, "run", "stars")
        assert inv.currency == "XTR" and inv.amount == prices["run"].stars
        assert await billing.check_pre_checkout(s, user.tg_id, inv.payload, "XTR", inv.amount) is None
        assert await billing.check_pre_checkout(s, user.tg_id, inv.payload, "XTR", inv.amount + 1)
        assert await billing.check_pre_checkout(s, 12345, inv.payload, "XTR", inv.amount)
        assert await billing.check_pre_checkout(s, user.tg_id, "garbage", "XTR", inv.amount)
        await _pay(s, user, promo="BLOG20")
        assert (await s.get(PromoCode, "BLOG20")).used == 1
        assert user.promo_code is None


async def test_promo_limits(db):
    async with session_scope() as s:
        s.add_all([
            PromoCode(code="FULL", discount_percent=10, products="run", active=True, used=5, max_uses=5),
            PromoCode(code="OFF", discount_percent=10, products="run", active=False, used=0),
            PromoCode(code="OLD", discount_percent=10, products="run", active=True, used=0,
                      expires_at=clock.now() - timedelta(days=1)),
            PromoCode(code="OK", discount_percent=10, products="run", active=True, used=0),
        ])
        await s.flush()
        for code in ("FULL", "OFF", "OLD", "NOPE", ""):
            assert await billing.valid_promo(s, code) is None
        assert (await billing.valid_promo(s, " ok ")).code == "OK"


async def test_free_promo_opens_access(db):
    async with session_scope() as s:
        s.add(PromoCode(code="FRIEND", discount_percent=100, products="run", active=True, used=0))
        user = await _user(s)
        enr = await _with_plan(s, user)
        assert await billing.apply_free_promo(s, user) is None  # без промокода бесплатно нельзя
        user.promo_code = "FRIEND"
        p = await billing.apply_free_promo(s, user)
        assert p is not None and p.provider == "promo" and p.amount == 0
        assert enr.status == "paid"


def test_payload_roundtrip():
    payload = billing.make_payload("month", 42, "BLOG20")
    assert billing.parse_payload(payload) == ("month", 42, "BLOG20")
    assert billing.parse_payload(billing.make_payload("run", 7, None)) == ("run", 7, None)
    for bad in ("", "v1:run", "v2:run:1:-:x", "v1:car:1:-:x", "v1:run:abc:-:x"):
        assert billing.parse_payload(bad) is None


def test_receipt_for_54fz():
    data = json.loads(billing.receipt_provider_data("run", 990))
    item = data["receipt"]["items"][0]
    assert item["amount"] == {"value": "990.00", "currency": "RUB"}
    assert item["payment_subject"] == "service" and item["payment_mode"] == "full_payment"


# --------------------------------------------------------------------------- гарантия возврата


async def test_star_refund_is_automatic_and_once(db):
    bot = FakeBot()
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        await _pay(s, user, charge="st-1")
        assert (await billing.refund_check(s, user)).eligible
        assert await billing.request_refund(s, user, bot) == "ok"
        assert bot.refunds == ["st-1"]
        assert enr.status == "refunded"
        p = await s.scalar(select(Purchase))
        assert p.status == "refunded" and p.refunded_at is not None
        # вторая покупка — гарантия уже использована
        enr2 = await ensure_enrollment(s, user)
        assert enr2.id != enr.id
        await add_paper_book(s, user, enr2, "Другая", "Автор", 210)
        await confirm_plan(s, user, enr2, 21)
        await _pay(s, user, charge="st-2")
        chk = await billing.refund_check(s, user)
        assert not chk.eligible and "один раз" in chk.reason


async def test_refund_window_closes(db):
    async with session_scope() as s:
        user = await _user(s)
        await _with_plan(s, user)
        await _pay(s, user)
    set_now(TODAY + timedelta(days=get_settings().refund_days + 1), 10)
    async with session_scope() as s:
        user = await _user(s)
        chk = await billing.refund_check(s, user)
        assert not chk.eligible


async def test_card_refund_without_api_keys_goes_to_admin(db):
    outbox = Outbox()
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        p = await _pay(s, user, provider="yookassa", charge="yk-charge")
        assert p.currency == "RUB" and p.amount == get_settings().price_run_rub * 100
        assert await billing.request_refund(s, user, None, outbox) == "requested"
        assert p.status == "refund_requested"
        assert any("/refund_done" in m.text for m in outbox.messages)
        assert enr.status == "paid"  # доступ закрывается, когда деньги действительно вернули
        await billing.mark_refunded(s, p.id)
        assert p.status == "refunded" and enr.status == "refunded"


async def test_subscription_refund_cancels_and_closes_access(db):
    bot = FakeBot()
    async with session_scope() as s:
        user = await _user(s)
        await _pay(s, user, "month", charge="m1", is_recurring=True)
        enr = await _with_plan(s, user)
        assert enr.access == "subscription"
        assert await billing.request_refund(s, user, bot) == "ok"
        assert bot.cancelled == ["m1"]
        assert not billing.subscription_active(user)
        assert enr.status == "refunded"


async def test_sales_summary(db):
    async with session_scope() as s:
        a = await _user(s, 701)
        b = await _user(s, 702)
        await _pay(s, a, charge="a1")
        await _pay(s, b, "month", charge="b1", provider="yookassa")
        summary = await billing.sales_summary(s)
        assert summary["count"] == 2 and summary["payers"] == 2
        assert summary["rub"] == get_settings().price_month_rub
        assert summary["stars"] == get_settings().price_run_stars
        assert a.run_credits == 1  # книги ещё нет — забег ждёт на счету


async def test_sales_reminders_once(db):
    from jobs.scheduler import _sales

    async with session_scope() as s:
        user = await _user(s)
        user.morning_time = time(8, 0)
        await _with_plan(s, user)
        sub_user = await _user(s, 701)
        sub_user.morning_time = user.morning_time
        await _pay(s, sub_user, "year", charge="y-1", provider="yookassa")
        sub_user.subscription_until = clock.now() + timedelta(days=5)
    set_now(TODAY + timedelta(days=1), 10)
    out = Outbox()
    await _sales(out)
    kinds = [m.kind for m in out.messages]
    assert kinds.count("paywall") == 1 and kinds.count("sub") == 1
    out2 = Outbox()
    await _sales(out2)
    assert out2.messages == []  # каждое — один раз
    set_now(TODAY + timedelta(days=4), 10)
    out3 = Outbox()
    await _sales(out3)
    assert [m.kind for m in out3.messages] == ["sub"]  # второе — за день до конца
