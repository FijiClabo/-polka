"""Оплата через ЮKassa, права доступа, промокоды, напоминания и связанные с ними сценарии забега."""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from sqlalchemy import func, select

from core import clock
from db.models import PromoCode, Purchase
from db.session import session_scope
from services import billing, payments
from services.books import PlanError, add_paper_book, confirm_plan
from services.common import Outbox
from services.progress import load_view
from services.runs import current_enrollment, ensure_enrollment, sprint_block, start_sprint
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
    """Подменяет API ЮKassa: хранит платежи и возвраты, статус меняем вручную."""

    def __init__(self, monkeypatch):
        self.payments: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}

        async def create_payment(p, return_url):
            pid = f"{len(self.payments) + 1:08d}-0000-0000"
            p.provider_charge_id = pid
            self.payments[pid] = {"id": pid, "status": "pending",
                                  "amount": {"value": f"{p.amount / 100:.2f}", "currency": "RUB"}}
            return f"https://yoomoney.ru/checkout/{pid}"

        async def fetch_payment(pid):
            return self.payments.get(pid)

        async def fetch_refund(rid):
            return self.refunds.get(rid)

        monkeypatch.setattr(payments, "create_payment", create_payment)
        monkeypatch.setattr(payments, "fetch_payment", fetch_payment)
        monkeypatch.setattr(payments, "fetch_refund", fetch_refund)

    def succeed(self, pid: str) -> None:
        self.payments[pid]["status"] = "succeeded"


async def _user(s, tg_id: int = 700):
    user, _ = await get_or_create_user(s, tg_id, first_name="Оля")
    user.timezone = "Europe/Moscow"
    return user


async def _with_plan(s, user, days: int = 21, pages: int = 210):
    enr = await ensure_enrollment(s, user)
    await add_paper_book(s, user, enr, "Книга", "Автор", pages)
    await confirm_plan(s, user, enr, days)
    return enr


async def _paid(s, yk, user, product="run", outbox=None):
    """Нажали «Оплатить», ЮKassa подтвердила оплату, пришло уведомление."""
    url, order = await payments.start_payment(s, user, product)
    assert url.startswith("https://yoomoney.ru/")
    p = await s.scalar(select(Purchase).where(Purchase.order_id == order))
    yk.succeed(p.provider_charge_id)
    got = await payments.handle_notification(s, {"event": "payment.succeeded", "object": {"id": p.provider_charge_id}},
                                             outbox)
    assert got is p
    return p


# --------------------------------------------------------------------------- путь «книга → план → оплата»


async def test_plan_waits_for_payment(db):
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        assert enr.status == "invited" and enr.plan_start_date is None
        assert (await load_view(s, user, enr)).state == "awaiting_payment"
        assert enr.run.kind == "solo"


async def test_payment_starts_today_and_notification_is_idempotent(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    out = Outbox()
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        p = await _paid(s, yk, user, outbox=out)
        assert p.status == "paid" and p.user_id == user.id and p.amount == get_settings().price_run_rub * 100
        assert enr.status == "paid" and enr.access == "credit" and enr.purchase_id == p.id
        assert enr.plan_start_date == TODAY and user.run_credits == 0
        assert any("Оплата" in m.text for m in out.messages)  # админу
        # повторное уведомление ничего не меняет
        again = await payments.handle_notification(s, {"event": "payment.succeeded", "object": {"id": p.provider_charge_id}})
        assert again is None and user.run_credits == 0
        assert await s.scalar(select(func.count(Purchase.id))) == 1


async def test_forged_notification_grants_nothing(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        await _with_plan(s, user)
        _url, order = await payments.start_payment(s, user, "run")
        p = await s.scalar(select(Purchase).where(Purchase.order_id == order))
        # уведомление «оплачено», а в ЮKassa платёж ещё не оплачен
        await payments.handle_notification(s, {"event": "payment.succeeded", "object": {"id": p.provider_charge_id}})
        assert p.status == "pending" and user.run_credits == 0
        # сумма в ЮKassa не совпадает с заказом
        yk.succeed(p.provider_charge_id)
        yk.payments[p.provider_charge_id]["amount"]["value"] = "1.00"
        await payments.handle_notification(s, {"event": "payment.succeeded", "object": {"id": p.provider_charge_id}})
        assert p.status == "pending"
        # чужой или выдуманный id
        assert await payments.handle_notification(s, {"event": "payment.succeeded", "object": {"id": "../../x"}}) is None


async def test_evening_payment_starts_tomorrow(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    set_now(TODAY, 20)
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        await _paid(s, yk, user)
        assert enr.plan_start_date == TODAY + timedelta(days=1)


async def test_paid_before_book_then_plan_starts_by_itself(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        await _paid(s, yk, user)
        assert user.run_credits == 1
        enr = await _with_plan(s, user)
        assert enr.status == "paid" and enr.access == "credit" and user.run_credits == 0


async def test_subscription_stacks_and_covers_next_runs(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        await _paid(s, yk, user, "month")
        until = user.subscription_until
        enr = await _with_plan(s, user)
        assert enr.access == "subscription" and enr.freezes_per_week == get_settings().sub_freezes_per_week
        await _paid(s, yk, user, "year")
        assert user.subscription_until >= until + timedelta(days=364)  # год прибавился к месяцу


async def test_canceled_payment_releases_promo(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        s.add(PromoCode(code="ONE", discount_percent=50, products="run", active=True, used=0, max_uses=1))
        user = await _user(s)
        user.promo_code = "ONE"
        await s.flush()
        _url, order = await payments.start_payment(s, user, "run")
        p = await s.scalar(select(Purchase).where(Purchase.order_id == order))
        assert p.amount == round(get_settings().price_run_rub * 0.5) * 100
        assert (await s.get(PromoCode, "ONE")).used == 1  # зарезервирован под заказ
        other = await _user(s, 701)
        other.promo_code = "ONE"
        prices = await billing.prices_for(s, other)
        assert prices["run"].promo is None  # лимит занят — скидки нет
        yk.payments[p.provider_charge_id]["status"] = "canceled"
        await payments.handle_notification(s, {"event": "payment.canceled", "object": {"id": p.provider_charge_id}})
        assert p.status == "canceled"
        assert (await s.get(PromoCode, "ONE")).used == 0  # снова свободен


async def test_payments_disabled_and_email_for_receipt(db, monkeypatch):
    FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        monkeypatch.setenv("FISCAL_RECEIPTS", "true")
        get_settings.cache_clear()
        assert payments.needs_email(user)
        with pytest.raises(payments.PaymentError, match="e-mail"):
            await payments.start_payment(s, user, "run")
        with pytest.raises(payments.PaymentError, match="e-mail"):
            await payments.start_payment(s, user, "run", "not-an-email")
        url, order = await payments.start_payment(s, user, "run", "reader@example.com")
        assert user.email == "reader@example.com" and not payments.needs_email(user)
        monkeypatch.setenv("YOOKASSA_SHOP_ID", "")
        get_settings.cache_clear()
        with pytest.raises(payments.PaymentError):
            await payments.start_payment(s, user, "run")


async def test_promo_limits_and_free_promo(db):
    async with session_scope() as s:
        s.add_all([
            PromoCode(code="FULL", discount_percent=10, products="run", active=True, used=5, max_uses=5),
            PromoCode(code="OFF", discount_percent=10, products="run", active=False, used=0),
            PromoCode(code="OLD", discount_percent=10, products="run", active=True, used=0,
                      expires_at=clock.now() - timedelta(days=1)),
            PromoCode(code="FRIEND", discount_percent=100, products="run", active=True, used=0),
        ])
        await s.flush()
        for code in ("FULL", "OFF", "OLD", "NOPE", ""):
            assert await billing.valid_promo(s, code) is None
        user = await _user(s)
        enr = await _with_plan(s, user)
        assert await billing.apply_free_promo(s, user) is None  # без промокода бесплатно нельзя
        user.promo_code = "friend"
        p = await billing.apply_free_promo(s, user)
        assert p is not None and p.provider == "promo" and p.amount == 0 and enr.status == "paid"


async def test_verified_refund_closes_access(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        p = await _paid(s, yk, user)
        # неподтверждённое уведомление о возврате не действует
        await payments.handle_notification(s, {"event": "refund.succeeded", "object": {"id": "r-1", "payment_id": p.provider_charge_id}})
        assert p.status == "paid" and enr.status == "paid"
        yk.refunds["0000aaaa-r1"] = {"id": "0000aaaa-r1", "status": "succeeded", "payment_id": p.provider_charge_id}
        await payments.handle_notification(s, {"event": "refund.succeeded", "object": {"id": "0000aaaa-r1"}})
        assert p.status == "refunded" and enr.status == "refunded"


async def test_refund_of_last_subscription_keeps_earlier_period(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        await _paid(s, yk, user, "year")
        p2 = await _paid(s, yk, user, "month")
        await billing.mark_refunded(s, p2.id)
        assert billing.subscription_active(user)
        assert user.subscription_until > clock.now() + timedelta(days=360)


async def test_sales_summary(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        a = await _user(s, 701)
        b = await _user(s, 702)
        await _paid(s, yk, a)
        await _paid(s, yk, b, "month")
        summary = await billing.sales_summary(s)
        assert summary["count"] == 2 and summary["payers"] == 2
        assert summary["rub"] == get_settings().price_run_rub + get_settings().price_month_rub


async def test_expire_stale_orders(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        _url, order = await payments.start_payment(s, user, "run")
        _url2, order2 = await payments.start_payment(s, user, "month")
        p2 = await s.scalar(select(Purchase).where(Purchase.order_id == order2))
        yk.succeed(p2.provider_charge_id)  # оплачено, но уведомление потерялось
    set_now(TODAY + timedelta(days=3), 10)
    async with session_scope() as s:
        await payments.expire_stale_orders(s)
        p = await s.scalar(select(Purchase).where(Purchase.order_id == order))
        p2 = await s.scalar(select(Purchase).where(Purchase.order_id == order2))
        assert p.status == "canceled" and p2.status == "paid"


# --------------------------------------------------------------------------- связанные сценарии забега


async def test_credit_applied_after_sprint(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        plan = await _with_plan(s, user)
        sprint = await start_sprint(s, user)
        assert sprint is not None and (await current_enrollment(s, user.id)).id == sprint.id
        await _paid(s, yk, user)  # заплатил во время спринта — кредит ждёт
        assert user.run_credits == 1 and plan.status == "invited"
        sprint.status = "finished"
        await s.flush()
        assert await billing.activate_pending(s, user) == "credit"
        assert plan.status == "paid" and user.run_credits == 0


async def test_sprint_blocked_while_paid_run_goes(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        await _with_plan(s, user)
        await _paid(s, yk, user)
        assert await sprint_block(s, user.id) == "running"
        assert await start_sprint(s, user) is None


async def test_stale_plan_button_cannot_restart_running_plan(db, monkeypatch):
    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        enr = await _with_plan(s, user)
        await _paid(s, yk, user)
    set_now(TODAY + timedelta(days=3), 10)
    async with session_scope() as s:
        user = await _user(s)
        enr = await current_enrollment(s, user.id)
        with pytest.raises(PlanError):
            await confirm_plan(s, user, enr, 30)
        assert enr.plan_start_date == TODAY


async def test_sales_reminders_once(db, monkeypatch):
    from jobs.scheduler import _sales

    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        user.morning_time = time(8, 0)
        await _with_plan(s, user)
        sub_user = await _user(s, 701)
        sub_user.morning_time = time(8, 0)
        await _paid(s, yk, sub_user, "year")
        sub_user.subscription_until = clock.now() + timedelta(days=5)
    set_now(TODAY + timedelta(days=1), 10)
    out = Outbox()
    await _sales(out)
    kinds = [m.kind for m in out.messages]
    assert kinds.count("paywall") == 1 and kinds.count("sub") == 1
    out2 = Outbox()
    await _sales(out2)
    assert [m for m in out2.messages if m.kind in ("paywall", "sub")] == []  # каждое — один раз
    set_now(TODAY + timedelta(days=4), 10)
    out3 = Outbox()
    await _sales(out3)
    assert [m.kind for m in out3.messages if m.kind in ("paywall", "sub")] == ["sub"]  # второе — за день до конца


async def test_delete_me_keeps_payment_anonymous(db, monkeypatch):
    from db.models import Consent
    from services.consent import give_consent
    from services.users import delete_user_data

    yk = FakeYooKassa(monkeypatch)
    async with session_scope() as s:
        user = await _user(s)
        await give_consent(s, user, "bot")
        await _with_plan(s, user)
        p = await _paid(s, yk, user)
        p.email, p.source = "olya@example.com", "ad_vk"
        await delete_user_data(s, user)
    async with session_scope() as s:
        p = await s.scalar(select(Purchase))
        assert p.status == "paid" and p.user_id is None and p.email is None and p.source is None
        assert await s.scalar(select(func.count(Consent.id))) == 0
