"""Согласие на обработку персональных данных (152-ФЗ): отдельный документ, журнал с версией и хешем."""

from __future__ import annotations

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
    from services.site_orders import doc_sha256

    s = get_settings()
    session.add(Consent(user_id=user.id, doc_type="pd", doc_version=s.offer_version,
                        doc_sha256=doc_sha256("consent.html"), channel=channel))
    await log_event(session, "consent_pd", user.id, channel=channel)
