"""Сохранить синтетический набор книг (10 файлов раздела 9.8) для ручной проверки бота.

    python -m scripts.make_samples ./samples
"""

from __future__ import annotations

import sys
from pathlib import Path

from books.samples import sample_set


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in sample_set().items():
        (out / name).write_bytes(data)
        print(f"{out / name}  ({len(data) // 1024} КБ)")


if __name__ == "__main__":
    main()
