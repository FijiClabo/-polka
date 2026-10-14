"""Пользователи: создание, профиль, удаление всех данных."""

from __future__ import annotations

from datetime import time

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.days import get_tz
from db.models import (
    Book,
    Consent,
    Enrollment,
    Event,
    Feedback,
    FriendNudge,
    Friendship,
    Notification,
    Pair,
    Purchase,
    Retelling,
    User,
    UserAchievement,
)
from services.common import log_event, now, random_code
from settings import get_settings


async def get_user_by_tg(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def unique_friend_code(session: AsyncSession) -> str:
    for _ in range(20):
        code = random_code(8)
        if not await session.scalar(select(User.id).where(User.friend_code == code)):
            return code
    return random_code(12)


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
    language_code: str | None = None,
    photo_url: str | None = None,
) -> tuple[User, bool]:
    user = await get_user_by_tg(session, tg_id)
    created = False
    if user is None:
        user = User(
            tg_id=tg_id,
            first_name=(first_name or "")[:128],
            last_name=(last_name or None),
            tg_username=username,
            language_code=language_code,
            timezone=get_settings().default_timezone,
            friend_code=await unique_friend_code(session),
            morning_time=time(9, 0),
            evening_time=time(21, 0),
            retell_format="voice",
            nudges_enabled=True,
            onboarding_step="new",
            webapp_onboarded=False,
            bot_blocked=False,
        )
        session.add(user)
        await session.flush()
        await log_event(session, "start", user.id)
        created = True
    else:
        if first_name is not None:
            user.first_name = first_name[:128]
        if last_name is not None:
            user.last_name = last_name
        if username is not None:
            user.tg_username = username
        if language_code:
            user.language_code = language_code
        if user.bot_blocked:
            user.bot_blocked = False
    if photo_url:
        user.photo_url = photo_url[:512]
    user.last_seen_at = now()
    return user, created


# --------------------------------------------------------------------------- как видят другие

AVATAR_SIZE = 320
MAX_AVATAR_BYTES = 8 * 1024 * 1024


def avatars_dir():
    return get_settings().data_dir / "avatars"


def avatar_url(u: User) -> str | None:
    """Фото, которое видят другие: своё, из Telegram или никакого (тогда в приложении — буква)."""
    if u.avatar == "none":
        return None
    if u.avatar == "custom" and u.avatar_token:
        return f"/a/{u.avatar_token}.jpg"
    return u.photo_url


def public_tg(u: User) -> str | None:
    """Username для друзей, напарника и клуба — если человек его не скрыл."""
    return u.tg_username if (u.show_username and u.tg_username) else None


def who(u: User) -> str:
    """Имя для сообщений о людях: «Саша (@sasha)» — чтобы было понятно, кто это в Telegram."""
    tg = public_tg(u)
    return f"{u.display_name} (@{tg})" if tg else u.display_name


def save_avatar(u: User, data: bytes) -> str:
    """Своё фото: квадрат 320×320, JPEG, без метаданных. Старый файл удаляется."""
    import io
    import secrets

    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError("Фото больше 8 МБ — выбери поменьше")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("Это не похоже на фото: нужен JPEG, PNG или WebP") from e
    if img.width * img.height > 40_000_000:
        raise ValueError("Слишком большое фото")
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
    d = avatars_dir()
    d.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    img.save(d / f"{token}.jpg", "JPEG", quality=85)  # метаданные (в том числе геометка) не сохраняются
    remove_avatar_file(u)
    u.avatar_token = token
    u.avatar = "custom"
    return token


def remove_avatar_file(u: User) -> None:
    if u.avatar_token:
        try:
            (avatars_dir() / f"{u.avatar_token}.jpg").unlink(missing_ok=True)
        except OSError:
            pass
        u.avatar_token = None


def valid_timezone(tz: str) -> bool:
    try:
        return get_tz(tz).key == tz
    except Exception:
        return False


async def delete_user_data(session: AsyncSession, user: User) -> None:
    """/delete_me: пересказы, книги, профиль — всё."""
    enr_ids = list(await session.scalars(select(Enrollment.id).where(Enrollment.user_id == user.id)))
    if enr_ids:
        await session.execute(delete(Retelling).where(Retelling.enrollment_id.in_(enr_ids)))
    await session.execute(update(Enrollment).where(Enrollment.user_id == user.id).values(pair_id=None))
    await session.execute(
        delete(Pair).where((Pair.user_a_id == user.id) | (Pair.user_b_id == user.id))
    )
    from services.books import remove_file

    for path in await session.scalars(select(Book.file_path).where(Book.owner_user_id == user.id, Book.file_path.is_not(None))):
        remove_file(path)  # файл книги, если разбор так и не закончился
    await session.execute(delete(Book).where(Book.owner_user_id == user.id))
    await session.execute(delete(Friendship).where((Friendship.user_low_id == user.id) | (Friendship.user_high_id == user.id)))
    await session.execute(delete(FriendNudge).where((FriendNudge.from_user_id == user.id) | (FriendNudge.to_user_id == user.id)))
    await session.execute(delete(Notification).where(Notification.user_id == user.id))
    await session.execute(delete(UserAchievement).where(UserAchievement.user_id == user.id))
    # оценки из опросов остаются обезличенными, свободный текст удаляется
    await session.execute(update(Feedback).where(Feedback.user_id == user.id).values(user_id=None, text=None))
    await session.execute(update(Event).where(Event.user_id == user.id).values(user_id=None))
    # записи об оплатах храним по закону, но обезличенно: без пользователя, e-mail и метки источника
    await session.execute(update(Purchase).where(Purchase.user_id == user.id).values(user_id=None, email=None, source=None))
    await session.execute(delete(Consent).where(Consent.user_id == user.id))
    await session.execute(update(User).where(User.invited_by_id == user.id).values(invited_by_id=None))
    await session.execute(delete(Enrollment).where(Enrollment.user_id == user.id))
    remove_avatar_file(user)
    await session.delete(user)
