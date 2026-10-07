"""Разбор fb2 и fb2.zip: главы по <section>, названия по <title>."""

from __future__ import annotations

import re
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from books.htmltext import clean_inline
from books.safezip import SafeZip
from books.types import SUBHEADING_MARK, BookParseError, ParsedBook, ParsedChapter

MAX_FB2 = 60 * 1024 * 1024
_BAD_AMP = re.compile(rb"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][a-zA-Z0-9]*;)")
_DOCTYPE = re.compile(rb"<!DOCTYPE[^>]*>", re.I)
SKIP_BODY_NAMES = {"notes", "comments", "footnotes"}


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _attr(el: Element, name: str) -> str:
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return ""


def _parse_xml(data: bytes) -> Element:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return SafeET.fromstring(data)
    except DefusedXmlException as e:
        raise BookParseError("broken", "forbidden xml construct") from e
    except Exception:
        pass
    # частые поломки fb2: голые & и DOCTYPE
    fixed = _DOCTYPE.sub(b"", _BAD_AMP.sub(b"&amp;", data))
    try:
        return SafeET.fromstring(fixed)
    except DefusedXmlException as e:
        raise BookParseError("broken", "forbidden xml construct") from e
    except Exception as e:
        raise BookParseError("broken", f"xml: {e}") from e


def _inline_text(el: Element) -> str:
    """Текст абзаца без сносок и картинок."""
    parts: list[str] = []

    def walk(e: Element) -> None:
        if e.text:
            parts.append(e.text)
        for c in e:
            t = _local(c.tag)
            skip = t in ("image", "binary", "sup") or (t == "a" and _attr(c, "type") == "note")
            if not skip:
                walk(c)
            if c.tail:
                parts.append(c.tail)

    walk(el)
    return clean_inline("".join(parts))


def _title_text(el: Element | None) -> str:
    if el is None:
        return ""
    lines = [_inline_text(p) for p in el if _local(p.tag) == "p"]
    lines = [x for x in lines if x]
    if not lines:
        return clean_inline("".join(el.itertext()))
    return ". ".join(lines)


class _State:
    def __init__(self) -> None:
        self.chapters: list[ParsedChapter] = []
        self.images = 0


def _content_paragraphs(el: Element, st: _State) -> list[str]:
    """Абзацы из блочного элемента (p, poem, cite, epigraph, subtitle...)."""
    tag = _local(el.tag)
    if tag in ("p", "v", "text-author"):
        t = _inline_text(el)
        return [t] if t else []
    if tag == "subtitle":
        t = _inline_text(el)
        return [f"{SUBHEADING_MARK}{t}"] if t else []
    if tag == "image":
        st.images += 1
        return []
    if tag in ("table", "empty-line", "binary"):
        return []
    if tag == "title":  # заголовок внутри стихотворения и т.п.
        t = _title_text(el)
        return [f"{SUBHEADING_MARK}{t}"] if t else []
    out: list[str] = []
    for c in el:
        out.extend(_content_paragraphs(c, st))
    return out


def _walk_section(sec: Element, st: _State, inherited_title: str) -> None:
    title = ""
    direct: list[str] = []
    subsections: list[Element] = []
    pending_before_sub: list[str] = []
    for c in sec:
        tag = _local(c.tag)
        if tag == "title":
            title = _title_text(c)
        elif tag == "section":
            subsections.append(c)
        elif tag == "image":
            st.images += 1
        else:
            paras = _content_paragraphs(c, st)
            if subsections:
                pending_before_sub.extend(paras)  # текст после подглав — редкость, добавим в конец
            else:
                direct.extend(paras)

    full_title = ". ".join(x for x in (inherited_title, title) if x)
    has_direct_text = any(not p.startswith(SUBHEADING_MARK) for p in direct)
    if not subsections:
        if direct or full_title:
            st.chapters.append(ParsedChapter(full_title[:300], direct))
        return
    if has_direct_text:
        st.chapters.append(ParsedChapter(full_title[:300], direct))
        child_inherit = ""
    else:
        child_inherit = full_title  # «Часть первая» → «Часть первая. Глава 1»
    for i, s in enumerate(subsections):
        _walk_section(s, st, child_inherit if i == 0 else "")
    if pending_before_sub and st.chapters:
        st.chapters[-1].paragraphs.extend(pending_before_sub)


def parse_fb2_bytes(data: bytes) -> ParsedBook:
    root = _parse_xml(data)
    if _local(root.tag) != "FictionBook":
        raise BookParseError("broken", "not FictionBook")

    title, authors = "", []
    for d in root:
        if _local(d.tag) != "description":
            continue
        for ti in d:
            if _local(ti.tag) != "title-info":
                continue
            for x in ti:
                t = _local(x.tag)
                if t == "book-title":
                    title = clean_inline("".join(x.itertext()))
                elif t == "author":
                    parts = {_local(n.tag): clean_inline("".join(n.itertext())) for n in x}
                    name = " ".join(
                        p for p in (parts.get("first-name"), parts.get("middle-name"), parts.get("last-name")) if p
                    ) or parts.get("nickname", "")
                    if name:
                        authors.append(name)

    st = _State()
    bodies = [b for b in root if _local(b.tag) == "body"]
    if not bodies:
        raise BookParseError("broken", "no body")
    for body in bodies:
        if _attr(body, "name").lower() in SKIP_BODY_NAMES:
            continue
        preface: list[str] = []
        has_sections = False
        for c in body:
            tag = _local(c.tag)
            if tag == "section":
                if preface:
                    st.chapters.append(ParsedChapter("", preface))
                    preface = []
                has_sections = True
                _walk_section(c, st, "")
            elif tag == "title":
                continue  # название книги в начале body
            elif tag == "image":
                st.images += 1
            else:
                preface.extend(_content_paragraphs(c, st))
        if preface:
            if has_sections and st.chapters:
                st.chapters[-1].paragraphs.extend(preface)
            else:
                st.chapters.append(ParsedChapter("", preface))

    chapters = [c for c in st.chapters if c.paragraphs or c.title]
    has_chapters = sum(1 for c in chapters if c.paragraphs) >= 2
    if not has_chapters:
        chapters = [ParsedChapter("", [p for c in chapters for p in c.paragraphs])]
    return ParsedBook(
        source="fb2",
        title=title,
        author=", ".join(dict.fromkeys(authors)),
        chapters=chapters,
        has_chapters=has_chapters,
        images=st.images,
    )


def parse_fb2_zip(data: bytes) -> ParsedBook:
    z = SafeZip(data)
    names = [n for n in z.names() if n.lower().endswith(".fb2")]
    if not names:
        raise BookParseError("broken", "no fb2 inside zip")
    return parse_fb2_bytes(z.read(names[0]))
