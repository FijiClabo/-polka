"""Синтетические книги для тестов и ручной проверки (раздел 9.8 ТЗ).

Настоящие файлы в репозиторий не коммитим — этот генератор создаёт файлы с теми же
особенностями: аккуратный epub, epub без оглавления, одна гигантская глава, fb2, fb2.zip,
защищённый файл, книга из картинок, очень короткая и очень длинная книга.
Запуск: python -m scripts.make_samples ./samples
"""

from __future__ import annotations

import io
import random
import zipfile
from xml.sax.saxutils import escape

WORDS = (
    "утро город дорога окно письмо дом старый молодой человек время свет тихо вдруг снова долго "
    "вечер ветер река мост сад дверь лестница разговор вопрос ответ память мысль сердце голос "
    "рука взгляд улица площадь поезд станция книга страница история надежда страх радость "
    "неожиданно медленно быстро осторожно спокойно громко внимательно задумчиво наконец потом "
    "сказал подумал увидел вспомнил ответил спросил понял решил пошёл вернулся остановился"
).split()
NAMES = ["Анна", "Пётр", "Мария", "Илья", "Вера", "Лев", "Софья", "Глеб"]


def paragraphs(n: int, seed: int = 1, words: tuple[int, int] = (40, 90)) -> list[str]:
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        sentences = []
        total = rnd.randint(*words)
        while total > 0:
            k = min(total, rnd.randint(6, 16))
            ws = [rnd.choice(WORDS) for _ in range(k)]
            if rnd.random() < 0.3:
                ws[rnd.randrange(k)] = rnd.choice(NAMES)
            s = " ".join(ws)
            sentences.append(s[0].upper() + s[1:] + rnd.choice([".", ".", ".", "!", "?", "…"]))
            total -= k
        out.append(" ".join(sentences))
    return out


def chapters_text(n_chapters: int, paras_per_chapter: int, seed: int = 1) -> list[tuple[str, list[str]]]:
    return [(f"Глава {i + 1}", paragraphs(paras_per_chapter, seed + i)) for i in range(n_chapters)]


# --------------------------------------------------------------------------- epub


def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        f"<head><title>{escape(title)}</title><style>p{{text-indent:1em}}</style></head><body>{body}</body></html>"
    )


def make_epub(
    title: str = "Тестовая книга",
    author: str = "Иван Тестов",
    chapters: list[tuple[str, list[str]]] | None = None,
    *,
    toc: str = "nav",  # nav | ncx | none
    headings: bool = True,
    single_file: bool = False,
    front_matter: bool = True,
    back_matter: bool = True,
    footnotes: bool = True,
    encrypted: bool = False,
    images_only: int = 0,
    font_obfuscation: bool = False,
) -> bytes:
    chapters = chapters if chapters is not None else chapters_text(12, 60)
    files: list[tuple[str, str, str]] = []  # (id, href, content)
    toc_entries: list[tuple[str, str]] = []

    if front_matter:
        files.append(("cover", "cover.xhtml", _xhtml("Обложка", '<div><img src="cover.jpg" alt="cover"/></div>')))
        files.append(
            ("info", "info.xhtml", _xhtml("Info", "<p>ISBN 978-5-00000-000-0</p><p>© Издательство, 2026</p>"))
        )
        toc_entries.append(("Обложка", "cover.xhtml"))

    def chapter_body(ci: int, ch_title: str, paras: list[str]) -> str:
        parts = []
        if headings:
            parts.append(f'<h2 id="ch{ci}">{escape(ch_title)}</h2>')
        else:
            parts.append(f'<a id="ch{ci}"></a>')
        for pi, p in enumerate(paras):
            note = ""
            if footnotes and pi == 2:
                note = f'<sup><a epub:type="noteref" href="#n{ci}">{ci}</a></sup>'
            parts.append(f"<p>{escape(p)}{note}</p>")
        if footnotes:
            parts.append(f'<aside epub:type="footnote" id="n{ci}"><p>Сноска {ci}: служебный текст.</p></aside>')
        return "".join(parts)

    if images_only:
        body = "".join(f'<p><img src="p{i}.jpg" alt=""/></p>' for i in range(images_only))
        files.append(("c1", "scan.xhtml", _xhtml("Скан", body)))
        toc_entries.append(("Скан", "scan.xhtml"))
    elif single_file:
        body = "".join(chapter_body(i, t, ps) for i, (t, ps) in enumerate(chapters))
        files.append(("c1", "text.xhtml", _xhtml(title, body)))
        for i, (t, _) in enumerate(chapters):
            toc_entries.append((t, f"text.xhtml#ch{i}"))
    else:
        for i, (t, ps) in enumerate(chapters):
            href = f"ch{i + 1:03d}.xhtml"
            files.append((f"c{i + 1}", href, _xhtml(t, chapter_body(i, t, ps))))
            toc_entries.append((t, href))

    if back_matter:
        files.append(
            ("about", "about.xhtml", _xhtml("Об авторе", "<h2>Об авторе</h2><p>" + "Автор родился. " * 30 + "</p>"))
        )
        toc_entries.append(("Об авторе", "about.xhtml"))
        files.append(
            ("ads", "ads.xhtml", _xhtml("Другие книги", "<h2>Другие книги серии</h2><p>Купите ещё.</p>"))
        )
        toc_entries.append(("Другие книги серии", "ads.xhtml"))

    manifest = [f'<item id="{i}" href="{h}" media-type="application/xhtml+xml"/>' for i, h, _ in files]
    spine = [f'<itemref idref="{i}"/>' for i, _, _ in files]
    nav_xhtml = ""
    ncx = ""
    spine_attr = ""
    if toc == "nav":
        lis = "".join(f'<li><a href="{h}">{escape(t)}</a></li>' for t, h in toc_entries)
        nav_xhtml = _xhtml("Содержание", f'<nav epub:type="toc"><h1>Содержание</h1><ol>{lis}</ol></nav>')
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    if toc == "ncx":
        points = "".join(
            f'<navPoint id="np{k}" playOrder="{k + 1}"><navLabel><text>{escape(t)}</text></navLabel>'
            f'<content src="{h}"/></navPoint>'
            for k, (t, h) in enumerate(toc_entries)
        )
        ncx = (
            '<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f"<head></head><docTitle><text>{escape(title)}</text></docTitle><navMap>{points}</navMap></ncx>"
        )
        manifest.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        spine_attr = ' toc="ncx"'

    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="uid">test</dc:identifier><dc:title>{escape(title)}</dc:title>'
        f"<dc:creator>{escape(author)}</dc:creator><dc:language>ru</dc:language></metadata>"
        f"<manifest>{''.join(manifest)}</manifest><spine{spine_attr}>{''.join(spine)}</spine></package>"
    )
    container = (
        '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        if nav_xhtml:
            z.writestr("OEBPS/nav.xhtml", nav_xhtml)
        if ncx:
            z.writestr("OEBPS/toc.ncx", ncx)
        for _, h, content in files:
            data = content.encode("utf-8")
            if encrypted and h.startswith("ch"):
                data = bytes(random.Random(1).randrange(256) for _ in range(len(data)))
            z.writestr(f"OEBPS/{h}", data)
        if encrypted:
            enc = (
                '<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
                '<enc:CipherData><enc:CipherReference URI="OEBPS/ch001.xhtml"/></enc:CipherData></enc:EncryptedData>'
                "</encryption>"
            )
            z.writestr("META-INF/encryption.xml", enc)
        if font_obfuscation:
            enc = (
                '<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
                '<enc:CipherData><enc:CipherReference URI="OEBPS/font.otf"/></enc:CipherData></enc:EncryptedData>'
                "</encryption>"
            )
            z.writestr("META-INF/encryption.xml", enc)
    return buf.getvalue()


# --------------------------------------------------------------------------- fb2


def make_fb2(
    title: str = "Тестовая книга fb2",
    author: tuple[str, str] = ("Мария", "Тестова"),
    parts: int = 2,
    chapters_per_part: int = 5,
    paras: int = 60,
    encoding: str = "windows-1251",
) -> bytes:
    sections = []
    ci = 0
    for p in range(parts):
        chs = []
        for _ in range(chapters_per_part):
            ci += 1
            ps = "".join(
                f"<p>{escape(t)}{'<a l:href=\"#n1\" type=\"note\">[1]</a>' if k == 1 else ''}</p>"
                for k, t in enumerate(paragraphs(paras, ci))
            )
            chs.append(f"<section><title><p>Глава {ci}</p></title>{ps}</section>")
        sections.append(f"<section><title><p>Часть {p + 1}</p></title>{''.join(chs)}</section>")
    xml = (
        f'<?xml version="1.0" encoding="{encoding}"?>\n'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">'
        "<description><title-info>"
        f"<author><first-name>{author[0]}</first-name><last-name>{author[1]}</last-name></author>"
        f"<book-title>{escape(title)}</book-title><lang>ru</lang></title-info></description>"
        f"<body><title><p>{escape(title)}</p></title>"
        "<epigraph><p>Эпиграф к книге, короткий.</p></epigraph>"
        f"{''.join(sections)}</body>"
        '<body name="notes"><section id="n1"><title><p>1</p></title><p>Примечание переводчика.</p></section></body>'
        '<binary id="cover.jpg" content-type="image/jpeg">AAAA</binary>'
        "</FictionBook>"
    )
    return xml.encode(encoding)


def make_fb2_zip(**kwargs) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("book.fb2", make_fb2(**kwargs))
    return buf.getvalue()


# --------------------------------------------------------------------------- набор 9.8


def sample_set() -> dict[str, bytes]:
    return {
        "01_clean_with_toc.epub": make_epub("Аккуратная книга", "Анна Аккуратова"),
        "02_no_toc.epub": make_epub("Книга без оглавления", "Борис Безоглавлев", toc="none"),
        "03_one_giant_chapter.epub": make_epub(
            "Одна глава", "Глеб Гигантов", chapters=[("", paragraphs(700, 7))], toc="none", headings=False,
            front_matter=False, back_matter=False,
        ),
        "04_book.fb2": make_fb2(),
        "05_book.fb2.zip": make_fb2_zip(title="Архивная книга"),
        "06_protected.epub": make_epub("Защищённая", "Дмитрий Защитов", encrypted=True),
        "07_scanned_images.epub": make_epub("Скан", "Евгений Сканов", images_only=200, front_matter=False,
                                            back_matter=False),
        "08_very_short.epub": make_epub("Короткая", "Жанна Краткова", chapters=chapters_text(2, 8)),
        "09_very_long.epub": make_epub("Длинная", "Зиновий Длиннов", chapters=chapters_text(80, 120)),
        "10_ncx_single_file_anchors.epub": make_epub(
            "Одним файлом", "Ирина Якорева", toc="ncx", single_file=True, chapters=chapters_text(15, 50)
        ),
    }
