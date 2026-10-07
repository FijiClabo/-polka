"""API мини-приложения (раздел 8 ТЗ + пересказ из приложения, загрузка файла, финиш).

Каждый запрос проходит проверку initData. Текст книги отдаётся только владельцу,
пересказы — только себе и напарнику по правилам пары, друзьям — никогда.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time as _time
from datetime import date, time, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from ai.llm import get_llm
from ai.stt import STTError, build_stt, transcribe_audio
from api.auth import current_user, get_session
from books.parse import detect_format, is_other_book_format
from books.plan import PlanError, segment_minutes
from books.types import PARSE_ERRORS, SUBHEADING_MARK
from core.days import local_now, plan_day_number
from db.models import Book, DayResult, Enrollment, Purchase, Retelling, Segment, User
from services import billing
from services.books import (
    add_paper_book,
    confirm_plan,
    create_pending_book,
    delete_book,
    has_progress,
    options_for,
    store_upload,
    update_book_meta,
)
from services.common import Outbox, log_event, today_for
from services.flow import submit_retelling
from services.progress import DayView, accepted_by_day, effective_freezes, load_view
from services.retell import RetellOutcome
from services.runs import current_enrollment, ensure_enrollment, sprint_used, start_sprint
from services.shelf import achievements_of, conspect, shelf
from services.social import (
    are_friends,
    ensure_pair_code,
    friend_ids,
    get_pair_for,
    nudge,
    nudged_today,
    partner_feed,
    public_status,
)
from services.users import valid_timezone
from settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


async def _flush(outbox: Outbox) -> None:
    if _bot is not None and outbox.messages:
        from bot.ui import flush

        try:
            await flush(_bot, outbox)
        except Exception:
            log.exception("flush failed")


# --------------------------------------------------------------------------- сериализация


def user_brief(u: User) -> dict:
    return {"id": u.id, "name": u.display_name, "photo_url": u.photo_url}


def book_brief(b: Book | None) -> dict | None:
    if b is None:
        return None
    return {
        "id": b.id, "title": b.title, "author": b.author, "spine_color": b.spine_color, "source": b.source,
        "pages": b.total_pages, "has_text": b.has_text, "parse_status": b.parse_status,
        "chapters": b.chapters_count, "words": b.total_words,
        "reading_minutes": round((b.total_words or 0) / 200) if b.total_words else round(b.total_pages * 1.3),
    }


def seg_brief(seg: Segment | None, book: Book | None, day: int | None = None) -> dict | None:
    if seg is None:
        return None
    paper = book is not None and book.source == "paper"
    pages = seg.page_to - seg.page_from + 1
    return {
        "day_number": seg.day_number, "title": seg.title, "page_from": seg.page_from, "page_to": seg.page_to,
        "pages": pages, "minutes": segment_minutes(seg.word_count, pages, paper),
        "retell_prompt": seg.retell_prompt, "can_read": bool(book and book.has_text and seg.text),
        "pos_to": seg.pos_to,
    }


def run_brief(enr: Enrollment | None) -> dict | None:
    if enr is None:
        return None
    r = enr.run
    return {"id": r.id, "title": r.title, "kind": r.kind, "start_date": r.start_date.isoformat() if r.start_date else None,
            "price_rub": r.price_rub, "grace_days": r.grace_days}


def outcome_json(o: RetellOutcome) -> dict:
    return {
        "status": o.status, "reply": o.reply, "question": o.question, "retelling_id": o.retelling_id,
        "segment_title": o.segment_title, "day_number": o.day_number, "streak": o.streak,
        "streak_grew": o.streak_grew, "pair_streak": o.pair_streak, "partner_name": o.partner_name,
        "partner_done": o.partner_done, "achievements": o.achievements, "finished": o.finished,
        "verified": o.verified, "can_submit_more": o.can_submit_more, "next_title": o.next_title,
        "message": _status_message(o),
    }


def _status_message(o: RetellOutcome) -> str | None:
    if o.status in ("accepted", "clarify", "rejected"):
        return None
    if o.status == "queued":
        return texts.QUEUED
    if o.status == "too_short":
        return "Расскажи чуть подробнее — хотя бы пару предложений (от 50 знаков)."
    if o.status == "rate_limited":
        return texts.RATE_LIMITED
    if o.status == "checking":
        return texts.CHECKING
    import re

    return re.sub(r"<[^>]+>", "", texts.state_message(o.status, next_title=o.next_title))


# --------------------------------------------------------------------------- профиль


class MePatch(BaseModel):
    timezone: str | None = None
    morning_time: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    evening_time: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    retell_format: str | None = Field(default=None, pattern=r"^(voice|text)$")
    nudges_enabled: bool | None = None
    webapp_onboarded: bool | None = None


def _me(u: User, enr: Enrollment | None, view: DayView | None) -> dict:
    from bot.ui import bot_username

    s = get_settings()
    return {
        "user": {
            **user_brief(u), "first_name": u.first_name, "timezone": u.timezone,
            "morning_time": u.morning_time.strftime("%H:%M"), "evening_time": u.evening_time.strftime("%H:%M"),
            "retell_format": u.retell_format, "nudges_enabled": u.nudges_enabled,
            "webapp_onboarded": u.webapp_onboarded, "is_admin": u.tg_id in s.admin_ids,
        },
        "access": {"state": view.state if view else "no_run", "enrollment_status": enr.status if enr else None,
                   "run": run_brief(enr)},
        "project_name": s.project_name,
        "bot_username": bot_username(),
        "features": {"ai": get_llm().available, "voice": bool(build_stt())},
    }


@router.get("/me")
async def get_me(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    view = await load_view(s, user, enr)
    return _me(user, enr, view)


@router.patch("/me")
async def patch_me(body: MePatch, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    if body.timezone is not None:
        if not valid_timezone(body.timezone):
            raise HTTPException(400, "Неизвестный часовой пояс")
        user.timezone = body.timezone
    for field, attr in (("morning_time", "morning_time"), ("evening_time", "evening_time")):
        v = getattr(body, field)
        if v:
            h, m = map(int, v.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise HTTPException(400, "Неверное время")
            setattr(user, attr, time(h, m))
    if body.retell_format:
        user.retell_format = body.retell_format
    if body.nudges_enabled is not None:
        user.nudges_enabled = body.nudges_enabled
    if body.webapp_onboarded is not None:
        user.webapp_onboarded = body.webapp_onboarded
    enr = await current_enrollment(s, user.id)
    return _me(user, enr, await load_view(s, user, enr))


# --------------------------------------------------------------------------- сегодня


async def _week(s: AsyncSession, user: User, enr: Enrollment | None, today: date) -> list[dict]:
    monday = today - timedelta(days=today.weekday())
    results: dict[date, str] = {}
    if enr is not None:
        rows = await s.execute(
            select(DayResult.user_day, DayResult.result).where(
                DayResult.enrollment_id == enr.id, DayResult.user_day >= monday, DayResult.user_day < monday + timedelta(days=7)
            )
        )
        results = dict(rows.all())
    out = []
    for i in range(7):
        d = monday + timedelta(days=i)
        st = results.get(d)
        if st is None:
            in_plan = enr is not None and enr.plan_start_date is not None and d >= enr.plan_start_date
            st = "today" if d == today else ("future" if d > today else ("none" if not in_plan else "missed"))
        n = plan_day_number(enr.plan_start_date, d) if enr and enr.plan_start_date else None
        out.append({"date": d.isoformat(), "weekday": WEEKDAYS_SHORT[i], "day": d.day, "state": st,
                    "is_today": d == today, "plan_day": n if n and n >= 1 else None})
    return out


async def _partner_block(s: AsyncSession, enr: Enrollment | None) -> dict | None:
    pair, partner, p_enr = await get_pair_for(s, enr)
    if not pair or not partner:
        return None
    st = await public_status(s, partner)
    return {**user_brief(partner), "today": st.today, "done_at": st.done_at, "pair_streak": pair.streak,
            "best_pair_streak": pair.best_streak, "book_title": st.book_title, "plan_day": st.plan_day,
            "plan_days": st.plan_days}


@router.get("/today")
async def get_today(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id, lock=True)
    if enr is not None:
        from services.progress import close_pending_days

        out = Outbox()
        await close_pending_days(s, enr, user, out)
    v = await load_view(s, user, enr)
    now_local = local_now(_now(), user.timezone)
    seg = v.segment if v.state in ("to_read", "clarify", "checking", "not_started") else None
    data = {
        "state": v.state,
        "date": (v.today or today_for(user)).isoformat(),
        "hour": now_local.hour,
        "book": book_brief(v.book),
        "plan_day": v.plan_day, "plan_days": v.plan_days,
        "segment": seg_brief(seg, v.book),
        "next_segment": seg_brief(v.next_segment, v.book),
        "streak": enr.streak if enr else 0, "best_streak": enr.best_streak if enr else 0,
        "freezes_left": v.freezes_left if enr else 0,
        "progress": round(v.progress, 3),
        "done_today": v.done_today, "accepted_today": v.accepted_today, "limit": v.limit,
        "catching_up": v.catching_up,
        "open_retelling": {
            "id": v.open_retelling.id, "verdict": v.open_retelling.verdict, "reply": v.open_retelling.ai_reply,
            "question": v.open_retelling.ai_question,
        } if v.open_retelling else None,
        "starts_on": v.starts_on.isoformat() if v.starts_on else None,
        "deadline": v.deadline.isoformat() if v.deadline else None,
        "run": run_brief(enr),
        "partner": await _partner_block(s, enr),
        "week": await _week(s, user, enr, v.today or today_for(user)),
        "payment_info": get_settings().payment_info if v.state == "awaiting_payment" else None,
        "accepted_days": sorted(v.accepted),
        "sprint_available": v.state in ("no_run", "refunded", "expired", "awaiting_payment", "no_book")
        and not await sprint_used(s, user.id),
        "has_access": billing.has_access(user),
    }
    return data


def _now():
    from core import clock

    return clock.now()


# --------------------------------------------------------------------------- забег


@router.get("/run")
async def get_run(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    v = await load_view(s, user, enr)
    if enr is None or not enr.plan_days:
        return {"state": v.state, "days": [], "book": book_brief(v.book)}
    results: dict[date, DayResult] = {}
    if enr.plan_start_date:
        rows = await s.scalars(select(DayResult).where(DayResult.enrollment_id == enr.id))
        results = {r.user_day: r for r in rows}
    today = v.today or today_for(user)
    days = []
    total_days = enr.plan_days + (enr.run.grace_days if v.state in ("expired",) or (v.plan_day or 0) > enr.plan_days else 0)
    for n in range(1, total_days + 1):
        d = enr.plan_start_date + timedelta(days=n - 1) if enr.plan_start_date else None
        r = results.get(d) if d else None
        if r is not None:
            st = r.result
        elif d is None or d > today:
            st = "future"
        elif d == today:
            st = "today"
        else:
            st = "missed"
        days.append({"n": n, "date": d.isoformat() if d else None, "state": st, "grace": n > enr.plan_days})
    done = sum(1 for x in days if x["state"] == "done")
    last_day = enr.plan_start_date + timedelta(days=enr.plan_days - 1) if enr.plan_start_date else None
    return {
        "state": v.state, "book": book_brief(v.book), "plan_days": enr.plan_days,
        "start": enr.plan_start_date.isoformat() if enr.plan_start_date else None,
        "finish": last_day.isoformat() if last_day else None,
        "deadline": v.deadline.isoformat() if v.deadline else None,
        "today_n": v.plan_day, "days": days,
        "stats": {"done": done, "segments_done": len(v.accepted), "streak": enr.streak,
                  "freezes_left": effective_freezes(enr, v.plan_day)},
    }


@router.get("/run/day/{n}")
async def get_run_day(n: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    if enr is None or not enr.plan_days or not enr.plan_start_date or enr.book_id is None:
        raise HTTPException(404, "Нет плана")
    d = enr.plan_start_date + timedelta(days=n - 1)
    book = await s.get(Book, enr.book_id)
    dr = await s.scalar(select(DayResult).where(DayResult.enrollment_id == enr.id, DayResult.user_day == d))
    rets = list(await s.scalars(
        select(Retelling).where(Retelling.enrollment_id == enr.id, Retelling.user_day == d).order_by(Retelling.id)
    ))
    accepted = [r for r in rets if r.verdict == "accepted"]
    shown = accepted[0] if accepted else (rets[-1] if rets else None)
    seg = await s.get(Segment, shown.segment_id) if shown and shown.segment_id else None
    if seg is None:
        seg = await s.scalar(select(Segment).where(Segment.book_id == book.id, Segment.day_number == min(n, enr.plan_days)))
    return {
        "n": n, "date": d.isoformat(), "state": dr.result if dr else None,
        "segment": seg_brief(seg, book),
        "retelling": {
            "text": shown.raw_text, "verdict": shown.verdict, "reply": shown.ai_reply, "question": shown.ai_question,
            "verified": shown.verified, "source": shown.source,
        } if shown else None,
    }


# --------------------------------------------------------------------------- напарник


@router.get("/pair")
async def get_pair(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    pair, partner, p_enr = await get_pair_for(s, enr)
    if not pair or not partner:
        link = None
        if enr is not None and enr.status in ("invited", "paid", "active"):
            from bot.ui import deep_link

            link = deep_link(f"p_{await ensure_pair_code(s, enr)}")
        return {"has_pair": False, "invite_link": link, "invite_text": texts.pair_invite_text(user.display_name)}
    feed = await partner_feed(s, user, enr)
    st = await public_status(s, partner)
    nudged = partner.id in await nudged_today(s, user)
    return {
        "has_pair": True, "partner": {**user_brief(partner), "today": st.today, "done_at": st.done_at,
                                      "book_title": st.book_title, "book_author": st.book_author,
                                      "plan_day": st.plan_day, "plan_days": st.plan_days, "streak": st.streak},
        "pair_streak": pair.streak, "best_pair_streak": pair.best_streak, "same_book": feed["same_book"],
        "feed": feed["items"], "can_nudge": st.today in ("reading", "burned") and not nudged, "nudged": nudged,
    }


@router.post("/pair/nudge")
async def post_pair_nudge(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    _, partner, _ = await get_pair_for(s, enr)
    if partner is None:
        raise HTTPException(404, "Нет напарника")
    out = Outbox()
    res = await nudge(s, user, partner, out, kind="partner")
    await s.commit()
    await _flush(out)
    return {"result": res}


@router.get("/pair/invite")
async def get_pair_invite(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    from bot.ui import deep_link, share_link

    enr = await current_enrollment(s, user.id)
    if enr is None:
        raise HTTPException(404, "Нет забега")
    link = deep_link(f"p_{await ensure_pair_code(s, enr)}")
    text = texts.pair_invite_text(user.display_name)
    return {"link": link, "text": text, "share_url": share_link(link, text)}


# --------------------------------------------------------------------------- книга


class BookPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    author: str | None = Field(default=None, max_length=300)
    spine_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")


class PaperBook(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    author: str = Field(default="", max_length=300)
    pages: int = Field(ge=20, le=3000)


class PlanBody(BaseModel):
    days: int


async def _book_enrollment(s: AsyncSession, user: User) -> Enrollment:
    """Текущее участие или новое: групповой забег ведущего, иначе личный забег."""
    return await ensure_enrollment(s, user)


@router.get("/book")
async def get_book(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    book = await s.get(Book, enr.book_id) if enr and enr.book_id else None
    if book is None:
        return {"book": None, "can_add": True, "max_mb": get_settings().max_book_mb}
    return {
        "book": book_brief(book),
        "parse_status": book.parse_status,
        "parse_error": PARSE_ERRORS.get(book.parse_error or "", None) if book.parse_status == "failed" else None,
        "plan_days": enr.plan_days, "plan_confirmed": bool(enr.plan_confirmed_at),
        "start": enr.plan_start_date.isoformat() if enr.plan_start_date else None,
        "has_progress": await has_progress(s, enr), "run": run_brief(enr), "max_mb": get_settings().max_book_mb,
        "awaiting_payment": enr.status == "invited" and bool(enr.plan_confirmed_at),
    }


@router.patch("/book")
async def patch_book(body: BookPatch, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    book = await s.get(Book, enr.book_id) if enr and enr.book_id else None
    if book is None or book.owner_user_id != user.id:
        raise HTTPException(404, "Нет книги")
    await update_book_meta(s, book, title=body.title, author=body.author, spine_color=body.spine_color)
    return {"book": book_brief(book)}


@router.post("/book/paper")
async def post_paper(body: PaperBook, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await _book_enrollment(s, user)
    book = await add_paper_book(s, user, enr, body.title, body.author, body.pages)
    return {"book": book_brief(book)}


@router.post("/sprint")
async def post_sprint(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await start_sprint(s, user)
    if enr is None:
        raise HTTPException(409, "Бесплатный спринт уже был — дальше забег по тарифу")
    return {"ok": True, "enrollment_id": enr.id}


@router.post("/runs/new")
async def post_new_run(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    """Следующая книга: новое участие (личный забег), если текущее закончено."""
    enr = await ensure_enrollment(s, user)
    await log_event(s, "next_run", user.id, enr.run_id)
    return {"ok": True, "enrollment_id": enr.id, "status": enr.status}


# --------------------------------------------------------------------------- оплата


def _price_json(p) -> dict:
    return {"rub": p.rub, "stars": p.stars, "list_rub": p.list_rub, "list_stars": p.list_stars, "promo": p.promo,
            "discount": p.discount, "free": p.free}


async def _billing_state(s: AsyncSession, user: User) -> dict:
    st = get_settings()
    prices = await billing.prices_for(s, user)
    chk = await billing.refund_check(s, user)
    base = st.public_url if st.public_url.startswith("http") else ""
    enr = await current_enrollment(s, user.id)
    book = await s.get(Book, enr.book_id) if enr and enr.book_id else None
    purchases = list(await s.scalars(
        select(Purchase).where(Purchase.user_id == user.id).order_by(Purchase.id.desc()).limit(10)
    ))
    return {
        "enabled": st.payments_enabled,
        "methods": {"card": st.payments_yookassa, "stars": st.payments_stars},
        "prices": {k: _price_json(v) for k, v in prices.items()},
        "promo": user.promo_code,
        "subscription": {
            "active": billing.subscription_active(user),
            "until": user.subscription_until.isoformat() if user.subscription_until else None,
            "kind": user.subscription_kind, "recurring": user.subscription_recurring,
        },
        "credits": user.run_credits,
        "refund": {"eligible": chk.eligible, "partial": chk.partial, "reason": chk.reason if not chk.eligible else "",
                   "until": chk.until.isoformat() if chk.until else None},
        "guarantee_days": st.refund_days,
        "offer_url": f"{base}/offer" if base else None,
        "privacy_url": f"{base}/privacy" if base else None,
        "manual_info": st.payment_info,
        "sprint_available": not await sprint_used(s, user.id),
        "book": {"title": book.title, "author": book.author, "spine_color": book.spine_color} if book else None,
        "plan_days": enr.plan_days if enr and enr.plan_confirmed_at else None,
        "needs_access": bool(enr and enr.status == "invited"),
        "purchases": [
            {"id": p.id, "product": p.product, "status": p.status, "date": p.created_at.date().isoformat(),
             "amount": billing.format_amount(p)}
            for p in purchases
        ],
    }


@router.get("/billing")
async def get_billing(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    await log_event(s, "paywall_shown", user.id, via="webapp")
    return await _billing_state(s, user)


class InvoiceBody(BaseModel):
    product: str = Field(pattern=r"^(run|month|year)$")
    method: str = Field(pattern=r"^(card|stars)$")


@router.post("/billing/invoice")
async def post_invoice(body: InvoiceBody, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    """Ссылка на оплату для Telegram.WebApp.openInvoice."""
    from aiogram.types import LabeledPrice

    try:
        inv = await billing.build_invoice(s, user, body.product, body.method)
    except ValueError as e:
        raise HTTPException(400, "Этот способ оплаты сейчас недоступен") from e
    if _bot is None:
        raise HTTPException(503, "Оплата временно недоступна")
    await log_event(s, "invoice_opened", user.id, product=body.product, method=body.method, via="webapp")
    link = await _bot.create_invoice_link(
        title=inv.title[:32], description=inv.description[:255], payload=inv.payload, currency=inv.currency,
        prices=[LabeledPrice(label=inv.title[:32], amount=inv.amount)], provider_token=inv.provider_token or None,
        subscription_period=inv.subscription_period, need_email=inv.need_email or None,
        send_email_to_provider=inv.send_email_to_provider or None, provider_data=inv.provider_data,
    )
    return {"url": link}


class PromoBody(BaseModel):
    code: str = Field(min_length=1, max_length=32)


@router.post("/billing/promo")
async def post_promo(body: PromoBody, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    promo = await billing.valid_promo(s, body.code)
    if promo is None:
        raise HTTPException(404, "Такого промокода нет или он закончился")
    user.promo_code = promo.code
    if not user.source:
        user.source = f"promo:{promo.code}"
    await log_event(s, "promo_applied", user.id, code=promo.code, via="webapp")
    return await _billing_state(s, user)


class FreeBody(BaseModel):
    product: str = Field(default="run", pattern=r"^(run|month|year)$")


@router.post("/billing/free")
async def post_free(body: FreeBody, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    p = await billing.apply_free_promo(s, user, body.product)
    if p is None:
        raise HTTPException(400, "Промокод не даёт бесплатный доступ")
    return await _billing_state(s, user)


class CodeBody(BaseModel):
    code: str = Field(min_length=4, max_length=40)


@router.post("/billing/redeem")
async def post_redeem(body: CodeBody, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    """Код активации из заказа на сайте."""
    out = Outbox()
    status, p = await billing.redeem_code(s, user, body.code, out)
    if status not in ("ok", "already"):
        raise HTTPException(404 if status == "not_found" else 409, texts.CODE_RESULT[status])
    await s.commit()
    await _flush(out)
    return {"result": status, "product": p.product if p else None, **(await _billing_state(s, user))}


@router.post("/billing/cancel")
async def post_cancel_sub(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    """Отключить автопродление звёздного абонемента; доступ остаётся до конца срока."""
    res = await billing.cancel_subscription(s, user, _bot)
    if res == "error":
        raise HTTPException(502, texts.sub_cancel_result(res, user.subscription_until))
    return {"result": res, "message": texts.sub_cancel_result(res, user.subscription_until)}


@router.post("/billing/refund")
async def post_refund(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    out = Outbox()
    res = await billing.request_refund(s, user, _bot, out)
    await s.commit()
    await _flush(out)
    if res not in ("ok", "requested"):
        raise HTTPException(400, res)
    return {"result": res}


@router.post("/book/upload")
async def post_upload(
    background: BackgroundTasks, file: UploadFile = File(...), user: User = Depends(current_user),
    s: AsyncSession = Depends(get_session),
):
    name = file.filename or "book"
    if detect_format(name) is None:
        msg = "Пока умею только epub и fb2." if is_other_book_format(name) else "Это не похоже на книгу epub/fb2."
        raise HTTPException(415, msg)
    max_bytes = get_settings().max_book_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, f"Файл больше {get_settings().max_book_mb} МБ")
    await _book_enrollment(s, user)
    book = await create_pending_book(s, user, name, len(data))
    book.file_path = store_upload(data, "." + name.rsplit(".", 1)[-1].lower())
    await log_event(s, "book_uploaded", user.id, format=detect_format(name), size=len(data), via="webapp")
    await s.commit()

    async def parse_later(uid: int, tg_id: int, bid: int):
        from bot.handlers_books import handle_parse

        if _bot is not None:
            await handle_parse(_bot, tg_id, uid, bid, name, data)

    background.add_task(parse_later, user.id, user.tg_id, book.id)
    return {"book_id": book.id, "status": "pending"}


@router.get("/book/plan-options")
async def get_plan_options(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    book = await s.get(Book, enr.book_id) if enr and enr.book_id else None
    if book is None or book.parse_status != "ok":
        raise HTTPException(404, "Нет книги")
    opts = options_for(book, enr)
    return {"options": [o.__dict__ for o in opts], "book": book_brief(book), "run": run_brief(enr)}


@router.post("/book/plan")
async def post_plan(body: PlanBody, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    if enr is None or enr.book_id is None:
        raise HTTPException(404, "Нет книги")
    try:
        await confirm_plan(s, user, enr, body.days)
    except PlanError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "plan_days": enr.plan_days,
            "start": enr.plan_start_date.isoformat() if enr.plan_start_date else None,
            "awaiting_payment": enr.status == "invited"}


@router.delete("/book")
async def del_book(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    book = await s.get(Book, enr.book_id) if enr and enr.book_id else None
    if book is None or book.owner_user_id != user.id:
        raise HTTPException(404, "Нет книги")
    await delete_book(s, enr, book)
    return {"ok": True}


# --------------------------------------------------------------------------- чтение


@router.get("/read/{day_number}")
async def get_read(day_number: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    """Текст отрезка: только владельцу книги и только день не позже текущего."""
    enr = await current_enrollment(s, user.id)
    if enr is None or enr.book_id is None or not enr.plan_days:
        raise HTTPException(404, "Нет плана")
    book = await s.get(Book, enr.book_id)
    if book is None or book.owner_user_id != user.id or not book.has_text:
        raise HTTPException(404, "Текст недоступен")
    accepted = await accepted_by_day(s, enr)
    current = plan_day_number(enr.plan_start_date, today_for(user)) if enr.plan_start_date else 0
    allowed = day_number <= current or day_number in accepted or enr.status == "finished"
    if not allowed or day_number < 1 or day_number > enr.plan_days:
        raise HTTPException(403, "Этот отрезок откроется в свой день")
    seg = await s.scalar(select(Segment).where(Segment.book_id == book.id, Segment.day_number == day_number))
    if seg is None or not seg.text:
        raise HTTPException(404, "Текст недоступен")
    paragraphs = []
    for line in seg.text.split("\n"):
        if line.startswith(SUBHEADING_MARK):
            paragraphs.append({"t": "h", "text": line[len(SUBHEADING_MARK):]})
        elif line.strip():
            paragraphs.append({"t": "p", "text": line})
    await log_event(s, "read_open", user.id, enr.run_id, day=day_number)
    return {
        "day_number": day_number, "title": seg.title, "book_title": book.title, "author": book.author,
        "minutes": segment_minutes(seg.word_count, seg.page_to - seg.page_from + 1, False),
        "page_from": seg.page_from, "page_to": seg.page_to, "paragraphs": paragraphs,
        "accepted": day_number in accepted, "plan_days": enr.plan_days,
    }


# --------------------------------------------------------------------------- пересказ из приложения


class RetellBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@router.post("/retell")
async def post_retell(body: RetellBody, user: User = Depends(current_user)):
    out = Outbox()
    res = await submit_retelling(user.id, body.text, source="text", via="webapp", outbox=out)
    await _flush(out)
    return outcome_json(res)


@router.post("/retell/voice")
async def post_retell_voice(
    audio: UploadFile = File(...), duration: float = Form(0), user: User = Depends(current_user),
):
    data = await audio.read(15 * 1024 * 1024)
    if not data:
        raise HTTPException(400, "Пустая запись")
    try:
        text, dur = await transcribe_audio(data)
    except STTError as e:
        log.warning("webapp STT failed: %s", e)
        raise HTTPException(503, "Не получилось разобрать запись. Попробуй ещё раз или напиши текстом.") from e
    if dur > get_settings().voice_max_sec + 5:
        raise HTTPException(400, "Запись длиннее 3 минут")
    if not text.strip():
        raise HTTPException(422, "Не расслышал. Попробуй ещё раз поближе к микрофону.")
    out = Outbox()
    res = await submit_retelling(user.id, text, source="voice", via="webapp", voice_duration=int(dur), outbox=out)
    await _flush(out)
    return {**outcome_json(res), "heard": text}


# --------------------------------------------------------------------------- друзья


@router.get("/friends")
async def get_friends(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    me = await public_status(s, user)
    nudged = await nudged_today(s, user)
    items = []
    for fid in await friend_ids(s, user.id):
        f = await s.get(User, fid)
        if not f:
            continue
        st = await public_status(s, f)
        items.append({**st.__dict__, "is_me": False, "can_nudge": st.today in ("reading", "burned") and fid not in nudged,
                      "nudged": fid in nudged})
    items.append({**me.__dict__, "is_me": True, "can_nudge": False, "nudged": False})
    order = {"done": 0, "reading": 1, "burned": 2, "waiting": 3, "finished": 4, "idle": 5}
    items.sort(key=lambda x: (order.get(x["today"], 9), -x["streak"]))
    enr = await current_enrollment(s, user.id)
    return {"items": items, "pair": await _partner_block(s, enr), "count": len(items) - 1}


@router.get("/friends/invite")
async def get_friends_invite(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    from bot.ui import deep_link, share_link

    link = deep_link(f"f_{user.friend_code}")
    text = texts.friend_invite_text(user.display_name)
    await log_event(s, "friend_invite_shared", user.id, via="webapp")
    return {"link": link, "text": text, "share_url": share_link(link, text)}


@router.get("/friends/{user_id}")
async def get_friend(user_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    """Карточка друга: книга, стрик, ачивки, полка. Пересказы и конспекты — никогда."""
    if not await are_friends(s, user.id, user_id):
        raise HTTPException(404, "Не найдено")
    f = await s.get(User, user_id)
    st = await public_status(s, f)
    enr = await current_enrollment(s, f.id)
    sh = await shelf(s, f, enr)
    for item in sh["finished"] + ([sh["current"]] if sh["current"] else []):
        item.pop("retellings", None)  # другу — только полка, ничего о пересказах
    sh.pop("retellings_count", None)
    return {"status": st.__dict__, "achievements": await achievements_of(s, f.id), "shelf": sh}


@router.post("/friends/{user_id}/nudge")
async def post_friend_nudge(user_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    target = await s.get(User, user_id)
    if target is None or not await are_friends(s, user.id, user_id):
        raise HTTPException(404, "Не найдено")
    out = Outbox()
    res = await nudge(s, user, target, out, kind="friend")
    await s.commit()
    await _flush(out)
    return {"result": res}


# --------------------------------------------------------------------------- ачивки, полка, конспект


@router.get("/achievements")
async def get_achievements(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    return {"items": await achievements_of(s, user.id)}


@router.get("/shelf")
async def get_shelf(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    return await shelf(s, user, enr)


@router.get("/shelf/{book_id}")
async def get_shelf_book(book_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    data = await conspect(s, user, book_id)
    if data is None:
        raise HTTPException(404, "Не найдено")
    return data


@router.get("/finish")
async def get_finish(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await s.scalar(
        select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.status == "finished")
        .order_by(Enrollment.finished_at.desc()).limit(1)
    )
    if enr is None:
        raise HTTPException(404, "Ещё нет дочитанных книг")
    book = await s.get(Book, enr.book_id) if enr.book_id else None
    retells = await s.scalar(select(func.count(Retelling.id)).where(Retelling.enrollment_id == enr.id, Retelling.verdict == "accepted"))
    _, partner, _ = await get_pair_for(s, enr)
    sh = await shelf(s, user, None)
    return {
        "book": book_brief(book), "plan_days": enr.plan_days, "best_streak": enr.best_streak, "retellings": retells or 0,
        "partner": user_brief(partner) if partner else None,
        "start": enr.plan_start_date.isoformat() if enr.plan_start_date else None,
        "finished_at": enr.finished_at.isoformat() if enr.finished_at else None,
        "shelf": [{"color": b["spine_color"], "pages": b["pages"]} for b in sh["finished"]],
    }


# --------------------------------------------------------------------------- карточки


def _sign(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(get_settings().bot_token.encode(), raw, hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _unsign(token: str) -> dict | None:
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except (ValueError, TypeError):
        return None
    calc = hmac.new(get_settings().bot_token.encode(), raw, hashlib.sha256).digest()[:12]
    if not hmac.compare_digest(calc, sig):
        return None
    data = json.loads(raw)
    if data.get("exp", 0) < _time.time():
        return None
    return data


async def render_for(s: AsyncSession, user: User, kind: str, size: str) -> bytes:
    from share.cards import render_card

    enr = await current_enrollment(s, user.id)
    if kind == "finish":
        enr = await s.scalar(
            select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.status == "finished")
            .order_by(Enrollment.finished_at.desc()).limit(1)
        )
        if enr is None:
            raise HTTPException(404, "Ещё нет дочитанных книг")
    if enr is None:
        raise HTTPException(404, "Нет забега")
    book = await s.get(Book, enr.book_id) if enr.book_id else None
    pair, partner, _ = await get_pair_for(s, enr)
    if kind == "pair" and not pair:
        raise HTTPException(404, "Нет напарника")
    retells = await s.scalar(select(func.count(Retelling.id)).where(Retelling.enrollment_id == enr.id, Retelling.verdict == "accepted"))
    sh = await shelf(s, user, None)
    shelf_items = [(b["spine_color"], b["pages"]) for b in sh["finished"]]
    await log_event(s, "share_generated", user.id, type=kind, size=size)
    return await asyncio.to_thread(
        render_card, kind, user=user, enr=enr, book=book, partner=partner, retells=retells or 0,
        pair_streak=pair.streak if pair else 0, shelf=shelf_items, size=size,
    )


@router.get("/share/{kind}")
async def get_share(kind: str, size: str = "story", user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    if kind not in ("streak", "finish", "pair"):
        raise HTTPException(404)
    png = await render_for(s, user, kind, size)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@router.post("/share/{kind}/send")
async def send_share(kind: str, size: str = "story", user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    """Прислать карточку в чат с ботом — оттуда её удобно переслать или сохранить."""
    if kind not in ("streak", "finish", "pair"):
        raise HTTPException(404)
    png = await render_for(s, user, kind, size)
    if _bot is None:
        raise HTTPException(503, "Бот недоступен")
    from aiogram.types import BufferedInputFile

    await _bot.send_photo(user.tg_id, BufferedInputFile(png, f"{kind}.png"),
                          caption="Готово — перешли друзьям или сохрани для сторис.")
    return {"ok": True}


@router.get("/share/{kind}/link")
async def share_link_ep(kind: str, request: Request, size: str = "story", user: User = Depends(current_user)):
    """Публичная ссылка на картинку на 1 час — нужна для «Поделиться в сторис» (shareToStory)."""
    if kind not in ("streak", "finish", "pair"):
        raise HTTPException(404)
    token = _sign({"u": user.id, "k": kind, "s": size, "exp": int(_time.time()) + 3600})
    base = get_settings().public_url or str(request.base_url).rstrip("/")
    return {"url": f"{base}/share/img/{token}.png"}


public_router = APIRouter()


@public_router.get("/share/img/{token}.png")
async def public_share_img(token: str, s: AsyncSession = Depends(get_session)):
    data = _unsign(token)
    if not data:
        raise HTTPException(404)
    user = await s.get(User, int(data["u"]))
    if user is None:
        raise HTTPException(404)
    png = await render_for(s, user, data["k"], data.get("s", "story"))
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=600"})


# --------------------------------------------------------------------------- события


class EventBody(BaseModel):
    type: str = Field(pattern=r"^(webapp_open|share_clicked|onboarding_seen)$")
    screen: str | None = Field(default=None, max_length=32)


@router.post("/events")
async def post_event(body: EventBody, user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    await log_event(s, body.type, user.id, screen=body.screen)
    return {"ok": True}


