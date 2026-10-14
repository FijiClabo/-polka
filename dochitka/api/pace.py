"""Свой темп чтения: экран «Темп», «прочитано не всё», «перескажу ещё один», пауза."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import current_user, get_session
from api.routes import need_consent, seg_brief
from db.models import Book, User
from services.pace import PaceError, ask_more, pace_info, partial_options, pause, replan, resume, split_today
from services.progress import load_view
from services.runs import current_enrollment

router = APIRouter(prefix="/api")


async def _enr(s: AsyncSession, user: User):
    enr = await current_enrollment(s, user.id)
    if enr is None or not enr.plan_days:
        raise HTTPException(404, "Сначала нужна книга с планом")
    return enr


async def _pace_json(s: AsyncSession, user: User) -> dict:
    enr = await _enr(s, user)
    try:
        info = await pace_info(s, user, enr)
    except PaceError as e:
        raise HTTPException(409, str(e)) from e
    v = await load_view(s, user, enr)
    seg = v.segment if v.state == "to_read" and not v.extra else None
    paper = v.book is not None and v.book.source == "paper"
    info["today"] = {
        "segment": seg_brief(seg, v.book),
        "partial": partial_options(seg, paper) if seg else [],
        "has_next": bool(seg and seg.day_number < (enr.plan_days or 0)),
    }
    return info


@router.get("/pace")
async def get_pace(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    return await _pace_json(s, user)


class PaceBody(BaseModel):
    minutes: int | None = Field(default=None, ge=5, le=180)
    finish: date | None = None
    by_chapter: bool = False


@router.post("/pace")
async def post_pace(body: PaceBody, user: User = Depends(need_consent), s: AsyncSession = Depends(get_session)):
    enr = await _enr(s, user)
    try:
        await replan(s, user, enr, minutes=body.minutes, finish=body.finish, by_chapter=body.by_chapter)
    except PaceError as e:
        raise HTTPException(400, str(e)) from e
    return await _pace_json(s, user)


class PauseBody(BaseModel):
    days: int = Field(ge=1, le=14)


@router.post("/pace/pause")
async def post_pause(body: PauseBody, user: User = Depends(need_consent), s: AsyncSession = Depends(get_session)):
    enr = await _enr(s, user)
    try:
        await pause(s, user, enr, body.days)
    except PaceError as e:
        raise HTTPException(400, str(e)) from e
    return await _pace_json(s, user)


@router.post("/pace/resume")
async def post_resume(user: User = Depends(need_consent), s: AsyncSession = Depends(get_session)):
    """Закончить паузу раньше."""
    enr = await _enr(s, user)
    try:
        await resume(s, user, enr)
    except PaceError as e:
        raise HTTPException(400, str(e)) from e
    return await _pace_json(s, user)


class PartialBody(BaseModel):
    quarter: int | None = Field(default=None, ge=1, le=3)
    page: int | None = Field(default=None, ge=1)
    rest: Literal["tomorrow", "day"] = "tomorrow"


@router.post("/pace/partial")
async def post_partial(body: PartialBody, user: User = Depends(need_consent), s: AsyncSession = Depends(get_session)):
    enr = await _enr(s, user)
    try:
        p = await split_today(s, user, enr, quarter=body.quarter, page=body.page, rest=body.rest)
    except PaceError as e:
        raise HTTPException(400, str(e)) from e
    book = await s.get(Book, enr.book_id)
    return {"segment": seg_brief(p.segment, book), "rest": p.rest, "plan_days": p.plan_days}


@router.post("/today/more")
async def post_more(user: User = Depends(need_consent), s: AsyncSession = Depends(get_session)):
    enr = await _enr(s, user)
    seg = await ask_more(s, user, enr)
    if seg is None:
        raise HTTPException(409, "Сегодня больше отрезков не открыть: либо всё уже сдано, либо на сегодня их и так пять")
    v = await load_view(s, user, enr)
    return {"segment": seg_brief(seg, v.book)}
