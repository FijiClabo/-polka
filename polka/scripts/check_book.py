"""Проверить, как разбираются твои файлы книг (раздел 9.8 ТЗ).

    python -m scripts.check_book путь/к/книге.epub [ещё файлы...]

Печатает: формат, название, автор, страниц, глав, варианты срока и первые отрезки,
или понятную причину отказа. Ничего не сохраняет.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from books.parse import parse_book
from books.plan import build_plan, plan_options
from books.types import BookParseError


def main(paths: list[str]) -> None:
    if not paths:
        print(__doc__)
        return
    for p in paths:
        path = Path(p)
        print(f"\n=== {path.name} ({path.stat().st_size // 1024} КБ)")
        t = time.monotonic()
        try:
            book = parse_book(path.name, path.read_bytes())
        except BookParseError as e:
            print(f"  ❌ НЕ РАЗОБРАН: {e.human}  [{e.code}: {e.detail}]")
            continue
        dt = time.monotonic() - t
        print(f"  ✅ «{book.title}» — {book.author or 'автор не указан'}")
        print(f"  {book.total_pages} усл. стр. · {book.total_words} слов · ≈ {book.reading_minutes // 60} ч чтения · "
              f"глав: {len(book.chapters) if book.has_chapters else 'нет деления'} · разбор {dt:.1f} с")
        if book.has_chapters:
            titles = [c.title or "(без названия)" for c in book.chapters]
            print(f"  Главы: {', '.join(titles[:6])}{' …' if len(titles) > 6 else ''}")
        opts = plan_options(book.total_pages, book.total_words)
        print("  Сроки: " + "; ".join(
            f"{o.days} дн. = {o.pages_per_day:g} стр./{o.minutes_per_day} мин{' ⭐' if o.recommended else ''}"
            + (f" ({o.warning})" if o.warning else "") for o in opts))
        rec = next(o for o in opts if o.recommended)
        segs = build_plan(book.chapters, rec.days, book.has_chapters)
        sizes = [s.page_to - s.page_from + 1 for s in segs]
        print(f"  План на {rec.days} дн.: отрезки {min(sizes)}–{max(sizes)} стр.")
        for s in segs[:3]:
            print(f"    День {s.day_number}: {s.title} · стр. {s.page_from}–{s.page_to}")


if __name__ == "__main__":
    main(sys.argv[1:])
