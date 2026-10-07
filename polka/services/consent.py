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
    return bool(await session.scalar(
        select(Consent.id).where(Consent.user_id == user.id, Consent.doc_type == "pd", Consent.revoked_at.is_(None)).limit(1)
    ))


async def give_consent(session: AsyncSession, user: User, channel: str) -> None:
    if await has_consent(session, user):
        return
    s = get_settings()
    session.add(Consent(user_id=user.id, doc_type="pd", doc_version=s.offer_version,
                        doc_sha256=doc_sha256("consent.html"), channel=channel))
    await log_event(session, "consent_pd", user.id, channel=channel)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def doc_sha256(name: str) -> str | None:
    """Хеш текста документа на момент согласия — чтобы потом доказать, с какой редакцией человек согласился."""
    f = WEB_DIR / name
    return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
