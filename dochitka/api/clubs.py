"""Книжные клубы и запросы в друзья для мини-приложения."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from api.auth import current_user, get_session
from api.routes import _flush, need_consent, user_brief
from db.models import Book, Club, User
from services.clubs import ClubError, club_of, create_club, leave_club, members, nudge_club
from services.common import Outbox
from services.runs import current_enrollment
from services.social import are_friends, request_friend
from services.together import shareable_book

router = APIRouter(prefix="/api")


def club_link(club: Club) -> str:
    from bot.ui import deep_link

    return deep_link(f"c_{club.code}")


def _meet(d: date | None) -> str | None:
    return f"{d.day} {texts.MONTHS[d.month - 1]}" if d else None


async def club_json(s: AsyncSession, club: Club, viewer: User) -> dict:
    from bot.ui import share_link

    book = await s.get(Book, club.book_id) if club.book_id else None
    host = await s.get(User, club.host_user_id) if club.host_user_id else None
    rows = await members(s, club)
    link = club_link(club)
    days = None
    if host is not None:
        h_enr = await current_enrollment(s, host.id)
        days = h_enr.plan_days if h_enr and h_enr.club_id == club.id else None
    days = days or max((m.plan_days or 0 for m in rows), default=0)
    invite = texts.club_invite_text(host.display_name if host else "Ведущий", club.title, book.title if book else "",
                                    days, _meet(club.meet_date))
    out = []
    for m in rows:
        u = await s.get(User, m.user_id)
        out.append({
            **user_brief(u), "progress": round(m.progress * 100), "today": m.today, "plan_day": m.plan_day,
            "plan_days": m.plan_days, "streak": m.streak, "is_host": m.is_host, "is_me": m.user_id == viewer.id,
            "is_friend": m.user_id != viewer.id and await are_friends(s, viewer.id, m.user_id),
        })
    return {
        "id": club.id, "title": club.title, "book_title": book.title if book else None,
        "book_author": book.author if book else None, "spine_color": book.spine_color if book else None,
        "meet_date": club.meet_date.isoformat() if club.meet_date else None, "plan_days": days,
        "is_host": club.host_user_id == viewer.id, "host_name": host.display_name if host else None,
        "link": link, "invite_text": invite, "share_url": share_link(link, invite), "members": out,
        "done_today": sum(1 for m in rows if m.today == "done"), "count": len(rows),
    }


@router.get("/club")
async def get_club(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    enr = await current_enrollment(s, user.id)
    club = await club_of(s, enr)
    if club is not None:
        return {"club": await club_json(s, club, user), "can_create": False}
    book = await shareable_book(s, enr)
    return {"club": None, "can_create": book is not None, "book_title": book.title if book else None}


class ClubBody(BaseModel):
    title: str = Field(default="", max_length=120)
    meet_date: date | None = None


@router.post("/club")
async def post_club(body: ClubBody, user: User = Depends(need_consent), s: AsyncSession = Depends(get_session)):
    from services.common import OutMsg

    existed = await club_of(s, await current_enrollment(s, user.id))
    try:
        club = await create_club(s, user, body.title, body.meet_date)
    except ClubError as err:
        raise HTTPException(409, str(err)) from err
    await s.flush()
    data = await club_json(s, club, user)
    out = Outbox()
    if existed is None:
        # ссылку удобнее переслать из чата: сообщение приходит в бота
        out.add(OutMsg(user.tg_id, texts.club_created(club.title, data["link"]), kind="club",
                       buttons=[[{"text": "Отправить в чат клуба", "url": data["share_url"]}],
                                [{"text": "Клуб", "webapp": "club"}]]))
    await s.commit()
    await _flush(out)
    return {"club": data}


@router.post("/club/nudge")
async def post_club_nudge(user: User = Depends(need_consent), s: AsyncSession = Depends(get_session)):
    club = await club_of(s, await current_enrollment(s, user.id))
    if club is None:
        raise HTTPException(404, "Ты не в клубе")
    out = Outbox()
    try:
        sent = await nudge_club(s, user, club, out)
    except ClubError as err:
        raise HTTPException(403, str(err)) from err
    await s.commit()
    await _flush(out)
    return {"sent": sent}


@router.post("/club/leave")
async def post_club_leave(user: User = Depends(current_user), s: AsyncSession = Depends(get_session)):
    if not await leave_club(s, user):
        raise HTTPException(404, "Ты не в клубе")
    return {"ok": True}


class FriendRequestBody(BaseModel):
    user_id: int


@router.post("/friends/request")
async def post_friend_request(body: FriendRequestBody, user: User = Depends(need_consent),
                              s: AsyncSession = Depends(get_session)):
    """Позвать в друзья участника своего клуба или напарника — с подтверждением у второй стороны."""
    target = await s.get(User, body.user_id)
    if target is None:
        raise HTTPException(404, "Человек не найден")
    out = Outbox()
    res = await request_friend(s, user, target, out)
    if res == "not_allowed":
        raise HTTPException(403, "Позвать в друзья можно участника своего клуба или напарника. Остальным — личную ссылку.")
    await s.commit()
    await _flush(out)
    return {"result": res}
