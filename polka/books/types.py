from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

CHARS_PER_PAGE = 1800  # условная страница: 1800 знаков с пробелами
WORDS_PER_MINUTE = 200
PAPER_MINUTES_PER_PAGE = 1.3  # бумажная страница ≈ 260 слов
MIN_PAGES = 20
SUBHEADING_MARK = "## "  # подзаголовок внутри текста главы

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Человеко-понятные причины неудачного разбора (показываются пользователю)
PARSE_ERRORS: dict[str, str] = {
    "unsupported": "Пока умею только epub и fb2.",
    "too_big": "Файл слишком большой.",
    "broken": "Файл не открывается — похоже, он повреждён.",
    "drm": "Файл защищён от копирования, текст из него не достать.",
    "too_short": "В файле слишком мало текста — меньше 20 страниц.",
    "no_text": "В файле почти нет текста — похоже, книга состоит из картинок (скан).",
    "zip_bomb": "Файл распаковывается в слишком большой объём.",
}


class BookParseError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def human(self) -> str:
        return PARSE_ERRORS.get(self.code, "Не получилось разобрать файл.")


@dataclass
class ParsedChapter:
    title: str
    paragraphs: list[str]

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.paragraphs) + max(len(self.paragraphs) - 1, 0)

    @property
    def word_count(self) -> int:
        return sum(count_words(p) for p in self.paragraphs)

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)


@dataclass
class ParsedBook:
    source: str  # epub | fb2
    title: str
    author: str
    chapters: list[ParsedChapter]
    has_chapters: bool = True
    images: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(c.char_count for c in self.chapters)

    @property
    def total_words(self) -> int:
        return sum(c.word_count for c in self.chapters)

    @property
    def total_pages(self) -> int:
        return pages_for_chars(self.total_chars)

    @property
    def reading_minutes(self) -> int:
        return math.ceil(self.total_words / WORDS_PER_MINUTE)


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def pages_for_chars(chars: int) -> int:
    return max(1, math.ceil(chars / CHARS_PER_PAGE)) if chars > 0 else 0


def visible_text(paragraph: str) -> str:
    return paragraph[len(SUBHEADING_MARK):] if paragraph.startswith(SUBHEADING_MARK) else paragraph
