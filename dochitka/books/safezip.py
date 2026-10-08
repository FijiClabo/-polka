"""Безопасная работа с zip: лимиты на распакованный объём (защита от архивных бомб)."""

from __future__ import annotations

import io
import zipfile

from books.types import BookParseError

MAX_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRY = 60 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_RATIO = 200  # во сколько раз файл может «раздуваться» при распаковке


class SafeZip:
    def __init__(self, data: bytes):
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError, ValueError) as e:
            raise BookParseError("broken", f"bad zip: {e}") from e
        infos = self.zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BookParseError("zip_bomb", "too many entries")
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise BookParseError("zip_bomb", f"uncompressed {total}")
        for i in infos:
            if i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_RATIO and i.file_size > 5_000_000:
                raise BookParseError("zip_bomb", f"ratio {i.filename}")
        self._names = {i.filename: i for i in infos}
        self._lower = {i.filename.lower(): i.filename for i in infos}

    def names(self) -> list[str]:
        return list(self._names)

    def resolve(self, name: str) -> str | None:
        if name in self._names:
            return name
        return self._lower.get(name.lower())

    def has(self, name: str) -> bool:
        return self.resolve(name) is not None

    def read(self, name: str) -> bytes:
        real = self.resolve(name)
        if real is None:
            raise KeyError(name)
        info = self._names[real]
        if info.flag_bits & 0x1:
            raise BookParseError("drm", "encrypted zip entry")
        try:
            with self.zf.open(info) as f:
                data = f.read(MAX_ENTRY + 1)
        except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, RuntimeError) as e:
            raise BookParseError("broken", f"read {name}: {e}") from e
        if len(data) > MAX_ENTRY:
            raise BookParseError("zip_bomb", f"entry too big {name}")
        return data
