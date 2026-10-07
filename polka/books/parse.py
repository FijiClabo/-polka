"""Точка входа разбора книги: формат → главы → очистка → проверки."""

from __future__ import annotations

import hashlib
import re

from books.epub import parse_epub
from books.fb2 import parse_fb2_bytes, parse_fb2_zip
from books.htmltext import clean_inline
from books.types import (
    CHARS_PER_PAGE,
    MIN_PAGES,
    SUBHEADING_MARK,
    BookParseError,
    ParsedBook,
    ParsedChapter,
)

SUPPORTED_EXT = (".epub", ".fb2", ".fb2.zip")
OTHER_BOOK_EXT = (".pdf", ".mobi", ".azw", ".azw3", ".txt", ".doc", ".docx", ".rtf", ".djvu", ".djv", ".cbz", ".odt")

# Служебные части, которые выбрасываем всегда
_SERVICE_ALWAYS = re.compile(
    r"^(обложка|cover|титул|титульный лист|title page|оглавление|содержание|contents|table of contents|toc|"
    r"аннотация|annotation|от издательства|выходные данные|информация об издании|copyright|"
    r"реклама|оформление|информация от издательства|благодарности издательства)$",
    re.I,
)
# Служебные части, которые выбрасываем, только если они в конце книги
_SERVICE_TAIL = re.compile(
    r"^(об авторе|об авторах|about the author|примечания|notes|сноски|комментарии|"
    r"другие книги.*|читайте также.*|also by.*|книги автора.*|новинки.*|литрес.*|"
    r"конец ознакомительного фрагмента.*|приложение\.? список иллюстраций)$",
    re.I,
)
_COPYRIGHT_HINT = re.compile(r"(ISBN|УДК|ББК|©|All rights reserved|Все права защищены|Охраняется законом)", re.I)
_PREVIEW_TAIL = re.compile(r"(ознакомительного фрагмента|купить полную версию|litres\.ru|литрес)", re.I)
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def detect_format(filename: str) -> str | None:
    name = (filename or "").lower().strip()
    if name.endswith(".fb2.zip") or name.endswith(".fbz"):
        return "fb2.zip"
    if name.endswith(".epub"):
        return "epub"
    if name.endswith(".fb2"):
        return "fb2"
    if name.endswith(".zip"):
        return "zip"
    return None


def is_other_book_format(filename: str) -> bool:
    return (filename or "").lower().endswith(OTHER_BOOK_EXT)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm_title(t: str) -> str:
    t = clean_inline(t).lower().strip(" .:—-")
    return re.sub(r"\s+", " ", t)


def _clean_chapters(book: ParsedBook) -> list[ParsedChapter]:
    chapters = [c for c in book.chapters]
    n = len(chapters)
    total = sum(c.char_count for c in chapters) or 1
    out: list[ParsedChapter] = []
    passed = 0
    for i, ch in enumerate(chapters):
        size = ch.char_count
        tail_zone = passed / total > 0.8 or i >= n - 3
        passed += size
        t = _norm_title(ch.title)
        if t and _SERVICE_ALWAYS.match(t):
            continue
        if t and tail_zone and _SERVICE_TAIL.match(t):
            continue
        text = ch.text
        # страница с выходными данными
        if size < 3000 and _COPYRIGHT_HINT.search(text) and (i < 3 or tail_zone):
            continue
        # титульный лист: крошечный кусок без заголовка в самом начале
        if i < 3 and size < 200 and not t:
            continue
        out.append(ParsedChapter(ch.title, list(ch.paragraphs)))

    # хвост ознакомительного фрагмента LitRes
    if out:
        last = out[-1]
        while last.paragraphs and _PREVIEW_TAIL.search(last.paragraphs[-1]) and len(last.paragraphs[-1]) < 300:
            last.paragraphs.pop()

    # слишком короткие главы (например, «Часть первая» без текста) присоединяем к следующей
    merged: list[ParsedChapter] = []
    carry_title = ""
    carry_paras: list[str] = []
    for ch in out:
        body_chars = sum(len(p) for p in ch.paragraphs if not p.startswith(SUBHEADING_MARK))
        if body_chars < 300 and ch is not out[-1]:
            if ch.title:
                carry_title = f"{carry_title}. {ch.title}" if carry_title else ch.title
            carry_paras.extend(ch.paragraphs)
            continue
        title = ch.title
        if carry_title:
            title = f"{carry_title}. {title}" if title else carry_title
        merged.append(ParsedChapter(title[:300], carry_paras + ch.paragraphs))
        carry_title, carry_paras = "", []
    if carry_paras or carry_title:
        if merged:
            merged[-1].paragraphs.extend(carry_paras)
        else:
            merged.append(ParsedChapter(carry_title, carry_paras))
    return [c for c in merged if c.paragraphs]


def _letters_ratio(book: ParsedBook) -> float:
    text = "".join("".join(c.paragraphs) for c in book.chapters)
    nonspace = sum(1 for ch in text if not ch.isspace())
    if not nonspace:
        return 0.0
    return len(_LETTER.findall(text)) / nonspace


def parse_book(filename: str, data: bytes) -> ParsedBook:
    """Разобрать файл. При неудаче — BookParseError с понятной причиной (.human)."""
    fmt = detect_format(filename)
    if fmt is None:
        raise BookParseError("unsupported", filename)
    if fmt == "epub":
        book = parse_epub(data)
    elif fmt == "fb2":
        book = parse_fb2_bytes(data)
    elif fmt == "fb2.zip":
        book = parse_fb2_zip(data)
    else:  # просто .zip: внутри может быть fb2 или это epub с неправильным расширением
        try:
            book = parse_fb2_zip(data)
        except BookParseError:
            book = parse_epub(data)

    raw_chars = book.total_chars
    book.chapters = _clean_chapters(book)
    if book.has_chapters and len(book.chapters) < 2:
        book.has_chapters = False
    if not book.has_chapters:
        book.chapters = [ParsedChapter("", [p for c in book.chapters for p in c.paragraphs])]

    chars = book.total_chars
    if chars < 200 and (book.images >= 3 or raw_chars < 200):
        raise BookParseError("no_text", f"images={book.images}")
    if chars > 2000 and _letters_ratio(book) < 0.45:
        raise BookParseError("no_text", "few letters")
    if chars < MIN_PAGES * CHARS_PER_PAGE:
        if book.images >= 10 and chars < 5 * CHARS_PER_PAGE:
            raise BookParseError("no_text", f"images={book.images}")
        raise BookParseError("too_short", f"chars={chars}")

    book.title = clean_inline(book.title)[:300] or "Без названия"
    book.author = clean_inline(book.author)[:300]
    return book
