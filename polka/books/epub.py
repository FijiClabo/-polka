"""Разбор epub: порядок по spine, названия по оглавлению (nav или ncx)."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from urllib.parse import unquote
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from books.htmltext import Block, DocText, clean_inline, decode_markup, extract_text
from books.safezip import SafeZip
from books.types import SUBHEADING_MARK, BookParseError, ParsedBook, ParsedChapter

# Алгоритмы «обфускации шрифтов» — это не DRM, текст читается
FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":")[-1]


def _xml(data: bytes, what: str) -> Element:
    try:
        return SafeET.fromstring(data)
    except DefusedXmlException as e:
        raise BookParseError("broken", f"{what}: forbidden xml construct") from e
    except Exception as e:  # ParseError и прочее
        raise BookParseError("broken", f"{what}: {e}") from e


def _iter(el: Element, name: str):
    for x in el.iter():
        if _local(x.tag) == name:
            yield x


def _find(el: Element, name: str) -> Element | None:
    return next(_iter(el, name), None)


def _text(el: Element | None) -> str:
    if el is None:
        return ""
    return clean_inline("".join(el.itertext()))


def _join(base_dir: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base_dir, href)) if href else ""


@dataclass
class TocEntry:
    title: str
    path: str
    fragment: str
    depth: int


# --------------------------------------------------------------------------- DRM


def _check_drm(z: SafeZip) -> None:
    if z.has("META-INF/rights.xml"):
        raise BookParseError("drm", "rights.xml")
    if not z.has("META-INF/encryption.xml"):
        return
    root = _xml(z.read("META-INF/encryption.xml"), "encryption.xml")
    for enc in _iter(root, "EncryptedData"):
        method = _find(enc, "EncryptionMethod")
        alg = method.get("Algorithm", "") if method is not None else ""
        ref = _find(enc, "CipherReference")
        uri = (ref.get("URI", "") if ref is not None else "").lower()
        if alg in FONT_OBFUSCATION:
            continue
        if uri.endswith((".ttf", ".otf", ".woff", ".woff2")):
            continue
        raise BookParseError("drm", f"encrypted {uri or alg}")


# --------------------------------------------------------------------------- TOC


class _NavParser:
    """Достаём ссылки из <nav epub:type="toc"> устойчиво к битой разметке."""

    def __init__(self, html: str):
        from html.parser import HTMLParser

        entries: list[tuple[str, str, int]] = []
        state = {"in_toc": 0, "nav_depth": 0, "ol": 0, "href": None, "buf": [], "any_nav": False}

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                t = tag.lower().split(":")[-1]
                if t == "nav":
                    state["nav_depth"] += 1
                    etype = (a.get("epub:type") or a.get("type") or a.get("role") or "").lower()
                    if "toc" in etype and not state["in_toc"]:
                        state["in_toc"] = state["nav_depth"]
                elif state["in_toc"]:
                    if t == "ol":
                        state["ol"] += 1
                    elif t == "a":
                        state["href"] = a.get("href")
                        state["buf"] = []

            def handle_endtag(self, tag):
                t = tag.lower().split(":")[-1]
                if t == "nav":
                    if state["in_toc"] == state["nav_depth"]:
                        state["in_toc"] = 0
                    state["nav_depth"] -= 1
                elif state["in_toc"]:
                    if t == "ol":
                        state["ol"] -= 1
                    elif t == "a" and state["href"] is not None:
                        entries.append((state["href"], clean_inline("".join(state["buf"])), max(state["ol"], 1)))
                        state["href"] = None

            def handle_data(self, data):
                if state["href"] is not None:
                    state["buf"].append(data)

        p = P(convert_charrefs=True)
        try:
            p.feed(html)
            p.close()
        except Exception:
            pass
        self.entries = entries


def _toc_from_nav(z: SafeZip, nav_path: str) -> list[TocEntry]:
    html = decode_markup(z.read(nav_path))
    base = posixpath.dirname(nav_path)
    out = []
    for href, title, depth in _NavParser(html).entries:
        if not href or href.startswith(("http:", "https:", "mailto:")):
            continue
        frag = unquote(href.split("#", 1)[1]) if "#" in href else ""
        out.append(TocEntry(title, _join(base, href), frag, depth))
    return out


def _toc_from_ncx(z: SafeZip, ncx_path: str) -> list[TocEntry]:
    root = _xml(z.read(ncx_path), "ncx")
    base = posixpath.dirname(ncx_path)
    out: list[TocEntry] = []
    nav_map = _find(root, "navMap")
    if nav_map is None:
        return out

    def walk(el: Element, depth: int) -> None:
        for child in el:
            if _local(child.tag) != "navPoint":
                continue
            label = _text(_find(child, "text"))
            content = next((c for c in child if _local(c.tag) == "content"), None)
            src = content.get("src", "") if content is not None else ""
            if src:
                frag = unquote(src.split("#", 1)[1]) if "#" in src else ""
                out.append(TocEntry(label, _join(base, src), frag, depth))
            walk(child, depth + 1)

    walk(nav_map, 1)
    return out


# --------------------------------------------------------------------------- main


def parse_epub(data: bytes) -> ParsedBook:
    z = SafeZip(data)
    _check_drm(z)

    if not z.has("META-INF/container.xml"):
        raise BookParseError("broken", "no container.xml")
    container = _xml(z.read("META-INF/container.xml"), "container.xml")
    rootfile = _find(container, "rootfile")
    opf_path = rootfile.get("full-path", "") if rootfile is not None else ""
    if not opf_path or not z.has(opf_path):
        cands = [n for n in z.names() if n.lower().endswith(".opf")]
        if not cands:
            raise BookParseError("broken", "no opf")
        opf_path = cands[0]
    opf = _xml(z.read(opf_path), "opf")
    opf_dir = posixpath.dirname(opf_path)

    # --- метаданные
    metadata = _find(opf, "metadata")
    title, authors = "", []
    if metadata is not None:
        title = _text(_find(metadata, "title"))
        for cr in _iter(metadata, "creator"):
            role = ""
            for k, v in cr.attrib.items():
                if _local(k) == "role":
                    role = v
            name = _text(cr)
            if name and role in ("", "aut"):
                authors.append(name)

    # --- manifest и spine
    manifest: dict[str, tuple[str, str, str]] = {}
    nav_path = ""
    for item in _iter(opf, "item"):
        iid, href = item.get("id", ""), item.get("href", "")
        if not href:
            continue
        path = _join(opf_dir, href)
        mtype = item.get("media-type", "")
        props = item.get("properties", "")
        manifest[iid] = (path, mtype, props)
        if "nav" in props.split():
            nav_path = path

    spine_el = _find(opf, "spine")
    spine: list[str] = []
    ncx_path = ""
    if spine_el is not None:
        toc_id = spine_el.get("toc", "")
        if toc_id in manifest:
            ncx_path = manifest[toc_id][0]
        for ref in spine_el:
            if _local(ref.tag) != "itemref":
                continue
            if ref.get("linear", "yes").lower() == "no":
                continue
            idref = ref.get("idref", "")
            if idref in manifest:
                path, mtype, _ = manifest[idref]
                if "html" in mtype or path.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                    spine.append(path)
    if not ncx_path:
        ncx_path = next((p for p, mt, _ in manifest.values() if mt == "application/x-dtbncx+xml"), "")
    if not spine:
        raise BookParseError("broken", "empty spine")

    # --- тексты документов
    docs: list[DocText] = []
    images = 0
    for path in spine:
        if not z.has(path):
            docs.append(DocText())
            continue
        raw = z.read(path)
        if b"\x00" in raw[:2000] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            # бинарный мусор вместо xhtml — признак шифрования
            raise BookParseError("drm", f"binary content {path}")
        doc = extract_text(decode_markup(raw))
        images += doc.images
        docs.append(doc)

    # --- оглавление
    toc: list[TocEntry] = []
    try:
        if nav_path and z.has(nav_path):
            toc = _toc_from_nav(z, nav_path)
        if len(toc) < 2 and ncx_path and z.has(ncx_path):
            toc = _toc_from_ncx(z, ncx_path)
    except BookParseError:
        toc = []

    chapters, has_chapters = _build_chapters(spine, docs, toc)
    book = ParsedBook(
        source="epub",
        title=title,
        author=", ".join(dict.fromkeys(authors)),
        chapters=chapters,
        has_chapters=has_chapters,
        images=images,
    )
    return book


# --------------------------------------------------------------------------- главы


def _build_chapters(spine: list[str], docs: list[DocText], toc: list[TocEntry]) -> tuple[list[ParsedChapter], bool]:
    # Единый поток блоков и смещения документов в нём
    stream: list[Block] = []
    doc_offset: dict[str, int] = {}
    anchor_pos: dict[tuple[str, str], int] = {}
    for path, doc in zip(spine, docs, strict=True):
        doc_offset.setdefault(path, len(stream))
        for aid, idx in doc.anchors.items():
            anchor_pos[(path, aid)] = len(stream) + idx
        stream.extend(doc.blocks)
    if not stream:
        return [], False

    # 1) по оглавлению
    starts: dict[int, str] = {}
    for e in toc:
        if e.path not in doc_offset:
            continue
        pos = anchor_pos.get((e.path, e.fragment), doc_offset[e.path]) if e.fragment else doc_offset[e.path]
        if pos >= len(stream):
            continue
        # если на одну позицию указывают несколько пунктов (часть + глава) — берём самый глубокий
        starts[pos] = e.title or starts.get(pos, "")
    if len(starts) >= 2:
        return _split(stream, starts), True

    # 2) по заголовкам
    heading_levels = [b.level for b in stream if b.kind == "h"]
    if heading_levels:
        for lvl in sorted(set(heading_levels)):
            idxs = [i for i, b in enumerate(stream) if b.kind == "h" and b.level == lvl]
            if len(idxs) >= 2:
                hstarts = {}
                for i in idxs:
                    hstarts[i] = stream[i].text
                return _split(stream, hstarts), True

    # 3) деления нет — одна «глава», план по абзацам («День 1», «День 2»)
    paragraphs = [_render(b) for b in stream]
    return [ParsedChapter("", [p for p in paragraphs if p])], False


def _render(b: Block) -> str:
    return f"{SUBHEADING_MARK}{b.text}" if b.kind == "h" else b.text


def _split(stream: list[Block], starts: dict[int, str]) -> list[ParsedChapter]:
    positions = sorted(starts)
    chapters: list[ParsedChapter] = []
    if positions[0] > 0:
        chapters.append(_make_chapter("", stream[: positions[0]]))
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(stream)
        chapters.append(_make_chapter(starts[pos], stream[pos:end]))
    return [c for c in chapters if c.paragraphs or c.title]


def _make_chapter(title: str, blocks: list[Block]) -> ParsedChapter:
    title = clean_inline(title)
    blocks = list(blocks)
    # заголовки в начале главы: первый становится названием, если в оглавлении пусто
    lead: list[str] = []
    while blocks and blocks[0].kind == "h" and len(lead) < 3:
        lead.append(blocks.pop(0).text)
    if not title and lead:
        title = ". ".join(lead[:2])
    elif lead:
        norm_title = title.lower()
        extra = [h for h in lead if h.lower() not in norm_title and norm_title not in h.lower()]
        blocks = [Block("h", h, 2) for h in extra] + blocks
    paragraphs = [_render(b) for b in blocks if b.text]
    return ParsedChapter(title[:300], paragraphs)
