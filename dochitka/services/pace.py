"""Свой темп чтения в пути: прочитано больше, прочитано не всё, другое время в день, к дате, глава в день, пауза.

Сданные отрезки не трогаем никогда: меняется только то, что ещё впереди. День плана считается от plan_start_date,
поэтому после пересборки дата старта сдвигается так, чтобы следующий несданный отрезок был отрезком «сегодня»
(или «завтра», если сегодня уже сдано). Прошлые итоги дней и стрик остаются как были.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from books.plan import (
    MAX_PLAN_DAYS,
    MINUTE_CHOICES,
    PlanError,
    PlannedSegment,
    build_paper_rest,
    build_rest_plan,
    chapter_groups,
    cut_text,
    days_for_minutes,
    segment_anchors,
    slice_book,
)
from books.types import PAPER_MINUTES_PER_PAGE, WORDS_PER_MINUTE, ParsedChapter, count_words
from core.days import deadline_day
from db.models import Book, Chapter, Club, Enrollment, Segment, User
from services.common import RateLimit, log_event, today_for
from services.progress import DayView, accepted_by_day, close_pending_days, load_view, paused_on

PAUSE_CHOICES = (1, 3, 7)
MAX_PAUSE_DAYS = 14
QUARTERS = {1: "четверть", 2: "половина", 3: "три четверти"}


class PaceError(ValueError):
    pass


# менять темп, ставить паузу и делить отрезок — до 8 раз в сутки: хватит на любые сомнения,
# но не даст перебором пересборок собрать «начни со слов / дочитай до» по всей книге
PACE_CHANGES = RateLimit(8, 24 * 3600)


def _count_change(user: User) -> None:
    if not PACE_CHANGES.hit(user.id):
        raise PaceError("Темп сегодня уже меняли много раз — давай завтра")


async def lock_plan(session: AsyncSession, enr: Enrollment) -> None:
    """Одна пересборка плана за раз: двойное нажатие ждёт первое и видит уже новый план (Postgres)."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(select(Enrollment.id).where(Enrollment.id == enr.id).with_for_update())
        await session.refresh(enr)


# --------------------------------------------------------------------------- что осталось


@dataclass
class Rest:
    view: DayView
    book: Book
    done: int  # сданные отрезки — всегда первые по порядку
    first_day: date  # с какого дня пойдёт остаток
    minutes: int  # минут чтения в остатке
    segments: list[Segment] = field(default_factory=list)  # оставшиеся отрезки
    earliest: date | None = None  # с какого дня можно начать паузу или новый темп

    @property
    def paper(self) -> bool:
        return self.book.source == "paper"


async def _segments(session: AsyncSession, book_id: int) -> list[Segment]:
    return list(await session.scalars(select(Segment).where(Segment.book_id == book_id).order_by(Segment.day_number)))


def _minutes_of(segs: list[Segment], paper: bool, book: Book) -> int:
    if paper:
        pages = sum(s.page_to - s.page_from + 1 for s in segs)
        return max(1, round(pages * PAPER_MINUTES_PER_PAGE))
    words = sum(s.word_count or 0 for s in segs)
    if not words and book.total_words and segs:  # копия плана без числа слов: по доле книги
        words = round(book.total_words * (segs[-1].pos_to - segs[0].pos_from))
    if not words:  # копия плана бумажной книги: слов нет, считаем по страницам
        pages = sum(s.page_to - s.page_from + 1 for s in segs)
        return max(1, round(pages * PAPER_MINUTES_PER_PAGE))
    return max(1, round(words / WORDS_PER_MINUTE))


def by_pages(book: Book) -> bool:
    """Темп считается по страницам: бумажная книга или копия её плана (слов там нет)."""
    return book.source == "paper" or (book.source == "shared" and not book.total_words)


SPRINT_LOCKED = ("Бесплатный спринт идёт ровно неделю, поэтому темп и паузу в нём не меняют. "
                 "Свой темп, паузы и «глава в день» — в личном забеге.")


def _no_sprint(enr: Enrollment) -> None:
    if enr.run.kind == "sprint":
        raise PaceError(SPRINT_LOCKED)


async def rest_of(session: AsyncSession, user: User, enr: Enrollment) -> Rest:
    if enr is None or not enr.plan_days or enr.book_id is None:
        raise PaceError("Сначала нужна книга с планом")
    if enr.status not in ("invited", "paid", "active"):
        raise PaceError("Этот забег уже закончен")
    view = await load_view(session, user, enr)
    if view.state in ("clarify", "checking"):
        raise PaceError("Сначала закончим с пересказом: ответь на вопрос или дождись проверки")
    book = await session.get(Book, enr.book_id)
    accepted = set((await accepted_by_day(session, enr)).keys())
    done = 0
    while done + 1 in accepted:
        done += 1
    segs = [s for s in await _segments(session, book.id) if s.day_number > done]
    today = today_for(user)
    # ближайший день, с которого можно что-то менять: сегодня, а если сегодня уже сдано — завтра; в паузе — после неё
    earliest = today + timedelta(days=1) if view.accepted_today else today
    if paused_on(enr, earliest):  # пауза идёт или начнётся завтра
        earliest = enr.paused_until
    # первый оставшийся отрезок — в свой день по плану (кто впереди, тот впереди), но не раньше earliest
    # (кто отстал, начинает с сегодняшнего дня — без долгов)
    first = earliest if enr.plan_start_date is None else max(earliest, enr.plan_start_date + timedelta(days=done))
    return Rest(view, book, done, first, _minutes_of(segs, book.source == "paper", book), segs, earliest)


def _finish(first_day: date, days: int) -> date:
    return first_day + timedelta(days=max(days, 1) - 1)


# --------------------------------------------------------------------------- превью для экрана «Темп»


async def _book_chapters(session: AsyncSession, book: Book) -> list[ParsedChapter] | None:
    """Главы с текстом: свои или исходной книги (у копии плана «читаем вместе»)."""
    book_id = book.shared_from_id if (book.source == "shared" and book.shared_from_id) else book.id
    if book.source == "paper":
        return None
    rows = list(await session.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.order_no)))
    chapters = [ParsedChapter(c.title, c.text.split("\n") if c.text else []) for c in rows]
    return chapters if any(c.paragraphs for c in chapters) else None


async def _has_chapters(session: AsyncSession, book: Book) -> bool:
    if book.source == "shared" and book.shared_from_id:
        src = await session.get(Book, book.shared_from_id)
        return bool(src and src.chapters_count)
    return bool(book.chapters_count)


async def pace_info(session: AsyncSession, user: User, enr: Enrollment) -> dict:
    """Всё для экрана «Темп»: как идёт сейчас и что будет при другом темпе."""
    r = await rest_of(session, user, enr)
    left = len(r.segments)
    v = r.view
    info: dict = {
        "plan_days": enr.plan_days, "done": r.done, "left": left, "paper": r.paper, "minutes_left": r.minutes,
        "minutes_per_day": round(r.minutes / left) if left else 0,
        "first_day": r.first_day.isoformat(),
        "finish": _finish(r.first_day, left).isoformat() if left else None,
        "plan_finish": (enr.plan_start_date + timedelta(days=enr.plan_days - 1)).isoformat() if enr.plan_start_date else None,
        "deadline": deadline_day(enr.plan_start_date, enr.plan_days, enr.run.grace_days).isoformat()
        if enr.plan_start_date else None,
        "behind": v.behind, "ahead": v.ahead, "started": bool(enr.plan_start_date and today_for(user) >= enr.plan_start_date),
        "can_pause": enr.plan_start_date is not None and enr.run.kind != "sprint",
        "paused_until": v.paused_until.isoformat() if v.paused_until else None,
        "locked": SPRINT_LOCKED if enr.run.kind == "sprint" else None,
        "minute_options": [], "pause_options": [], "chapters": None, "pair": None, "club": None,
    }
    if not left or enr.run.kind == "sprint":
        return info
    for m in MINUTE_CHOICES:
        days = days_for_minutes(r.minutes, m)
        if days > MAX_PLAN_DAYS or (by_pages(r.book) and days > sum(s.page_to - s.page_from + 1 for s in r.segments)):
            continue
        info["minute_options"].append({"minutes": m, "days": days, "finish": _finish(r.first_day, days).isoformat()})
    if enr.plan_start_date is not None:
        cur_finish = _finish(r.first_day, left)
        used = (enr.paused_until - enr.paused_from).days if enr.paused_until and enr.paused_until > today_for(user) else 0
        info["pause_options"] = [{"days": d, "finish": (cur_finish + timedelta(days=d)).isoformat()}
                                 for d in PAUSE_CHOICES if used + d <= MAX_PAUSE_DAYS]
    if not r.paper and await _has_chapters(session, r.book):
        chapters = await _book_chapters(session, r.book)
        if chapters:
            try:
                part = slice_book(chapters, r.segments[0].pos_from)
                groups = chapter_groups(part.chapters)
            except PlanError:
                groups = []
            if len(groups) >= 2:
                mins = [max(1, round(sum(part.chapters[i].word_count for i in g) / WORDS_PER_MINUTE)) for g in groups]
                info["chapters"] = {"days": len(groups), "min_minutes": min(mins), "max_minutes": max(mins),
                                    "finish": _finish(r.first_day, len(groups)).isoformat()}
    from services.social import get_pair_for

    _, partner, _ = await get_pair_for(session, enr)
    if partner is not None:
        info["pair"] = {"name": partner.display_name}
    if enr.club_id:
        club = await session.get(Club, enr.club_id)
        if club and club.active:
            info["club"] = {"title": club.title, "meet_date": club.meet_date.isoformat() if club.meet_date else None}
    return info


# --------------------------------------------------------------------------- пересборка остатка


async def _write_rest(session: AsyncSession, book: Book, done: int, planned: list[PlannedSegment], keep_text: bool) -> None:
    await session.execute(delete(Segment).where(Segment.book_id == book.id, Segment.day_number > done))
    await session.flush()
    for p in planned:
        anchors = None if keep_text else segment_anchors(p.text)
        session.add(Segment(
            book_id=book.id, day_number=p.day_number, title=p.title, page_from=p.page_from, page_to=p.page_to,
            pos_from=p.pos_from, pos_to=p.pos_to, word_count=p.word_count, text=p.text if keep_text else None,
            start_words=anchors[0][:200] if anchors else None, end_words=anchors[1][:200] if anchors else None,
        ))
    await session.flush()


def _even_rest(segs: list[Segment], days: int, first: int) -> list[PlannedSegment]:
    """Копия плана без текста исходной книги: делим остаток поровну по доле книги."""
    a, b = segs[0].pos_from, segs[-1].pos_to
    p0, p1 = segs[0].page_from, segs[-1].page_to
    out = []
    for i in range(days):
        f0, f1 = i / days, (i + 1) / days
        pf = p0 + round(f0 * (p1 - p0 + 1))
        pt = max(pf, p0 + round(f1 * (p1 - p0 + 1)) - 1)
        out.append(PlannedSegment(first + i, f"Отрезок {first + i}", pf, pt, round(a + f0 * (b - a), 6),
                                  round(a + f1 * (b - a), 6), 0, None))
    out[-1].pos_to = b
    return out


async def _move_start(enr: Enrollment, user: User, r: Rest) -> None:
    """Сдвинуть старт так, чтобы первый оставшийся отрезок пришёлся на r.first_day (догоняющему — без долгов).

    Раньше прежнего старт не переносим: прошедшие дни до старта стали бы днями плана — с ложной заморозкой
    или пропуском в календаре. Кто впереди плана, так и остаётся впереди.
    """
    if enr.plan_start_date is None:
        return
    if today_for(user) < enr.plan_start_date and r.view.state != "paused":
        return  # план ещё не начался — старт прежний
    enr.plan_start_date = max(enr.plan_start_date, r.first_day - timedelta(days=r.done))


async def replan(session: AsyncSession, user: User, enr: Enrollment, *, minutes: int | None = None,
                 finish: date | None = None, by_chapter: bool = False) -> Rest:
    """Пересобрать оставшиеся отрезки: другое время в день, дочитать к дате или глава в день."""
    await lock_plan(session, enr)
    _no_sprint(enr)
    r = await rest_of(session, user, enr)
    if not r.segments:
        raise PaceError("Все отрезки уже сданы")
    _count_change(user)
    if by_chapter:
        days = None
    elif minutes is not None:
        try:
            days = days_for_minutes(r.minutes, minutes)
        except PlanError as e:
            raise PaceError(str(e)) from e
    elif finish is not None:
        days = (finish - r.first_day).days + 1
        if days < 1:
            raise PaceError("Эта дата уже прошла — выбери дату позже")
    else:
        raise PaceError("Выбери время в день, дату или «глава в день»")
    if days is not None and days > MAX_PLAN_DAYS:
        raise PaceError("Больше года — это слишком долго")
    first = r.done + 1
    book = r.book
    try:
        if book.source == "paper":
            if by_chapter:
                raise PaceError("«Глава в день» работает для книг с файлом")
            planned = build_paper_rest(r.segments[0].page_from, r.segments[-1].page_to, days, first)
            await _write_rest(session, book, r.done, planned, keep_text=True)
        else:
            chapters = await _book_chapters(session, book)
            has_ch = await _has_chapters(session, book)
            if chapters:
                planned = build_rest_plan(chapters, r.segments[0].pos_from, days, has_ch, first, by_chapter=by_chapter)
            elif by_chapter:
                raise PaceError("Текста книги больше нет, поэтому «глава в день» недоступна")
            else:
                planned = _even_rest(r.segments, days, first)
            await _write_rest(session, book, r.done, planned, keep_text=book.source != "shared")
    except PlanError as e:
        raise PaceError(f"{e}. Попробуй другой темп") from e
    enr.plan_days = r.done + len(planned)
    await _move_start(enr, user, r)
    if enr.more_day == today_for(user):
        enr.more_day = None
    await log_event(session, "pace_changed", user.id, enr.run_id, mode="chapter" if by_chapter else
                    ("minutes" if minutes is not None else "date"), minutes=minutes, left=len(planned),
                    was_left=len(r.segments), behind=r.view.behind, ahead=r.view.ahead)
    await session.flush()
    return await rest_of(session, user, enr)


async def pause(session: AsyncSession, user: User, enr: Enrollment, days: int) -> date:
    """Пауза на days дней с ближайшего несданного дня. Дни паузы не сгорают, план сдвигается на их число.

    Возвращает новый финиш — тот же, что показан на кнопке паузы.
    """
    if not 1 <= days <= MAX_PAUSE_DAYS:
        raise PaceError(f"Пауза — от 1 до {MAX_PAUSE_DAYS} дней")
    _no_sprint(enr)
    await lock_plan(session, enr)
    if enr.plan_start_date is None:
        raise PaceError("План ещё не начался — пауза не нужна")
    await close_pending_days(session, enr, user)  # прошедшие дни закрываются по прежнему плану
    r = await rest_of(session, user, enr)
    if not r.segments:
        raise PaceError("Все отрезки уже сданы")
    if enr.paused_until and enr.paused_until > today_for(user):  # пауза идёт или назначена с завтра — продлеваем
        if (enr.paused_until - enr.paused_from).days + days > MAX_PAUSE_DAYS:
            raise PaceError(f"Пауза — не больше {MAX_PAUSE_DAYS} дней подряд")
        enr.paused_until += timedelta(days=days)
    else:
        start = max(r.earliest, enr.plan_start_date)  # план ещё не начался — пауза со дня старта
        enr.paused_from, enr.paused_until = start, start + timedelta(days=days)
    _count_change(user)
    enr.plan_start_date += timedelta(days=days)
    await log_event(session, "pace_pause", user.id, enr.run_id, days=days, behind=r.view.behind)
    await session.flush()
    after = await rest_of(session, user, enr)
    return _finish(after.first_day, len(after.segments))


async def resume(session: AsyncSession, user: User, enr: Enrollment) -> date:
    """Закончить паузу раньше: план возвращается на оставшиеся дни паузы. Прошедшие дни паузы остаются паузой."""
    await lock_plan(session, enr)
    today = today_for(user)
    if not (enr.paused_until and enr.paused_from and enr.paused_until > today):
        raise PaceError("Паузы сейчас нет")
    start = max(today, enr.paused_from)
    shift = (enr.paused_until - start).days
    enr.plan_start_date -= timedelta(days=shift)
    if start <= enr.paused_from:
        enr.paused_from = enr.paused_until = None
    else:
        enr.paused_until = start
    await log_event(session, "pace_resume", user.id, enr.run_id, days_back=shift)
    await session.flush()
    r = await rest_of(session, user, enr)
    return _finish(r.first_day, len(r.segments))


# --------------------------------------------------------------------------- прочитано не всё


@dataclass
class Partial:
    segment: Segment  # сегодняшний отрезок — теперь только прочитанная часть
    rest: str  # tomorrow | day
    plan_days: int


def _page_cut(seg: Segment, share: float | None, page: int | None) -> int:
    """Последняя прочитанная страница."""
    if seg.page_to <= seg.page_from:
        raise PaceError("В отрезке одна страница — делить нечего")
    if page is None:
        page = seg.page_from - 1 + round(share * (seg.page_to - seg.page_from + 1))
        page = max(seg.page_from, min(seg.page_to - 1, page))
    if not seg.page_from <= page < seg.page_to:
        raise PaceError(f"Нужна страница от {seg.page_from} до {seg.page_to - 1}")
    return page


async def split_today(session: AsyncSession, user: User, enr: Enrollment, *, quarter: int | None = None,
                      page: int | None = None, rest: str = "tomorrow") -> Partial:
    """Прочитано не всё: сегодня пересказываем прочитанное, остаток — завтра или отдельным днём."""
    if rest not in ("tomorrow", "day"):
        raise PaceError("Остаток — завтра или отдельным днём")
    if page is None and quarter not in QUARTERS:
        raise PaceError("Сколько прочитано: четверть, половина или три четверти")
    await lock_plan(session, enr)
    view = await load_view(session, user, enr)
    if view.state != "to_read" or view.extra or view.segment is None:
        raise PaceError("Делить можно отрезок, который ждёт пересказа сегодня")
    seg = view.segment
    book = view.book
    if await _split_today_already(session, user, enr, seg.day_number):
        raise PaceError("Сегодняшний отрезок уже поделён — перескажи прочитанное")
    _count_change(user)
    share = (quarter or 2) / 4
    nxt = await session.scalar(select(Segment).where(Segment.book_id == book.id, Segment.day_number == seg.day_number + 1))
    if nxt is None:
        rest = "day"  # последний отрезок — остаток только отдельным днём
    elif enr.run.kind == "sprint":
        rest = "tomorrow"  # спринт ровно на неделю: остаток — к следующему отрезку

    head_text = tail_text = None
    head_anchor = tail_anchor = None
    if by_pages(book):
        cut_page = _page_cut(seg, share, page)
        real = (cut_page - seg.page_from + 1) / (seg.page_to - seg.page_from + 1)
        head_words = tail_words = 0
    else:
        text = seg.text
        if not text:  # копия плана: текст исходной книги нужен только чтобы найти слова на границе
            chapters = await _book_chapters(session, book)
            if chapters:
                try:
                    text = "\n".join(p for c in slice_book(chapters, seg.pos_from, seg.pos_to).chapters for p in c.paragraphs)
                except PlanError:
                    text = None
        if text:
            try:
                cut = cut_text(text, share)
            except PlanError as e:
                raise PaceError(str(e)) from e
            real = cut.share
            head_words, tail_words = count_words(cut.head), count_words(cut.tail)
            if seg.text:
                head_text, tail_text = cut.head, cut.tail
            else:
                head_anchor, tail_anchor = segment_anchors(cut.head), segment_anchors(cut.tail)
        else:
            real = share
            head_words = round((seg.word_count or 0) * share)
            tail_words = (seg.word_count or 0) - head_words
        pages = seg.page_to - seg.page_from + 1
        cut_page = max(seg.page_from, min(seg.page_to - 1, seg.page_from - 1 + round(real * pages))) if pages > 1 else seg.page_from
    pos_cut = round(seg.pos_from + (seg.pos_to - seg.pos_from) * real, 6)

    tail = Segment(
        book_id=book.id, day_number=seg.day_number + 1, title=seg.title, page_from=min(cut_page + 1, seg.page_to),
        page_to=seg.page_to, pos_from=pos_cut, pos_to=seg.pos_to, word_count=tail_words, text=tail_text,
        start_words=tail_anchor[0][:200] if tail_anchor else None, end_words=seg.end_words,
    )
    # сегодняшний отрезок — только прочитанное
    seg.page_to, seg.pos_to, seg.word_count, seg.summary = cut_page, pos_cut, head_words, None
    if head_text is not None:
        seg.text = head_text
    elif not seg.text:  # копия плана: новые слова на границе (у остатка конец — прежний)
        seg.end_words = head_anchor[1][:200] if head_anchor else None
    if book.source == "paper":
        seg.title = f"стр. {seg.page_from}–{seg.page_to}"
        tail.title = f"стр. {tail.page_from}–{tail.page_to}"

    if rest == "tomorrow":
        nxt.pos_from, nxt.page_from = tail.pos_from, tail.page_from
        nxt.word_count = (nxt.word_count or 0) + tail.word_count
        nxt.summary = None
        if nxt.text is not None and tail.text:
            nxt.text = f"{tail.text}\n{nxt.text}"
        if not nxt.text:
            nxt.start_words = tail.start_words
        if book.source == "paper":
            nxt.title = f"стр. {nxt.page_from}–{nxt.page_to}"
        elif nxt.title != seg.title:
            nxt.title = f"{seg.title} — {nxt.title}"[:300]
    else:
        # освобождаем место: все дни после сегодняшнего сдвигаются на один
        await session.execute(update(Segment).where(Segment.book_id == book.id, Segment.day_number > seg.day_number)
                              .values(day_number=Segment.day_number + 100000))
        await session.flush()
        await session.execute(update(Segment).where(Segment.book_id == book.id, Segment.day_number > 100000)
                              .values(day_number=Segment.day_number - 100000 + 1))
        if book.source != "paper" and tail.title == seg.title:
            tail.title = f"{seg.title} (продолжение)"[:300]
        session.add(tail)
        enr.plan_days += 1
    await log_event(session, "pace_partial", user.id, enr.run_id, quarter=quarter, page=page is not None, rest=rest,
                    day=seg.day_number, on=today_for(user).isoformat())
    await session.flush()
    await session.refresh(seg)
    return Partial(seg, rest, enr.plan_days)


async def _split_today_already(session: AsyncSession, user: User, enr: Enrollment, day_number: int) -> bool:
    """Отрезок дня делим один раз: повторное нажатие (или второй запрос) не режет его ещё раз."""
    from db.models import Event

    rows = await session.scalars(select(Event.payload).where(
        Event.user_id == user.id, Event.type == "pace_partial", Event.run_id == enr.run_id).order_by(Event.id.desc()).limit(10))
    return any((p or {}).get("day") == day_number and (p or {}).get("on") == today_for(user).isoformat() for p in rows)


def partial_options(seg: Segment, paper: bool) -> list[dict]:
    """Варианты «сколько прочитано» для кнопок: у бумажной книги — до какой страницы."""
    out = []
    for q, label in QUARTERS.items():
        if paper:
            pages = seg.page_to - seg.page_from + 1
            if pages < 2:
                return []
            p = max(seg.page_from, min(seg.page_to - 1, seg.page_from - 1 + round(q / 4 * pages)))
            if any(o.get("page") == p for o in out):
                continue
            out.append({"quarter": q, "page": p, "label": f"до стр. {p}"})
        else:
            out.append({"quarter": q, "label": label})
    return out


# --------------------------------------------------------------------------- прочитано больше


async def ask_more(session: AsyncSession, user: User, enr: Enrollment) -> Segment | None:
    """«Перескажу ещё один»: день сдан, но прочитано больше — открываем следующий отрезок до конца дня."""
    view = await load_view(session, user, enr)
    if view.state == "to_read" and view.extra:
        return view.segment  # следующий уже открыт
    if not view.can_read_more:
        return None
    enr.more_day = view.today
    await log_event(session, "read_more", user.id, enr.run_id, day=view.next_segment.day_number, behind=view.behind)
    await session.flush()
    return view.next_segment
