"""Согласие на обработку персональных данных (152-ФЗ): отдельный документ, журнал с версией и хешем."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Consent, User
from services.common import log_event
from settings import get_settings


async def has_consent(session: AsyncSession, user: User) -> bool:
    """Согласие на текущую редакцию: если текст согласия поменялся (CONSENT_VERSION), спросим заново."""
    return bool(await session.scalar(
        select(Consent.id).where(Consent.user_id == user.id, Consent.doc_type == "pd", Consent.revoked_at.is_(None),
                                 Consent.doc_version == get_settings().consent_version).limit(1)
    ))


async def give_consent(session: AsyncSession, user: User, channel: str) -> None:
    if await has_consent(session, user):
        return
    session.add(Consent(user_id=user.id, doc_type="pd", doc_version=get_settings().consent_version,
                        doc_sha256=rendered_sha256("consent.html"), channel=channel))
    await log_event(session, "consent_pd", user.id, channel=channel)


def rendered_sha256(name: str) -> str | None:
    """Хеш того текста, который видел человек: с подставленными реквизитами и датой, а не шаблона."""
    try:
        from api.pages import render

        return hashlib.sha256(render(name).encode("utf-8")).hexdigest()
    except Exception:  # страница не собирается (нет файла) — хотя бы хеш шаблона
        return doc_sha256(name)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def doc_sha256(name: str) -> str | None:
    """Хеш текста документа на момент согласия — чтобы потом доказать, с какой редакцией человек согласился."""
    f = WEB_DIR / name
    return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
