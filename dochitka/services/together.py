"""«Читаем вместе»: друг идёт по той же книге и тому же плану, но читает свой экземпляр.

Друг получает копию плана — дни, названия глав, проценты книги, первые и последние слова отрезков, — но не текст:
текст видит только тот, кто загрузил файл (п. 9.2 оферты). Для проверки пересказов друга ИИ берёт краткие
содержания отрезков исходной книги (не текст) и, как у любой книги без текста, не отказывает, а уточняет.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from books.plan import anchors_of
from db.models import Book, Enrollment, Segment, User
from services.books import attach_book, has_progress
from services.common import log_event, now, random_code, today_for
from services.runs import OPEN_STATUSES, can_start_plan, current_enrollment, ensure_enrollment, plan_start_for
from services.social import get_pair_for


class TogetherError(Exception):
    pass


@dataclass
class TogetherOffer:
    partner_name: str
    title: str
    author: str
    plan_days: int
    book_id: int  # книга друга, с которой копируется план


async def shareable_book(session: AsyncSession, enr: Enrollment | None) -> Book | None:
    """Книга, план которой можно разделить: выбрана, разобрана, срок подтверждён, отрезки построены."""
    if enr is None or enr.status not in OPEN_STATUSES or not enr.book_id or not enr.plan_confirmed_at:
        return None
    book = await session.get(Book, enr.book_id)
    if book is None or book.parse_status != "ok":
        return None
    n = await session.scalar(select(func.count(Segment.id)).where(Segment.book_id == book.id))
    return book if n else None


async def _plan_running(session: AsyncSession, user: User, enr: Enrollment) -> bool:
    """Свой план уже идёт — книгу посреди пути не подменяем (то же правило, что и при смене срока)."""
    if enr.status not in OPEN_STATUSES or enr.plan_start_date is None:
        return False
    return today_for(user) > enr.plan_start_date or await has_progress(session, enr)


def same_book(mine: Book | None, theirs: Book | None) -> bool:
    if mine is None or theirs is None:
        return False
    if mine.id == theirs.id or mine.shared_from_id == theirs.id or theirs.shared_from_id == mine.id:
        return True
    if mine.shared_from_id and mine.shared_from_id == theirs.shared_from_id:
        return True
    return bool(mine.title_norm) and mine.title_norm == theirs.title_norm


async def offer_for(session: AsyncSession, user: User) -> TogetherOffer | None:
    """Предложить книгу напарника, если он позвал «читать вместе», а свой план ещё не начат."""
    enr = await current_enrollment(session, user.id)
    pair, partner, p_enr = await get_pair_for(session, enr)
    if pair is None or not pair.together or partner is None:
        return None
    src = await shareable_book(session, p_enr)
    if src is None:
        return None
    mine = await session.get(Book, enr.book_id) if enr and enr.book_id else None
    if same_book(mine, src) or await _plan_running(session, user, enr):
        return None
    return TogetherOffer(partner.display_name, src.title, src.author, p_enr.plan_days or 0, src.id)


async def join_same_book(session: AsyncSession, user: User) -> tuple[Book, Enrollment, TogetherOffer]:
    """Взять план книги напарника: копия отрезков без текста, тот же срок, старт — как обычно после доступа."""
    offer = await offer_for(session, user)
    if offer is None:
        raise TogetherError("Сейчас взять книгу напарника нельзя: у тебя уже идёт свой план или напарник ещё не выбрал срок.")
    enr = await ensure_enrollment(session, user)
    src = await session.get(Book, offer.book_id)
    book = await copy_plan(session, user, enr, src)
    await log_event(session, "together_joined", user.id, enr.run_id, source_book=src.id, days=enr.plan_days)
    return book, enr, offer


async def copy_plan(session: AsyncSession, user: User, enr: Enrollment, src: Book) -> Book:
    """Копия плана книги без текста: дни, главы, доли книги, метки начала и конца. Срок подтверждается сразу."""
    src_segs = list(await session.scalars(select(Segment).where(Segment.book_id == src.id).order_by(Segment.day_number)))
    root_id = src.shared_from_id if (src.source == "shared" and src.shared_from_id) else src.id  # краткие содержания — у оригинала
    book = Book(
        owner_user_id=user.id, source="shared", title=src.title, author=src.author, title_norm=src.title_norm,
        author_norm=src.author_norm, total_pages=src.total_pages, total_words=src.total_words,
        chapters_count=src.chapters_count, spine_color=src.spine_color, parse_status="ok", shared_from_id=root_id,
    )
    session.add(book)
    await session.flush()
    await attach_book(session, enr, book)
    for seg in src_segs:
        anchors = anchors_of(seg.text, seg.start_words, seg.end_words)
        session.add(Segment(
            book_id=book.id, day_number=seg.day_number,
            # у бумажной книги друга названия отрезков — страницы его издания: другому изданию они не подходят
            title=f"Отрезок {seg.day_number}" if src.source == "paper" else seg.title,
            page_from=seg.page_from, page_to=seg.page_to, pos_from=seg.pos_from, pos_to=seg.pos_to,
            word_count=seg.word_count, text=None,
            start_words=anchors[0][:200] if anchors else None, end_words=anchors[1][:200] if anchors else None,
        ))
    enr.plan_days = len(src_segs)
    enr.plan_confirmed_at = now()
    enr.plan_start_date = plan_start_for(enr.run, user) if can_start_plan(enr) else None
    enr.last_closed_day = None
    if enr.status == "invited":
        from services.billing import try_activate

        await try_activate(session, user, enr)  # есть абонемент или оплаченный забег — стартуем сразу
    if enr.pair_code is None:
        enr.pair_code = random_code(10)
    await session.flush()
    return book
