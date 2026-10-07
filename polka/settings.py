"""Настройки проекта. Всё берётся из переменных окружения (или файла .env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Продукт -------------------------------------------------------------
    project_name: str = "Полка"  # название не зашито в код: меняется здесь
    support_contact: str = ""  # @username для вопросов, показывается в /help

    # --- Telegram ------------------------------------------------------------
    bot_token: str = ""
    bot_mode: str = "polling"  # polling (локально) | webhook (сервер)
    public_url: str = ""  # https://polka.example.com — без слеша в конце
    webhook_secret: str = ""
    webhook_path: str = "/tg/webhook"
    webapp_url: str = ""  # по умолчанию public_url + "/app/"
    admin_tg_ids: str = ""  # через запятую

    # --- База ----------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://polka:polka@localhost:5432/polka"

    # --- ИИ ------------------------------------------------------------------
    llm_providers: str = "anthropic,yandex"  # порядок = приоритет
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"  # проверка пересказов
    anthropic_model_cheap: str = "claude-haiku-4-5"  # краткие содержания отрезков (дёшево)
    anthropic_effort: str = "low"  # low | medium | high: для проверки пересказа хватает low
    anthropic_refusal_fallback: bool = True  # серверный фолбэк при отказе модели
    anthropic_base_url: str = ""  # если нужен прокси до API
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandexgpt_model: str = "yandexgpt/latest"
    yandexgpt_model_cheap: str = "yandexgpt-lite/latest"
    llm_timeout_sec: float = 40.0

    # --- Речь в текст --------------------------------------------------------
    stt_provider: str = "yandex"  # yandex | whisper
    whisper_api_url: str = "https://api.openai.com/v1/audio/transcriptions"
    whisper_api_key: str = ""
    whisper_model: str = "whisper-1"
    voice_max_sec: int = 180

    # --- Время ---------------------------------------------------------------
    default_timezone: str = "Europe/Moscow"
    # Ускоренное время для отладки: «день» длится N минут. 0 = обычное время.
    fast_day_minutes: int = 0
    tick_seconds: int = 30
    allow_timewarp: bool = False  # /timewarp N — сдвинуть часы бота (только для тестового прогона)

    # --- Файлы ---------------------------------------------------------------
    data_dir: Path = Path("./data")
    max_book_mb: int = 20

    # --- Оплата (этап 1 — вручную) ------------------------------------------
    payment_info: str = (
        "Оплата забега — переводом. Реквизиты пришлёт ведущий. "
        "После оплаты доступ откроется автоматически."
    )
    sprint_for_everyone: bool = False

    # --- Прочее --------------------------------------------------------------
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    dev_auth_bypass: bool = Field(default=False, description="Только для локальной вёрстки без Telegram")

    @field_validator("public_url", "webapp_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def admin_ids(self) -> set[int]:
        out: set[int] = set()
        for part in self.admin_tg_ids.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return out

    @property
    def resolved_webapp_url(self) -> str:
        if self.webapp_url:
            return self.webapp_url
        return f"{self.public_url}/app" if self.public_url else ""

    @property
    def llm_order(self) -> list[str]:
        return [p.strip().lower() for p in self.llm_providers.split(",") if p.strip()]

    @property
    def books_dir(self) -> Path:
        return self.data_dir / "uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()
