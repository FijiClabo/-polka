"""Книжные клубы: ведущий делится планом своей книги, участники идут по нему со своими экземплярами.

Участник получает копию плана без текста, как при «читаем вместе»: те же главы в те же дни, к встрече клуба.
Участники клуба видят имена, фото и прогресс друг друга — так видно, кто идёт по плану. Пересказы и тексты
книг никому не показываются.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.models import Book, Club, Enrollment, User
from services.common import Outbox, OutMsg, log_event, random_code, today_for
from services.progress import load_view
from services.runs import OPEN_STATUSES, current_enrollment, ensure_enrollment
from services.social import nudge, public_status
from services.together import _plan_running, copy_plan, same_book, shareable_book

MAX_MEMBERS = 300


class ClubError(Exception):
    pass


@dataclass
class Member:
    user_id: int
    name: str
    photo_url: str | None
    progress: float
    today: str
    plan_day: int | None
    plan_days: int | None
    streak: int
    is_host: bool


async def club_of(session: AsyncSession, enr: Enrollment | None) -> Club | None:
    if enr is None or enr.club_id is None:
        return None
    club = await session.get(Club, enr.club_id)
    return club if club is not None and club.active else None


async def create_club(session: AsyncSession, user: User, title: str, meet_date: date | None) -> Club:
    """Клуб на текущую книгу ведущего: её план станет планом клуба."""
    enr = await current_enrollment(session, user.id)
    book = await shareable_book(session, enr)
    if book is None:
        raise ClubError("Сначала выбери книгу и срок — план клуба копируется с твоего.")
    if meet_date is not None and meet_date < today_for(user):
        raise ClubError("Дата встречи уже прошла.")
    name = (title or "").strip()[:120] or f"Читаем «{book.title}»"
    club = await session.scalar(select(Club).where(Club.host_user_id == user.id, Club.book_id == book.id, Club.active))
    if club is None:
        club = Club(code=random_code(10), title=name, host_user_id=user.id, book_id=book.id, meet_date=meet_date,
                    active=True)
        session.add(club)
        await session.flush()
        await log_event(session, "club_created", user.id, enr.run_id, club=club.id, days=enr.plan_days)
    else:
        club.title, club.meet_date = name, meet_date
    enr.club_id = club.id
    return club


async def join_club(session: AsyncSession, user: User, code: str, outbox: Outbox) -> tuple[str, Club | None]:
    """Вступить по ссылке ведущего: ok | already | host | busy | full | not_found."""
    club = await session.scalar(select(Club).where(Club.code == code))
    if club is None or not club.active:
        return "not_found", None
    if club.host_user_id == user.id:
        return "host", club
    src = await session.get(Book, club.book_id) if club.book_id else None
    if src is None:
        return "not_found", club
    enr = await current_enrollment(session, user.id)
    if enr is not None and enr.club_id == club.id and enr.status in OPEN_STATUSES:
        return "already", club
    if enr is not None and enr.status in OPEN_STATUSES and await _plan_running(session, user, enr):
        mine = await session.get(Book, enr.book_id) if enr.book_id else None
        if not same_book(mine, src):
            return "busy", club  # своя книга уже идёт — к клубу со следующей
    members = await session.scalar(select(func.count(Enrollment.id)).where(Enrollment.club_id == club.id))
    if (members or 0) >= MAX_MEMBERS:
        return "full", club
    enr = await ensure_enrollment(session, user)
    mine = await session.get(Book, enr.book_id) if enr.book_id else None
    if not (same_book(mine, src) and enr.plan_confirmed_at):
        await copy_plan(session, user, enr, src)
    enr.club_id = club.id
    if club.promo_code and not user.promo_code:
        from services.billing import valid_promo

        promo = await valid_promo(session, club.promo_code)
        if promo is not None:
            user.promo_code = promo.code
    if not user.source:
        user.source = f"club:{club.code}"
    host = await session.get(User, club.host_user_id) if club.host_user_id else None
    if host is not None and not host.bot_blocked:
        outbox.add(OutMsg(host.tg_id, texts.club_member_joined(user.display_name, club.title), kind="club",
                          buttons=[[{"text": "Клуб", "webapp": "club"}]]))
    await log_event(session, "club_joined", user.id, enr.run_id, club=club.id)
    return "ok", club


async def members(session: AsyncSession, club: Club) -> list[Member]:
    rows = (await session.execute(
        select(Enrollment, User).join(User, User.id == Enrollment.user_id)
        .where(Enrollment.club_id == club.id, Enrollment.status.in_(OPEN_STATUSES + ("finished",)))
        .order_by(Enrollment.id)
    )).all()
    out: list[Member] = []
    seen: set[int] = set()
    for enr, u in rows:
        if u.id in seen:
            continue
        seen.add(u.id)
        st = await public_status(session, u)
        view = await load_view(session, u, enr)  # прогресс — именно по книге клуба
        out.append(Member(u.id, u.display_name, u.photo_url, view.progress, st.today, view.plan_day, enr.plan_days,
                          enr.streak, u.id == club.host_user_id))
    out.sort(key=lambda m: (-m.progress, m.name))
    return out


async def nudge_club(session: AsyncSession, host: User, club: Club, outbox: Outbox) -> int:
    """Ведущий напоминает всем, кто сегодня ещё не сдал. Не чаще раза в день каждому."""
    if club.host_user_id != host.id:
        raise ClubError("Напоминать всем может только ведущий клуба.")
    sent = 0
    for m in await members(session, club):
        if m.is_host or m.today not in ("reading", "burned"):
            continue
        target = await session.get(User, m.user_id)
        if target is not None and await nudge(session, host, target, outbox, kind="club") == "ok":
            sent += 1
    await log_event(session, "club_nudge", host.id, club=club.id, sent=sent)
    return sent


async def leave_club(session: AsyncSession, user: User) -> bool:
    """Выйти из клуба: книга и план остаются, просто без общего списка. Ведущий закрывает клуб."""
    enr = await current_enrollment(session, user.id)
    club = await club_of(session, enr)
    if club is None:
        return False
    if club.host_user_id == user.id:
        club.active = False
        await log_event(session, "club_closed", user.id, club=club.id)
    enr.club_id = None
    await log_event(session, "club_left", user.id, club=club.id)
    return True
