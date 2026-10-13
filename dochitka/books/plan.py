"""Разбивка книги на отрезки по дням и варианты срока. Без зависимостей от базы."""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass

from books.types import (
    CHARS_PER_PAGE,
    PAPER_MINUTES_PER_PAGE,
    SUBHEADING_MARK,
    WORDS_PER_MINUTE,
    ParsedChapter,
    count_words,
)

DURATIONS = (21, 30, 45, 60)
SHORT_DURATIONS = (7, 14)
SPRINT_DAYS = 7
MIN_PAGES_PER_DAY = 3
MAX_PAGES_PER_DAY = 20
RECOMMENDED_PAGES = 11  # «ближайший к 10–12 страницам в день»
LONG_BOOK_PAGES = 1200
SHORT_BOOK_PAGES = 63
TOLERANCE = 0.4  # допустимое отклонение объёма отрезка от среднего при выборе границы главы
MIN_PLAN_DAYS = 3  # своё время в день: границы срока
MAX_PLAN_DAYS = 365
MINUTE_CHOICES = (10, 15, 20, 30, 45, 60)
MIN_MINUTES, MAX_MINUTES = 5, 180
MIN_CHAPTER_SHARE = 0.35  # глава короче 35% медианной идёт в один день со следующей

_SENTENCE_END = re.compile(r"(?<=[.!?…»\"])\s+")


@dataclass
class PlannedSegment:
    day_number: int
    title: str
    page_from: int
    page_to: int
    pos_from: float
    pos_to: float
    word_count: int
    text: str | None


@dataclass
class PlanOption:
    days: int
    pages_per_day: float
    minutes_per_day: int
    recommended: bool = False
    warning: str | None = None


class PlanError(ValueError):
    pass


# --------------------------------------------------------------------------- варианты срока


def plan_options(total_pages: int, total_words: int | None = None, *, paper: bool = False, sprint: bool = False) -> list[PlanOption]:
    """Сроки из 21/30/45/60, при которых нагрузка 3–20 страниц в день. Рекомендуемый — ближе к 10–12."""

    def minutes(days: int) -> int:
        if paper or not total_words:
            return max(1, round(total_pages * PAPER_MINUTES_PER_PAGE / days))
        return max(1, round(total_words / WORDS_PER_MINUTE / days))

    def opt(days: int, warning: str | None = None) -> PlanOption:
        return PlanOption(days, round(total_pages / days, 1), minutes(days), warning=warning)

    if total_pages <= 0:
        return []
    if sprint:
        o = opt(SPRINT_DAYS)
        o.recommended = True
        if total_pages / SPRINT_DAYS > MAX_PAGES_PER_DAY:
            o.warning = "Для спринта книга длинновата: больше 20 страниц в день."
        return [o]

    if total_pages < SHORT_BOOK_PAGES:
        options = [opt(d) for d in SHORT_DURATIONS]
    elif total_pages > LONG_BOOK_PAGES:
        options = [opt(60, f"Книга длинная: около {_pages_per_day(total_pages)} в день. "
                           "Это заметная нагрузка — можно выбрать книгу короче.")]
    else:
        options = [opt(d) for d in DURATIONS if MIN_PAGES_PER_DAY <= total_pages / d <= MAX_PAGES_PER_DAY]
        if not options:  # на всякий случай: ближайший по нагрузке
            best = min(DURATIONS, key=lambda d: abs(total_pages / d - RECOMMENDED_PAGES))
            options = [opt(best)]

    best = min(options, key=lambda o: (abs(o.pages_per_day - RECOMMENDED_PAGES), -o.days))
    best.recommended = True
    return options


def total_minutes(total_pages: int, total_words: int | None, paper: bool) -> int:
    """Сколько минут чтения во всей книге — для «своего времени в день»."""
    if paper or not total_words:
        return max(1, round(total_pages * PAPER_MINUTES_PER_PAGE))
    return max(1, round(total_words / WORDS_PER_MINUTE))


def days_for_minutes(minutes_total: int, per_day: int) -> int:
    """Сколько дней займёт книга (или остаток), если читать per_day минут в день."""
    if per_day < MIN_MINUTES or per_day > MAX_MINUTES:
        raise PlanError(f"Время в день — от {MIN_MINUTES} до {MAX_MINUTES} минут")
    return max(1, math.ceil(minutes_total / per_day))


def check_days(days: int) -> int:
    if days < MIN_PLAN_DAYS:
        raise PlanError(f"Слишком быстро: меньше {MIN_PLAN_DAYS} дней. Выбери меньше минут в день")
    if days > MAX_PLAN_DAYS:
        raise PlanError("Больше года — это слишком долго. Выбери больше минут в день")
    return days


# --------------------------------------------------------------------------- бумажная книга


def build_paper_plan(total_pages: int, plan_days: int) -> list[PlannedSegment]:
    if total_pages < plan_days:
        raise PlanError("Страниц меньше, чем дней в плане")
    segs: list[PlannedSegment] = []
    prev = 0
    for i in range(1, plan_days + 1):
        end = round(i * total_pages / plan_days)
        end = max(end, prev + 1)
        segs.append(
            PlannedSegment(
                day_number=i,
                title=f"стр. {prev + 1}–{end}",
                page_from=prev + 1,
                page_to=end,
                pos_from=prev / total_pages,
                pos_to=end / total_pages,
                word_count=0,
                text=None,
            )
        )
        prev = end
    segs[-1].pos_to = 1.0
    return segs


# --------------------------------------------------------------------------- книга из файла


@dataclass
class _Para:
    chapter: int
    text: str
    chars: int


def _split_long_paragraph(text: str, max_chars: int) -> list[str]:
    """Только для патологических абзацев длиннее целого отрезка: режем по предложениям."""
    sentences = _SENTENCE_END.split(text)
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > max_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        out.append(cur)
    # если предложений нет вовсе — режем по словам
    final: list[str] = []
    for chunk in out:
        while len(chunk) > max_chars * 1.5:
            cut = chunk.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            final.append(chunk[:cut])
            chunk = chunk[cut:].lstrip()
        final.append(chunk)
    return [x for x in final if x.strip()]


def build_plan(chapters: list[ParsedChapter], plan_days: int, has_chapters: bool = True) -> list[PlannedSegment]:
    """Разбить книгу на plan_days отрезков примерно равного объёма.

    Границы ставим на границах глав, если отрезок остаётся в пределах ±40% от среднего;
    иначе — на границе абзаца (внутри абзаца никогда не режем). Короткие главы объединяются,
    длинные делятся на части: «Глава 4 (часть 2 из 3)».
    """
    if plan_days < 1:
        raise PlanError("plan_days < 1")
    total_raw = sum(len(p) for c in chapters for p in c.paragraphs)
    if total_raw == 0:
        raise PlanError("Пустая книга")
    target0 = total_raw / plan_days

    paras: list[_Para] = []
    for ci, ch in enumerate(chapters):
        for p in ch.paragraphs:
            if not p:
                continue
            if len(p) > target0 * 0.9 and len(p) > 4000:
                for piece in _split_long_paragraph(p, max(int(target0 / 3), 1500)):
                    paras.append(_Para(ci, piece, len(piece)))
            else:
                paras.append(_Para(ci, p, len(p)))
    n_par = len(paras)
    if n_par < plan_days:
        raise PlanError("Слишком мало абзацев для такого срока")

    cum = [0]
    for p in paras:
        cum.append(cum[-1] + p.chars)
    total = cum[-1]
    chapter_starts = [i for i in range(1, n_par) if paras[i].chapter != paras[i - 1].chapter] if has_chapters else []

    cuts: list[int] = []
    start = 0
    for k in range(plan_days - 1):
        remaining_days = plan_days - k
        target = (total - cum[start]) / remaining_days
        ideal = cum[start] + target
        lo, hi = cum[start] + (1 - TOLERANCE) * target, cum[start] + (1 + TOLERANCE) * target
        max_cut = n_par - (remaining_days - 1)  # после разреза должно остаться достаточно абзацев
        best: int | None = None
        if chapter_starts:
            a = bisect.bisect_left(chapter_starts, start + 1)
            b = bisect.bisect_right(chapter_starts, max_cut)
            cands = [i for i in chapter_starts[a:b] if lo <= cum[i] <= hi]
            if cands:
                best = min(cands, key=lambda i: abs(cum[i] - ideal))
        if best is None:
            j = bisect.bisect_left(cum, ideal, lo=start + 1, hi=max_cut + 1)
            cands = [i for i in (j - 1, j) if start + 1 <= i <= max_cut]
            best = min(cands, key=lambda i: abs(cum[i] - ideal)) if cands else start + 1
        cuts.append(best)
        start = best

    bounds = [0, *cuts, n_par]
    ranges = [(bounds[i], bounds[i + 1]) for i in range(plan_days)]

    # сколько отрезков касается каждой главы (для «часть i из k»)
    chapter_segments: dict[int, list[int]] = {}
    for si, (a, b) in enumerate(ranges):
        for ci in sorted({paras[x].chapter for x in range(a, b)}):
            chapter_segments.setdefault(ci, []).append(si)

    def chapter_label(ci: int, si: int) -> str:
        title = chapters[ci].title.strip() or f"Глава {ci + 1}"
        segs = chapter_segments.get(ci, [])
        if len(segs) > 1:
            return f"{title} (часть {segs.index(si) + 1} из {len(segs)})"
        return title

    result: list[PlannedSegment] = []
    for si, (a, b) in enumerate(ranges):
        texts = [paras[x].text for x in range(a, b)]
        if has_chapters:
            c_first, c_last = paras[a].chapter, paras[b - 1].chapter
            if c_first == c_last:
                title = chapter_label(c_first, si)
            else:
                title = f"{chapter_label(c_first, si)} — {chapter_label(c_last, si)}"
        else:
            title = f"День {si + 1}"
        # «условные страницы» считаем по знакам с пробелами (абзацы разделены переводом строки)
        chars_from = cum[a] + a
        chars_to = cum[b] + b - 1
        total_with_breaks = total + n_par - 1
        page_from = chars_from // CHARS_PER_PAGE + 1
        page_to = max(page_from, math.ceil(chars_to / CHARS_PER_PAGE))
        result.append(
            PlannedSegment(
                day_number=si + 1,
                title=title[:300],
                page_from=page_from,
                page_to=page_to,
                pos_from=round(chars_from / total_with_breaks, 6),
                pos_to=round(min(chars_to / total_with_breaks, 1.0), 6),
                word_count=sum(count_words(t) for t in texts),
                text="\n".join(texts),
            )
        )
    result[-1].pos_to = 1.0
    return result


def segment_minutes(word_count: int, pages: int, paper: bool) -> int:
    if paper or not word_count:
        return max(1, round(pages * PAPER_MINUTES_PER_PAGE))
    return max(1, round(word_count / WORDS_PER_MINUTE))


ANCHOR_WORDS = 6


def segment_place(page_from: int, page_to: int, pos_from: float, pos_to: float, paper: bool) -> str:
    """Где отрезок: у бумажной книги — страницы её издания, у файла — проценты книги.

    Страницы файла условные и не совпадут ни с бумажным изданием, ни с другим приложением, а проценты совпадут.
    """
    if paper:
        return f"стр. {page_from}–{page_to}"
    a, b = round(pos_from * 100), max(1, round(pos_to * 100))
    return f"{b}% книги" if a >= b else f"{a}–{b}% книги"


def segment_anchors(text: str | None, words: int = ANCHOR_WORDS) -> tuple[str, str] | None:
    """Первые и последние слова отрезка: по ним место находится в любом издании и любом приложении."""
    if not text:
        return None
    lines = [x.strip() for x in text.split("\n") if x.strip() and not x.startswith(SUBHEADING_MARK)]
    ws = " ".join(lines).split()
    if len(ws) <= words * 2:
        return None
    start = " ".join(ws[:words]).rstrip(" ,;:—–-")
    end = " ".join(ws[-words:]).lstrip(" ,;:—–-")
    return f"{start}…", f"…{end}"


def anchors_of(text: str | None, start_words: str | None = None, end_words: str | None = None) -> tuple[str, str] | None:
    """Метки отрезка: из текста, а у копии плана друга — сохранённые при копировании."""
    return segment_anchors(text) or ((start_words, end_words) if start_words and end_words else None)


def strip_marks(text: str) -> str:
    return "\n".join(line[len(SUBHEADING_MARK):] if line.startswith(SUBHEADING_MARK) else line for line in text.split("\n"))


def _pages_per_day(total_pages: int) -> str:
    n = math.ceil(total_pages / 60)
    return f"{n} {'страницы' if n % 10 == 1 and n % 100 != 11 else 'страниц'}"  # «около 21 страницы»


# --------------------------------------------------------------------------- глава в день и остаток книги


def _flat(chapters: list[ParsedChapter]) -> list[tuple[int, str]]:
    return [(ci, p) for ci, ch in enumerate(chapters) for p in ch.paragraphs if p]


def _offsets(flat: list[tuple[int, str]]) -> tuple[list[int], int]:
    """Начало каждого абзаца в знаках с учётом переводов строки и общий объём — так же, как в build_plan."""
    offs, at = [], 0
    for _, t in flat:
        offs.append(at)
        at += len(t) + 1
    return offs, max(at - 1, 1)


def _nearest_start(offs: list[int], total: int, frac: float) -> int:
    """Индекс абзаца, с которого начинается часть книги после доли frac (ближайшая граница абзаца)."""
    if frac <= 0:
        return 0
    if frac >= 1:
        return len(offs)
    target = frac * total
    i = bisect.bisect_left(offs, target)
    cands = [j for j in (i - 1, i) if 0 <= j < len(offs)]
    if not cands:
        return len(offs)
    best = min(cands, key=lambda j: abs(offs[j] - target))
    return best


@dataclass
class BookPart:
    chapters: list[ParsedChapter]
    pos_from: float  # точные доли книги, где часть начинается и кончается (по границам абзацев)
    pos_to: float
    page_offset: int  # сколько условных страниц до начала части


def slice_book(chapters: list[ParsedChapter], pos_from: float, pos_to: float = 1.0) -> BookPart:
    """Часть книги между долями pos_from и pos_to — по ближайшим границам абзацев, с названиями глав.

    Глава, от которой взято не начало, называется «… (продолжение)».
    """
    flat = _flat(chapters)
    if not flat:
        raise PlanError("Пустая книга")
    offs, total = _offsets(flat)
    a = _nearest_start(offs, total, pos_from)
    b = _nearest_start(offs, total, pos_to) if pos_to < 1 else len(flat)
    if b <= a:
        raise PlanError("Слишком маленькая часть книги")
    out: list[ParsedChapter] = []
    last_ci = None
    for k in range(a, b):
        ci, text = flat[k]
        if ci != last_ci:
            title = chapters[ci].title.strip() or f"Глава {ci + 1}"
            first_of_chapter = k == 0 or flat[k - 1][0] != ci
            out.append(ParsedChapter(title if first_of_chapter else f"{title} (продолжение)", []))
            last_ci = ci
        out[-1].paragraphs.append(text)
    end = total if b >= len(flat) else offs[b] - 1
    return BookPart(out, round(offs[a] / total, 6), round(min(end / total, 1.0), 6), offs[a] // CHARS_PER_PAGE)


def _place_part(planned: list[PlannedSegment], part: BookPart, first_day: int) -> list[PlannedSegment]:
    """Перенести отрезки, собранные по части книги, на их место во всей книге."""
    span = part.pos_to - part.pos_from
    for p in planned:
        p.day_number += first_day - 1
        p.pos_from = round(part.pos_from + p.pos_from * span, 6)
        p.pos_to = round(part.pos_from + p.pos_to * span, 6)
        p.page_from += part.page_offset
        p.page_to += part.page_offset
    if planned and part.pos_to >= 1:
        planned[-1].pos_to = 1.0
    return planned


def chapter_groups(chapters: list[ParsedChapter]) -> list[list[int]]:
    """Главы по дням: каждая глава — свой день, совсем короткие — вместе со следующей."""
    sizes = [ch.char_count for ch in chapters]
    filled = sorted(x for x in sizes if x > 0)
    if not filled:
        return []
    median = filled[len(filled) // 2]
    groups: list[list[int]] = []
    cur: list[int] = []
    for i, size in enumerate(sizes):
        if size <= 0:
            continue
        cur.append(i)
        if sum(sizes[j] for j in cur) >= MIN_CHAPTER_SHARE * median:
            groups.append(cur)
            cur = []
    if cur:
        if groups:
            groups[-1].extend(cur)
        else:
            groups.append(cur)
    return groups


def build_chapter_plan(chapters: list[ParsedChapter]) -> list[PlannedSegment]:
    """План «глава в день». Объём дней разный — зато каждый день целая глава."""
    groups = chapter_groups(chapters)
    if len(groups) < 2:
        raise PlanError("Для плана «глава в день» нужно хотя бы две главы")
    flat_total = sum(ch.char_count for ch in chapters if ch.paragraphs) + max(sum(1 for ch in chapters if ch.paragraphs) - 1, 0)
    total = max(flat_total, 1)
    result: list[PlannedSegment] = []
    at = 0
    for di, g in enumerate(groups):
        paras = [p for ci in g for p in chapters[ci].paragraphs if p]
        chars = sum(len(p) for p in paras) + max(len(paras) - 1, 0)
        names = [chapters[ci].title.strip() or f"Глава {ci + 1}" for ci in g]
        title = names[0] if len(names) == 1 else f"{names[0]} — {names[-1]}"
        start, end = at, at + chars
        page_from = start // CHARS_PER_PAGE + 1
        result.append(PlannedSegment(
            day_number=di + 1, title=title[:300], page_from=page_from,
            page_to=max(page_from, math.ceil(end / CHARS_PER_PAGE)), pos_from=round(start / total, 6),
            pos_to=round(min(end / total, 1.0), 6), word_count=sum(count_words(p) for p in paras), text="\n".join(paras),
        ))
        at = end + 1
    result[-1].pos_to = 1.0
    return result


def build_rest_plan(chapters: list[ParsedChapter], pos_from: float, plan_days: int | None, has_chapters: bool,
                    first_day: int, *, by_chapter: bool = False) -> list[PlannedSegment]:
    """План для остатка книги с доли pos_from: новые отрезки получают номера дней с first_day."""
    part = slice_book(chapters, pos_from)
    if by_chapter:
        planned = build_chapter_plan(part.chapters)
    else:
        planned = build_plan(part.chapters, plan_days or 1, has_chapters)
    planned = _place_part(planned, part, first_day)
    if not has_chapters and not by_chapter:
        for p in planned:
            p.title = f"День {p.day_number}"
    return planned


def build_paper_rest(page_from: int, total_pages: int, plan_days: int, first_day: int) -> list[PlannedSegment]:
    """Остаток бумажной книги со страницы page_from — на plan_days дней."""
    left = total_pages - page_from + 1
    if left < plan_days:
        raise PlanError("Страниц осталось меньше, чем дней — выбери меньше дней")
    planned = build_paper_plan(left, plan_days)
    for p in planned:
        p.day_number += first_day - 1
        p.page_from += page_from - 1
        p.page_to += page_from - 1
        p.title = f"стр. {p.page_from}–{p.page_to}"
        p.pos_from = round((p.page_from - 1) / total_pages, 6)
        p.pos_to = round(p.page_to / total_pages, 6)
    planned[-1].pos_to = 1.0
    return planned


# --------------------------------------------------------------------------- прочитано не всё


@dataclass
class TextCut:
    head: str  # прочитанная часть
    tail: str  # остаток
    share: float  # доля прочитанного по знакам


def cut_text(text: str, share: float) -> TextCut:
    """Разрезать текст отрезка около доли share — по границе абзаца, а если абзац один — по границе предложения."""
    lines = [x for x in text.split("\n") if x.strip()]
    joiner = "\n"
    if len(lines) < 2:
        lines = [x for x in _SENTENCE_END.split(text.strip()) if x.strip()]
        joiner = " "
    if len(lines) < 2:
        raise PlanError("Отрезок слишком короткий, чтобы делить его на части")
    sizes = [len(x) + 1 for x in lines]
    total = sum(sizes)
    target = share * total
    acc, best, best_d = 0, 1, None
    for i in range(1, len(lines)):
        acc += sizes[i - 1]
        d = abs(acc - target)
        if best_d is None or d < best_d:
            best, best_d = i, d
    head, tail = joiner.join(lines[:best]), joiner.join(lines[best:])
    return TextCut(head, tail, sum(sizes[:best]) / total)
