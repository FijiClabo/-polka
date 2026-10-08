"""Слой-адаптер STTProvider: Yandex SpeechKit и Whisper-совместимый API.

Любое аудио (голосовое Telegram, запись из мини-приложения webm/mp4) сначала
приводится ffmpeg к 16 кГц моно. Голосовые файлы не хранятся: только во временной папке.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from settings import Settings, get_settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2  # s16le моно
YANDEX_CHUNK_SEC = 25  # синхронное распознавание: не больше 30 с и 1 МБ на запрос


class STTError(Exception):
    pass


class STTProvider(Protocol):
    name: str

    async def transcribe(self, pcm: bytes) -> str: ...


async def _ffmpeg(input_bytes: bytes, args: list[str]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="dochitka-audio-") as tmp:
        src = Path(tmp) / "in.bin"
        src.write_bytes(input_bytes)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src), *args, "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError as e:
            proc.kill()
            raise STTError("ffmpeg timeout") from e
        if proc.returncode != 0:
            raise STTError(f"ffmpeg: {err.decode(errors='ignore')[:300]}")
        return out


async def to_pcm(audio: bytes) -> bytes:
    return await _ffmpeg(audio, ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le"])


async def pcm_to_ogg(pcm: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="dochitka-audio-") as tmp:
        src = Path(tmp) / "in.raw"
        src.write_bytes(pcm)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "s16le", "-ar", str(SAMPLE_RATE),
            "-ac", "1", "-i", str(src), "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise STTError(f"ffmpeg ogg: {err.decode(errors='ignore')[:300]}")
        return out


def pcm_duration(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SEC


class YandexSTT:
    name = "yandex"
    URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

    def __init__(self, s: Settings):
        self.key = s.yandex_api_key
        self.folder = s.yandex_folder_id

    async def _chunk(self, client: httpx.AsyncClient, chunk: bytes) -> str:
        params = {
            "lang": "ru-RU", "topic": "general", "format": "lpcm", "sampleRateHertz": str(SAMPLE_RATE),
            "folderId": self.folder, "profanityFilter": "false",
        }
        r = await client.post(self.URL, params=params, content=chunk, headers={"Authorization": f"Api-Key {self.key}"})
        if r.status_code != 200:
            raise STTError(f"yandex stt {r.status_code}: {r.text[:200]}")
        return (r.json().get("result") or "").strip()

    async def transcribe(self, pcm: bytes) -> str:
        step = YANDEX_CHUNK_SEC * BYTES_PER_SEC
        chunks = [pcm[i : i + step] for i in range(0, len(pcm), step)] or [b""]
        async with httpx.AsyncClient(timeout=30) as client:
            parts = await asyncio.gather(*(self._chunk(client, c) for c in chunks))
        return " ".join(p for p in parts if p).strip()


class WhisperSTT:
    name = "whisper"

    def __init__(self, s: Settings):
        self.url = s.whisper_api_url
        self.key = s.whisper_api_key
        self.model = s.whisper_model

    async def transcribe(self, pcm: bytes) -> str:
        ogg = await pcm_to_ogg(pcm)
        files = {"file": ("voice.ogg", ogg, "audio/ogg")}
        data = {"model": self.model, "language": "ru", "response_format": "json"}
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(self.url, data=data, files=files, headers=headers)
        if r.status_code != 200:
            raise STTError(f"whisper {r.status_code}: {r.text[:200]}")
        return (r.json().get("text") or "").strip()


def build_stt(s: Settings | None = None) -> list[STTProvider]:
    s = s or get_settings()
    yandex = YandexSTT(s) if s.yandex_api_key and s.yandex_folder_id else None
    whisper = WhisperSTT(s) if s.whisper_api_url and (s.whisper_api_key or "localhost" in s.whisper_api_url) else None
    order = [yandex, whisper] if s.stt_provider != "whisper" else [whisper, yandex]
    return [p for p in order if p is not None]


_stt: list[STTProvider] | None = None


def set_stt(providers: list[STTProvider] | None) -> None:
    global _stt
    _stt = providers


async def transcribe_audio(audio: bytes) -> tuple[str, float]:
    """Распознать любое аудио. Возвращает (текст, длительность в секундах)."""
    global _stt
    if _stt is None:
        _stt = build_stt()
    if not _stt:
        raise STTError("Не настроен ни один провайдер распознавания речи")
    pcm = await to_pcm(audio)
    duration = pcm_duration(pcm)
    errors = []
    for p in _stt:
        try:
            text = await p.transcribe(pcm)
            from ai.usage import record_stt

            record_stt(p.name, duration)
            return text, duration
        except (STTError, httpx.HTTPError) as e:
            log.warning("STT %s failed: %s", p.name, e)
            errors.append(str(e))
    raise STTError("; ".join(errors))
