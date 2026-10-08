import pytest

from books.parse import detect_format, parse_book
from books.plan import (
    PlanError,
    build_paper_plan,
    build_plan,
    plan_options,
)
from books.samples import chapters_text, make_epub, make_fb2, make_fb2_zip, paragraphs, sample_set
from books.types import CHARS_PER_PAGE, BookParseError, ParsedChapter

SAMPLES = sample_set()


# --------------------------------------------------------------------------- набор 9.8


@pytest.mark.parametrize(
    "name,expect",
    [
        ("01_clean_with_toc.epub", "ok"),
        ("02_no_toc.epub", "ok"),
        ("03_one_giant_chapter.epub", "ok"),
        ("04_book.fb2", "ok"),
        ("05_book.fb2.zip", "ok"),
        ("06_protected.epub", "drm"),
        ("07_scanned_images.epub", "no_text"),
        ("08_very_short.epub", "too_short"),
        ("09_very_long.epub", "ok"),
        ("10_ncx_single_file_anchors.epub", "ok"),
    ],
)
def test_sample_set_expected_results(name, expect):
    data = SAMPLES[name]
    if expect == "ok":
        book = parse_book(name, data)
        assert book.total_pages >= 20
        assert book.chapters
    else:
        with pytest.raises(BookParseError) as e:
            parse_book(name, data)
        assert e.value.code == expect
        assert e.value.human  # понятное сообщение для пользователя


def test_clean_epub_metadata_and_service_parts_dropped():
    book = parse_book("x.epub", SAMPLES["01_clean_with_toc.epub"])
    assert book.title == "Аккуратная книга"
    assert book.author == "Анна Аккуратова"
    titles = [c.title for c in book.chapters]
    assert titles == [f"Глава {i}" for i in range(1, 13)]
    text = "\n".join(c.text for c in book.chapters)
    assert "ISBN" not in text
    assert "Сноска" not in text  # сноски отброшены
    assert "Автор родился" not in text  # «об авторе» в конце отброшено
    assert "Купите ещё" not in text


def test_epub_without_toc_uses_headings():
    book = parse_book("x.epub", SAMPLES["02_no_toc.epub"])
    assert book.has_chapters
    assert book.chapters[0].title == "Глава 1"


def test_giant_chapter_has_no_chapters_and_day_titles():
    book = parse_book("x.epub", SAMPLES["03_one_giant_chapter.epub"])
    assert not book.has_chapters
    segs = build_plan(book.chapters, 21, book.has_chapters)
    assert [s.title for s in segs[:2]] == ["День 1", "День 2"]


def test_fb2_parts_notes_and_encoding():
    book = parse_book("b.fb2", SAMPLES["04_book.fb2"])
    assert book.author == "Мария Тестова"
    assert book.chapters[0].title.startswith("Часть 1. Глава 1")
    assert any(c.title == "Глава 6" or c.title.endswith("Глава 6") for c in book.chapters)
    text = "\n".join(c.text for c in book.chapters)
    assert "Примечание переводчика" not in text
    assert "[1]" not in text


def test_fb2_zip_same_as_fb2():
    book = parse_book("b.fb2.zip", make_fb2_zip())
    assert len(book.chapters) == 10


def test_ncx_anchors_inside_single_file():
    book = parse_book("x.epub", SAMPLES["10_ncx_single_file_anchors.epub"])
    assert len(book.chapters) == 15
    assert book.chapters[3].title == "Глава 4"


def test_font_obfuscation_is_not_drm():
    data = make_epub(chapters=chapters_text(12, 40), font_obfuscation=True)
    book = parse_book("x.epub", data)
    assert book.total_pages > 20


def test_unsupported_and_broken():
    with pytest.raises(BookParseError) as e:
        parse_book("book.pdf", b"%PDF-1.4")
    assert e.value.code == "unsupported"
    with pytest.raises(BookParseError) as e:
        parse_book("book.epub", b"not a zip")
    assert e.value.code == "broken"
    with pytest.raises(BookParseError) as e:
        parse_book("book.fb2", b"<FictionBook><body>")
    assert e.value.code == "broken"


def test_xml_entity_bomb_is_rejected():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        b'<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><section><p>&lol2;</p></section></body></FictionBook>'
    )
    with pytest.raises(BookParseError):
        parse_book("bomb.fb2", bomb)


def test_zip_bomb_rejected():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("book.fb2", b"\0" * (70 * 1024 * 1024))
    with pytest.raises(BookParseError) as e:
        parse_book("x.fb2.zip", buf.getvalue())
    assert e.value.code == "zip_bomb"


def test_detect_format():
    assert detect_format("A.EPUB") == "epub"
    assert detect_format("a.fb2.zip") == "fb2.zip"
    assert detect_format("a.fb2") == "fb2"
    assert detect_format("a.mobi") is None


# --------------------------------------------------------------------------- build_plan


def _chapters(sizes_pages: list[float]) -> list[ParsedChapter]:
    out = []
    for i, pages in enumerate(sizes_pages):
        n = max(1, int(pages * CHARS_PER_PAGE / 450))
        out.append(ParsedChapter(f"Глава {i + 1}", paragraphs(n, i + 1, (60, 70))))
    return out


def test_build_plan_exact_count_and_continuity():
    chs = _chapters([12] * 25)
    segs = build_plan(chs, 30)
    assert len(segs) == 30
    assert [s.day_number for s in segs] == list(range(1, 31))
    assert segs[0].pos_from == 0 and segs[-1].pos_to == 1.0
    for a, b in zip(segs, segs[1:], strict=False):
        assert a.pos_to <= b.pos_from + 1e-6
        assert b.page_from >= a.page_from
    # весь текст сохранён, ничего не потеряно и не задвоено
    joined = "\n".join(s.text for s in segs)
    assert joined == "\n".join("\n".join(c.paragraphs) for c in chs)


def test_build_plan_prefers_chapter_boundaries():
    chs = _chapters([10] * 30)
    segs = build_plan(chs, 30)
    assert [s.title for s in segs] == [f"Глава {i}" for i in range(1, 31)]


def test_build_plan_merges_short_chapters():
    chs = _chapters([3] * 40)  # 120 стр., 21 день → примерно по 2 главы
    segs = build_plan(chs, 21)
    assert any(" — " in s.title for s in segs)
    sizes = [len(s.text) for s in segs]
    avg = sum(sizes) / len(sizes)
    assert max(sizes) <= avg * 1.6


def test_build_plan_splits_long_chapter_into_parts():
    chs = _chapters([10, 40, 10])
    segs = build_plan(chs, 6)
    titles = [s.title for s in segs]
    assert any("Глава 2 (часть 1 из" in t for t in titles)
    assert any("(часть 2 из" in t for t in titles)


def test_build_plan_never_cuts_inside_paragraph():
    chs = _chapters([20] * 5)
    all_paras = {p for c in chs for p in c.paragraphs}
    for s in build_plan(chs, 21):
        for line in s.text.split("\n"):
            assert line in all_paras


def test_build_plan_uniform_volume_within_tolerance():
    chs = _chapters([7, 25, 4, 4, 18, 9, 30, 2, 12, 15, 6, 11])
    segs = build_plan(chs, 21)
    sizes = [len(s.text) for s in segs]
    avg = sum(sizes) / len(sizes)
    assert max(sizes) <= avg * 1.8 and min(sizes) >= avg * 0.4


def test_build_plan_too_few_paragraphs():
    with pytest.raises(PlanError):
        build_plan([ParsedChapter("a", ["x" * 100] * 3)], 21)


# --------------------------------------------------------------------------- варианты срока


def test_plan_options_normal_book():
    opts = plan_options(300, 300 * 260)
    days = [o.days for o in opts]
    assert days == [21, 30, 45, 60]  # 14.3, 10, 6.7, 5 страниц в день
    rec = [o for o in opts if o.recommended]
    assert len(rec) == 1 and rec[0].days == 30


def test_plan_options_respect_page_limits():
    # 500 страниц: 21 день = 23.8 стр. — не предлагается
    days = [o.days for o in plan_options(500, 500 * 260)]
    assert 21 not in days and 30 in days


def test_plan_options_long_book_only_60_with_warning():
    opts = plan_options(1500, 1500 * 260)
    assert [o.days for o in opts] == [60]
    assert opts[0].warning


def test_plan_options_short_book():
    opts = plan_options(40, 40 * 260)
    assert [o.days for o in opts] == [7, 14]
    assert sum(o.recommended for o in opts) == 1


def test_plan_options_sprint():
    opts = plan_options(50, 50 * 260, sprint=True)
    assert [o.days for o in opts] == [7]


def test_plan_minutes():
    o = plan_options(300, 60000)[1]
    assert o.minutes_per_day == round(60000 / 200 / o.days)


# --------------------------------------------------------------------------- бумажная книга


def test_paper_plan():
    segs = build_paper_plan(320, 30)
    assert len(segs) == 30
    assert segs[0].page_from == 1 and segs[-1].page_to == 320
    assert all(s.text is None for s in segs)
    for a, b in zip(segs, segs[1:], strict=False):
        assert b.page_from == a.page_to + 1
    assert segs[3].title.startswith("стр. ")


def test_paper_plan_too_few_pages():
    with pytest.raises(PlanError):
        build_paper_plan(10, 21)


def test_long_book_plan_is_built():
    book = parse_book("x.epub", SAMPLES["09_very_long.epub"])
    segs = build_plan(book.chapters, 60, book.has_chapters)
    assert len(segs) == 60


def test_fb2_utf8():
    book = parse_book("u.fb2", make_fb2(encoding="utf-8"))
    assert book.title == "Тестовая книга fb2"
