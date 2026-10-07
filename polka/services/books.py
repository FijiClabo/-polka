"""Книги пользователя: сохранение разобранного файла, бумажная книга, план, удаление."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from books.parse import parse_book
from books.plan import PlanError, PlanOption, build_paper_plan, build_plan, plan_options
from books.types import BookParseError, ParsedBook, ParsedChapter
from core.rules import normalize_author, normalize_text
from db.models import Book, Chapter, Enrollment, Retelling, Segment, User
from services.common import log_event, now, today_for
from services.runs import can_start_plan, plan_start_for
from settings import get_settings

log = logging.getLogger(__name__)

SPINE_COLORS = ["#E2553F", "#3E8E6E", "#E98A6B", "#5B63D6", "#F2C14E", "#8C5BD6", "#2F7FB8", "#C2410C", "#4D7C0F"]


def pick_spine_color(seed: int) -> str:
    return SPINE_COLORS[seed % len(SPINE_COLORS)]


def store_upload(data: bytes, ext: str) -> str:
    """Сохранить файл вне публичных каталогов под случайным именем."""
    d = get_settings().books_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{secrets.token_hex(16)}{ext}"
    path.write_bytes(data)
    return str(path)


def remove_file(path: str | None) -> None:
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            log.warning("cannot remove %s: %s", path, e)


async def has_progress(session: AsyncSession, enr: Enrollment) -> bool:
    if enr.book_id is None:
        return False
    n = await session.scalar(
        select(func.count(Retelling.id))
        .join(Segment, Segment.id == Retelling.segment_id)
        .where(Retelling.enrollment_id == enr.id, Segment.book_id == enr.book_id, Retelling.verdict == "accepted")
    )
    return bool(n)


async def create_pending_book(session: AsyncSession, user: User, filename: str, size: int) -> Book:
    lower = filename.lower()
    source = "fb2" if ".fb2" in lower or lower.endswith(".fbz") else "epub"
    book = Book(
        owner_user_id=user.id, source=source, title=Path(filename).stem[:300], author="", parse_status="pending",
        file_size=size, spine_color=pick_spine_color(user.id + int(now().timestamp()) // 60),
    )
    session.add(book)
    await session.flush()
    return book


def parse_file(filename: str, data: bytes) -> ParsedBook:
    """Синхронный разбор (запускается в отдельном потоке)."""
    return parse_book(filename, data)


async def save_parsed(session: AsyncSession, book: Book, parsed: ParsedBook, digest: str) -> Book:
    book.title = parsed.title
    book.author = parsed.author
    book.title_norm = normalize_text(parsed.title)
    book.author_norm = normalize_author(parsed.author)
    book.total_chars = parsed.total_chars
    book.total_words = parsed.total_words
    book.total_pages = parsed.total_pages
    book.chapters_count = len(parsed.chapters) if parsed.has_chapters else 0
    book.file_hash = digest
    book.parse_status = "ok"
    book.parse_error = None
    for i, ch in enumerate(parsed.chapters):
        session.add(
            Chapter(
                book_id=book.id, order_no=i, title=ch.title, word_count=ch.word_count, char_count=ch.char_count,
                text=ch.text,
            )
        )
    remove_file(book.file_path)  # после разбора исходный файл не храним
    book.file_path = None
    await log_event(session, "book_parsed", book.owner_user_id, pages=book.total_pages, chapters=book.chapters_count)
    return book


async def mark_failed(session: AsyncSession, book: Book, err: BookParseError) -> None:
    book.parse_status = "failed"
    book.parse_error = err.code
    remove_file(book.file_path)
    book.file_path = None
    await log_event(session, "book_parse_failed", book.owner_user_id, reason=err.code)


async def attach_book(session: AsyncSession, enr: Enrollment, book: Book) -> None:
    """Сделать книгу текущей в участии. Старый план сбрасывается, стрик сохраняется."""
    old_id = enr.book_id
    enr.book_id = book.id
    enr.plan_days = None
    enr.plan_confirmed_at = None
    enr.plan_start_date = None
    enr.last_closed_day = None
    if old_id and old_id != book.id:
        old = await session.get(Book, old_id)
        if old is not None:
            if await _book_has_retellings(session, old.id):
                await delete_book_text(session, old)
            else:
                await session.delete(old)


async def _book_has_retellings(session: AsyncSession, book_id: int) -> bool:
    return bool(
        await session.scalar(
            select(func.count(Retelling.id)).join(Segment, Segment.id == Retelling.segment_id).where(Segment.book_id == book_id)
        )
    )


async def add_paper_book(session: AsyncSession, user: User, enr: Enrollment, title: str, author: str, pages: int) -> Book:
    book = Book(
        owner_user_id=user.id, source="paper", title=title.strip()[:300], author=author.strip()[:300],
        title_norm=normalize_text(title), author_norm=normalize_author(author), total_pages=int(pages),
        parse_status="ok", spine_color=pick_spine_color(user.id + len(title)),
    )
    session.add(book)
    await session.flush()
    await attach_book(session, enr, book)
    await log_event(session, "book_paper_added", user.id, enr.run_id, pages=pages)
    return book


def options_for(book: Book, enr: Enrollment) -> list[PlanOption]:
    return plan_options(
        book.total_pages, book.total_words, paper=book.source == "paper", sprint=enr.run.kind == "sprint"
    )


async def confirm_plan(session: AsyncSession, user: User, enr: Enrollment, plan_days: int) -> list[Segment]:
    """Подтвердить срок и построить отрезки. До подтверждения хранятся только главы."""
    book = await session.get(Book, enr.book_id) if enr.book_id else None
    if book is None or book.parse_status != "ok":
        raise PlanError("Нет книги")
    if enr.status not in ("invited", "paid", "active"):
        raise PlanError("Этот забег уже закончен — начни новый с этой или другой книгой")
    if enr.plan_start_date is not None and (today_for(user) > enr.plan_start_date or await has_progress(session, enr)):
        # старая кнопка в чате не должна пересобрать идущий план и обнулить прогресс
        raise PlanError("План уже идёт — срок поменять нельзя. Чтобы начать заново, замени книгу")
    allowed = {o.days for o in options_for(book, enr)}
    if plan_days not in allowed:
        raise PlanError("Такой срок для этой книги недоступен")
    await session.execute(delete(Segment).where(Segment.book_id == book.id))
    if book.source == "paper":
        planned = build_paper_plan(book.total_pages, plan_days)
    else:
        chapters = list(await session.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.order_no)))
        parsed = [ParsedChapter(c.title, c.text.split("\n") if c.text else []) for c in chapters]
        planned = build_plan(parsed, plan_days, has_chapters=book.chapters_count > 0)
    segs = []
    for p in planned:
        s = Segment(
            book_id=book.id, day_number=p.day_number, title=p.title, page_from=p.page_from, page_to=p.page_to,
            pos_from=p.pos_from, pos_to=p.pos_to, word_count=p.word_count, text=p.text,
        )
        session.add(s)
        segs.append(s)
    enr.plan_days = plan_days
    enr.plan_confirmed_at = now()
    enr.plan_start_date = plan_start_for(enr.run, user) if can_start_plan(enr) else None
    enr.last_closed_day = None
    if enr.status == "invited":
        from services.billing import try_activate

        await try_activate(session, user, enr)  # есть абонемент или оплаченный забег — стартуем сразу
    if enr.pair_code is None:
        from services.common import random_code

        enr.pair_code = random_code(10)
    await session.flush()
    ppd = round(book.total_pages / plan_days, 1)
    await log_event(session, "plan_confirmed", user.id, enr.run_id, days=plan_days, pages_per_day=ppd)
    return segs


async def update_book_meta(session: AsyncSession, book: Book, *, title=None, author=None, spine_color=None) -> Book:
    if title is not None and title.strip():
        book.title = title.strip()[:300]
        book.title_norm = normalize_text(book.title)
    if author is not None:
        book.author = author.strip()[:300]
        book.author_norm = normalize_author(book.author)
    if spine_color is not None and spine_color.startswith("#") and len(spine_color) in (4, 7):
        book.spine_color = spine_color
    return book


async def delete_book_text(session: AsyncSession, book: Book) -> None:
    """Удаление книги: главы и тексты отрезков. Пересказы и конспект остаются, книга — запись на полке."""
    await session.execute(delete(Chapter).where(Chapter.book_id == book.id))
    await session.execute(update(Segment).where(Segment.book_id == book.id).values(text=None, summary=None))
    remove_file(book.file_path)
    book.file_path = None
    book.deleted_at = now()


async def delete_book(session: AsyncSession, enr: Enrollment | None, book: Book) -> None:
    if await _book_has_retellings(session, book.id):
        await delete_book_text(session, book)
    else:
        await session.delete(book)
    if enr and enr.book_id == book.id and enr.status in ("invited", "paid", "active"):
        enr.book_id = None
        enr.plan_days = None
        enr.plan_confirmed_at = None
        enr.plan_start_date = None
        enr.last_closed_day = None
