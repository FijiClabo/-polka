"""Выдача ачивок: проверка условий, запись, одно сообщение на событие."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from core.achievements import ACH_BY_CODE, ACHIEVEMENTS, AchievementContext, check_achievements
from db.models import User, UserAchievement
from services.common import Outbox, OutMsg, log_event


async def user_codes(session: AsyncSession, user_id: int) -> set[str]:
    return set(await session.scalars(select(UserAchievement.achievement_code).where(UserAchievement.user_id == user_id)))


async def award(session: AsyncSession, user: User, ctx: AchievementContext, outbox: Outbox | None) -> list[str]:
    """Выдать новые ачивки. Не выдаёт повторно (и на уровне базы — уникальный индекс)."""
    already = await user_codes(session, user.id)
    new = check_achievements(ctx, already)
    given: list[str] = []
    for code in new:
        try:
            async with session.begin_nested():
                session.add(UserAchievement(user_id=user.id, achievement_code=code, context={"trigger": ctx.trigger}))
        except IntegrityError:
            continue
        given.append(code)
        await log_event(session, "achievement_awarded", user.id, code=code)
    if given and outbox is not None and not user.bot_blocked:
        # не более одной ачивки в сообщении; если выдано несколько — объединяем в одно
        outbox.add(OutMsg(tg_id=user.tg_id, text=texts.achievements_message(given), kind="achievement",
                          buttons=[[{"text": "Мои значки", "webapp": "profile"}]]))
    return given


def all_with_status(codes_with_dates: dict[str, object]) -> list[dict]:
    out = []
    for code, title, desc, order in ACHIEVEMENTS:
        out.append({
            "code": code, "title": title, "description": desc, "order": order,
            "earned": code in codes_with_dates,
            "awarded_at": codes_with_dates.get(code).isoformat() if code in codes_with_dates else None,  # type: ignore[union-attr]
        })
    return out


def title_of(code: str) -> str:
    a = ACH_BY_CODE.get(code)
    return a[1] if a else code
