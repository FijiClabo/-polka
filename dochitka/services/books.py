"""Книги пользователя: сохранение разобранного файла, бумажная книга, план, удаление."""

from __future__ import annotations

import logging
import secrets
from datetime import date
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from books.parse import parse_book
from books.plan import (
    MAX_PLAN_DAYS,
    MIN_PLAN_DAYS,
    MINUTE_CHOICES,
    PlanError,
    PlanOption,
    build_chapter_plan,
    build_paper_plan,
    build_plan,
    chapter_groups,
    check_days,
    days_for_minutes,
    plan_options,
    total_minutes,
)
from books.types import WORDS_PER_MINUTE, BookParseError, ParsedBook, ParsedChapter
from core.rules import normalize_author, normalize_text
from db.models import Book, Chapter, Enrollment, Retelling, Segment, User
from services.common import RateLimit, log_event, now, today_for
from services.runs import can_start_plan, plan_start_for
from settings import get_settings

log = logging.getLogger(__name__)

SPINE_COLORS = ["#C9644F", "#7E9C7A", "#4F6F9F", "#E2B84F", "#8A6A85", "#4E6B57", "#B98B6E", "#3D4B66", "#D49A8C"]  # спокойные «книжные» корешки

# яркие цвета корешков из первой версии → спокойная палитра (книги, добавленные до смены дизайна)
_LEGACY_SPINES = {"#e2553f": "#C9644F", "#3e8e6e": "#7E9C7A", "#e98a6b": "#D49A8C", "#5b63d6": "#4F6F9F",
                  "#f2c14e": "#E2B84F", "#8c5bd6": "#8A6A85", "#2f7fb8": "#3D4B66", "#c2410c": "#B98B6E",
                  "#4d7c0f": "#4E6B57"}



# загрузок книг — до 10 в сутки на человека: разбор файла нагружает сервер
BOOK_UPLOADS = RateLimit(10, 24 * 3600)
BOOK_UPLOADS_TOO_MANY = "Сегодня книг загружено уже много. Попробуй завтра — или читай без файла."

def calm_color(color: str | None) -> str:
    return _LEGACY_SPINES.get((color or "").lower(), color or SPINE_COLORS[0])


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
                remove_file(old.file_path)
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


async def _parsed_chapters(session: AsyncSession, book: Book) -> list[ParsedChapter]:
    chapters = list(await session.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.order_no)))
    return [ParsedChapter(c.title, c.text.split("\n") if c.text else []) for c in chapters]


def custom_allowed(book: Book, enr: Enrollment) -> bool:
    """Своё время в день и «глава в день» — везде, кроме бесплатного спринта (он ровно на неделю)."""
    return enr.run.kind != "sprint" and book.source != "shared"


async def plan_choices(session: AsyncSession, user: User, book: Book, enr: Enrollment) -> dict:
    """Всё для выбора срока: готовые сроки, своё время в день, «глава в день»."""
    paper = book.source == "paper"
    total = total_minutes(book.total_pages, book.total_words, paper)
    out: dict = {"options": [o.__dict__ for o in options_for(book, enr)], "total_minutes": total,
                 "custom": custom_allowed(book, enr), "minute_options": [], "chapters": None,
                 "min_days": MIN_PLAN_DAYS, "max_days": min(MAX_PLAN_DAYS, book.total_pages) if paper else MAX_PLAN_DAYS,
                 "expected_start": None}
    if not out["custom"]:
        return out
    start = plan_start_for(enr.run, user) or today_for(user)
    out["expected_start"] = start.isoformat()
    for m in MINUTE_CHOICES:
        days = days_for_minutes(total, m)
        if MIN_PLAN_DAYS <= days <= out["max_days"]:
            out["minute_options"].append({"minutes": m, "days": days})
    if not paper and book.chapters_count >= 3:
        chapters = await _parsed_chapters(session, book)
        groups = chapter_groups(chapters)
        if MIN_PLAN_DAYS <= len(groups) <= MAX_PLAN_DAYS:
            mins = [max(1, round(sum(chapters[i].word_count for i in g) / WORDS_PER_MINUTE)) for g in groups]
            out["chapters"] = {"days": len(groups), "min_minutes": min(mins), "max_minutes": max(mins)}
    return out


async def confirm_plan(session: AsyncSession, user: User, enr: Enrollment, plan_days: int | None = None, *,
                       minutes: int | None = None, finish: date | None = None, by_chapter: bool = False) -> list[Segment]:
    """Подтвердить срок и построить отрезки. До подтверждения хранятся только главы.

    Срок задаётся одним из способов: готовый вариант (plan_days), минуты в день, дата финиша или «глава в день».
    """
    book = await session.get(Book, enr.book_id) if enr.book_id else None
    if book is None or book.parse_status != "ok":
        raise PlanError("Нет книги")
    if enr.status not in ("invited", "paid", "active"):
        raise PlanError("Этот забег уже закончен — начни новый с этой или другой книгой")
    if enr.plan_start_date is not None and (today_for(user) > enr.plan_start_date or await has_progress(session, enr)):
        # старая кнопка в чате не должна пересобрать идущий план и обнулить прогресс
        raise PlanError("План уже идёт — поменять темп можно на экране «Темп» (/pace)")
    if book.source == "shared":
        raise PlanError("Это план напарника: срок такой же, как у него. Свой срок можно выбрать, если загрузить свой файл "
                        "книги или добавить её без файла.")
    paper = book.source == "paper"
    custom = minutes is not None or finish is not None or by_chapter
    if custom and not custom_allowed(book, enr):
        raise PlanError("Спринт идёт ровно неделю — своё время в день появится в обычном забеге")
    if by_chapter and paper:
        raise PlanError("«Глава в день» работает для книг с файлом")
    mode = "days"
    if minutes is not None:
        plan_days, mode = check_days(days_for_minutes(total_minutes(book.total_pages, book.total_words, paper), minutes)), "minutes"
    elif finish is not None:
        start = plan_start_for(enr.run, user) or today_for(user)
        plan_days, mode = check_days((finish - start).days + 1), "date"
    elif by_chapter:
        mode = "chapter"
    elif plan_days not in {o.days for o in options_for(book, enr)}:
        raise PlanError("Такой срок для этой книги недоступен")
    if paper and plan_days and plan_days > book.total_pages:
        raise PlanError("Дней получилось больше, чем страниц — выбери больше минут в день")
    # сначала строим план (может не получиться), и только потом заменяем старые отрезки
    if paper:
        planned = build_paper_plan(book.total_pages, plan_days)
    else:
        parsed = await _parsed_chapters(session, book)
        if by_chapter:
            planned = build_chapter_plan(parsed)
            plan_days = check_days(len(planned))
        else:
            planned = build_plan(parsed, plan_days, has_chapters=book.chapters_count > 0)
    await session.execute(delete(Segment).where(Segment.book_id == book.id))
    await session.flush()
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
    await log_event(session, "plan_confirmed", user.id, enr.run_id, days=plan_days, pages_per_day=ppd, mode=mode,
                    minutes=minutes)
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
    """Удаление книги: главы и тексты отрезков. Книга остаётся записью на полке."""
    await session.execute(delete(Chapter).where(Chapter.book_id == book.id))
    await session.execute(update(Segment).where(Segment.book_id == book.id).values(text=None, summary=None))
    remove_file(book.file_path)
    book.file_path = None
    book.deleted_at = now()


async def delete_book(session: AsyncSession, enr: Enrollment | None, book: Book) -> None:
    if await _book_has_retellings(session, book.id):
        await delete_book_text(session, book)
    else:
        remove_file(book.file_path)
        await session.delete(book)
    if enr and enr.book_id == book.id and enr.status in ("invited", "paid", "active"):
        enr.book_id = None
        enr.plan_days = None
        enr.plan_confirmed_at = None
        enr.plan_start_date = None
        enr.last_closed_day = None
