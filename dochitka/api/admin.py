"""Админ-панель мини-приложения: аналитика, промокоды и пробный доступ, выдача доступа вручную.

Доступно только тем, чей Telegram ID указан в ADMIN_TG_IDS: проверяется на сервере в каждом запросе.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import current_user, get_session
from db.models import PromoCode, User
from services import analytics, billing
from services.admin import find_user
from services.common import Outbox, log_event
from settings import get_settings

router = APIRouter(prefix="/api/admin")


async def admin_user(user: User = Depends(current_user)) -> User:
    if user.tg_id not in get_settings().admin_ids:
        raise HTTPException(403, "Только для администратора")
    return user


@router.get("/overview")
async def get_overview(_: User = Depends(admin_user), s: AsyncSession = Depends(get_session)):
    s_ = get_settings()
    data = await analytics.overview(s)
    data["prices"] = billing.list_prices()
    data["payments_enabled"] = s_.payments_enabled
    data["sprint_for_everyone"] = s_.sprint_for_everyone
    return data


class PromoBody(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    discount: int = Field(ge=1, le=100)
    products: list[str] = Field(default_factory=lambda: ["run", "month", "year"])
    max_uses: int | None = Field(default=None, ge=1, le=100000)
    trial_days: int | None = Field(default=None, ge=1, le=365)
    owner: str | None = Field(default=None, max_length=64)


@router.post("/promos")
async def post_promo(body: PromoBody, admin: User = Depends(admin_user), s: AsyncSession = Depends(get_session)):
    """Создать или обновить промокод. Скидка 100% + срок в днях — пробный доступ."""
    code = billing.normalize_code(body.code)
    if not re.fullmatch(r"[A-Z0-9_-]{2,32}", code):
        raise HTTPException(400, "Код — латиница, цифры, _ и -: иначе ссылка в Telegram не сработает")
    products = [x for x in billing.PRODUCTS if x in body.products]
    if not products:
        raise HTTPException(400, "Выбери хотя бы один тариф")
    trial = body.trial_days if body.discount == 100 else None
    if trial:
        products = [x for x in products if x != "run"] or ["month"]  # пробный срок — это абонемент
    p = await s.get(PromoCode, code)
    if p is None:
        p = PromoCode(code=code, used=0)
        s.add(p)
    p.discount_percent, p.products, p.max_uses = body.discount, ",".join(products), body.max_uses
    p.trial_days, p.owner, p.active = trial, (body.owner or None), True
    await s.flush()
    await log_event(s, "promo_created", admin.id, code=code, discount=body.discount, trial_days=trial, via="webapp")
    return await analytics.promo_row(s, p)


@router.post("/promos/{code}/toggle")
async def toggle_promo(code: str, _: User = Depends(admin_user), s: AsyncSession = Depends(get_session)):
    p = await s.get(PromoCode, billing.normalize_code(code))
    if p is None:
        raise HTTPException(404, "Промокод не найден")
    p.active = not p.active
    return await analytics.promo_row(s, p)


class GrantBody(BaseModel):
    user: str = Field(min_length=1, max_length=64, description="@username или Telegram ID")
    product: str = Field(pattern=r"^(run|month|year|days)$")
    days: int | None = Field(default=None, ge=1, le=365)


@router.post("/grant")
async def post_grant(body: GrantBody, admin: User = Depends(admin_user), s: AsyncSession = Depends(get_session)):
    """Выдать доступ вручную: одна книга, месяц, год или абонемент на N дней."""
    target = await find_user(s, body.user)
    if target is None:
        raise HTTPException(404, "Человек не найден. Нужен хотя бы один /start в боте с его аккаунта.")
    if body.product == "days" and not body.days:
        raise HTTPException(400, "Укажи, на сколько дней")
    product = "month" if body.product == "days" else body.product
    out = Outbox()
    p = await billing.grant_manual(s, target, product, out, days=body.days if body.product == "days" else None)
    await log_event(s, "admin_grant", admin.id, to=target.id, product=body.product, days=body.days)
    from api.routes import _after_paid_message

    await s.commit()
    await _after_paid_message(p)
    until = target.subscription_until.date().isoformat() if target.subscription_until else None
    return {"ok": True, "name": target.display_name, "product": body.product, "until": until,
            "credits": target.run_credits}
