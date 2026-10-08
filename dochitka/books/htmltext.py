"""Извлечение текста из (X)HTML глав epub.

Оставляем только абзацы и заголовки. Картинки, сноски, таблицы, стили, скрипты отбрасываем.
Используется стандартный HTMLParser: он не раскрывает внешние сущности и терпим к битой разметке.
Содержимое файла никогда не отдаётся в браузер как HTML — только как обычный текст.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

BLOCK_TAGS = {
    "p", "div", "li", "blockquote", "pre", "dd", "dt", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "body", "main", "center", "dl", "ul", "ol", "hr",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {
    "script", "style", "head", "title", "table", "svg", "math", "sup", "aside", "nav",
    "figure", "noscript", "iframe", "object", "video", "audio", "template", "button", "form",
}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "image", "col", "wbr", "source", "area", "base"}
SKIP_EPUB_TYPES = {"footnote", "endnote", "rearnote", "noteref", "pagebreak", "footnotes", "endnotes", "toc"}
SKIP_CLASS_RE = re.compile(r"(?:^|\s)(footnote|endnote|note-?ref|pagenum|page-?number)(?:\s|$)", re.I)

_WS = re.compile(r"[ \t\r\n\f\v   ]+")
_INVISIBLE = re.compile(r"[­​‌‍⁠﻿]")


def clean_inline(text: str) -> str:
    text = _INVISIBLE.sub("", text)
    return _WS.sub(" ", text).strip()


@dataclass
class Block:
    kind: str  # p | h
    text: str
    level: int = 0


@dataclass
class DocText:
    blocks: list[Block] = field(default_factory=list)
    anchors: dict[str, int] = field(default_factory=dict)  # id → индекс блока
    images: int = 0


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = DocText()
        self.stack: list[tuple[str, bool]] = []  # (tag, skipping-started-here)
        self.skip_depth = 0
        self.buf: list[str] = []
        self.heading_level = 0
        self.in_pre = False

    # ---------------------------------------------------------------- helpers
    def _flush(self) -> None:
        if not self.buf:
            return
        raw = "".join(self.buf)
        self.buf = []
        lines = [clean_inline(x) for x in raw.split("\n")] if self.in_pre else [clean_inline(raw)]
        for text in lines:
            if not text:
                continue
            if self.heading_level:
                self.doc.blocks.append(Block("h", text, self.heading_level))
            else:
                self.doc.blocks.append(Block("p", text))

    @staticmethod
    def _should_skip(tag: str, attrs: dict[str, str | None]) -> bool:
        if tag in SKIP_TAGS:
            return True
        etype = (attrs.get("epub:type") or attrs.get("type") or "").lower()
        if etype and any(t in SKIP_EPUB_TYPES for t in etype.split()):
            return True
        role = (attrs.get("role") or "").lower()
        if role in ("doc-footnote", "doc-endnote", "doc-noteref", "doc-pagebreak", "doc-endnotes"):
            return True
        cls = attrs.get("class") or ""
        if cls and SKIP_CLASS_RE.search(cls):
            return True
        if tag == "a" and etype == "noteref":
            return True
        style = (attrs.get("style") or "").replace(" ", "").lower()
        return "display:none" in style

    # ---------------------------------------------------------------- parser API
    def handle_starttag(self, tag: str, attrs_list) -> None:
        tag = tag.lower().split(":")[-1]
        attrs = {k.lower(): v for k, v in attrs_list}
        if tag in ("img", "image") or (tag == "svg"):
            self.doc.images += 1
        el_id = (attrs.get("id") or attrs.get("name")) if tag == "a" else attrs.get("id")
        if el_id and self.skip_depth == 0:
            self.doc.anchors.setdefault(el_id, len(self.doc.blocks))
        if tag in VOID_TAGS:
            if tag == "br" and self.skip_depth == 0:
                if self.in_pre:
                    self.buf.append("\n")
                else:
                    self.buf.append(" ")
            if tag == "hr" and self.skip_depth == 0:
                self._flush()
            return
        skipping = self.skip_depth == 0 and self._should_skip(tag, attrs)
        if skipping or self.skip_depth:
            self.skip_depth += 1
            self.stack.append((tag, True))
            return
        if tag in BLOCK_TAGS:
            self._flush()
            if tag in HEADING_TAGS:
                self.heading_level = int(tag[1])
            if tag == "pre":
                self.in_pre = True
        self.stack.append((tag, False))

    def handle_startendtag(self, tag, attrs) -> None:
        t = tag.lower().split(":")[-1]
        if t in VOID_TAGS:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower().split(":")[-1]
        if tag in VOID_TAGS:
            return
        # закрываем до ближайшего совпадающего тега (битая разметка)
        if not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            t, counted = self.stack.pop()
            if counted:
                self.skip_depth -= 1
            elif t in BLOCK_TAGS:
                self._flush()
                if t in HEADING_TAGS:
                    self.heading_level = 0
                if t == "pre":
                    self.in_pre = False
            if t == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        self.buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def extract_text(html: str) -> DocText:
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # HTMLParser почти не падает, но файл недоверенный
        p._flush()
    return p.doc


def decode_markup(data: bytes) -> str:
    """Декодирование с учётом BOM и объявления кодировки."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    head = data[:400].decode("ascii", errors="ignore")
    m = re.search(r"""encoding=["']([\w\-]+)["']""", head) or re.search(r"""charset=["']?([\w\-]+)""", head)
    enc = (m.group(1) if m else "utf-8").lower()
    try:
        text = data.decode(enc, errors="strict")
        return text
    except (LookupError, UnicodeDecodeError):
        pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")
