"""Модель данных. Соответствует разделу 4 ТЗ плюс несколько служебных полей."""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    from core import clock

    return clock.real_now()


def _now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128))
    photo_url: Mapped[str | None] = mapped_column(String(512))
    language_code: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    morning_time: Mapped[time] = mapped_column(Time, default=time(9, 0))
    evening_time: Mapped[time] = mapped_column(Time, default=time(21, 0))
    retell_format: Mapped[str] = mapped_column(String(8), default="voice")  # voice | text
    friend_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    invited_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    nudges_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    onboarding_step: Mapped[str] = mapped_column(String(32), default="new")
    onboarding_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    webapp_onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # продажи: откуда пришёл, промокод, права доступа
    source: Mapped[str | None] = mapped_column(String(64))  # метка из ссылки ?start=src_xxx / промокод / друг
    promo_code: Mapped[str | None] = mapped_column(String(32))  # введённый, ещё не использованный промокод
    run_credits: Mapped[int] = mapped_column(Integer, default=0)  # оплаченные, но не начатые забеги
    subscription_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_kind: Mapped[str | None] = mapped_column(String(16))  # month | year
    sub_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email: Mapped[str | None] = mapped_column(String(128))  # для чека 54-ФЗ, если продавец их формирует
    created_at: Mapped[datetime] = _now_col()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def display_name(self) -> str:
        return (self.first_name or self.tg_username or "Читатель").strip()


class Book(Base):
    """Книга принадлежит одному пользователю. Текст доступен только владельцу."""

    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(8))  # epub | fb2 | paper
    title: Mapped[str] = mapped_column(String(512), default="")
    author: Mapped[str] = mapped_column(String(512), default="")
    title_norm: Mapped[str] = mapped_column(String(512), default="", index=True)
    author_norm: Mapped[str] = mapped_column(String(512), default="")
    total_pages: Mapped[int] = mapped_column(Integer, default=0)
    total_words: Mapped[int | None] = mapped_column(Integer)
    total_chars: Mapped[int | None] = mapped_column(Integer)
    chapters_count: Mapped[int] = mapped_column(Integer, default=0)
    spine_color: Mapped[str] = mapped_column(String(16), default="#C9644F")
    file_path: Mapped[str | None] = mapped_column(String(512))
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    file_size: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | ok | failed
    parse_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Chapter.order_no"
    )
    segments: Mapped[list[Segment]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Segment.day_number"
    )

    @property
    def has_text(self) -> bool:
        return self.source in ("epub", "fb2") and self.deleted_at is None


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    order_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512), default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")  # абзацы через \n

    book: Mapped[Book] = relationship(back_populates="chapters")


class Segment(Base):
    """Отрезок книги на один день плана."""

    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("book_id", "day_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    day_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512), default="")
    page_from: Mapped[int] = mapped_column(Integer)
    page_to: Mapped[int] = mapped_column(Integer)
    pos_from: Mapped[float] = mapped_column(Float)
    pos_to: Mapped[float] = mapped_column(Float)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str | None] = mapped_column(Text)  # у бумажной книги текста нет
    summary: Mapped[str | None] = mapped_column(Text)
    retell_prompt: Mapped[str | None] = mapped_column(String(512))  # вопрос-приглашение к пересказу

    book: Mapped[Book] = relationship(back_populates="segments")


class Run(Base):
    """Забег: общий день старта для группы. kind=sprint — бесплатный личный спринт."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(16), default="main")  # main | sprint
    start_date: Mapped[date | None] = mapped_column(Date)
    grace_days: Mapped[int] = mapped_column(Integer, default=3)
    price_rub: Mapped[int] = mapped_column(Integer, default=990)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | open | active | finished
    host_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now_col()


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("run_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("books.id", ondelete="SET NULL"))
    plan_days: Mapped[int | None] = mapped_column(Integer)
    plan_start_date: Mapped[date | None] = mapped_column(Date)
    plan_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # invited | paid | active | finished | dropped | refunded
    status: Mapped[str] = mapped_column(String(16), default="invited", index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pair_id: Mapped[int | None] = mapped_column(ForeignKey("pairs.id", ondelete="SET NULL"))
    pair_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    freezes_left: Mapped[int] = mapped_column(Integer, default=1)
    freezes_week_start: Mapped[int] = mapped_column(Integer, default=0)  # индекс недели плана
    freezes_per_week: Mapped[int] = mapped_column(Integer, default=1)  # 2 — по абонементу
    access: Mapped[str | None] = mapped_column(String(16))  # как открыт доступ: purchase | credit | subscription | free | manual
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"))
    last_closed_day: Mapped[date | None] = mapped_column(Date)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conspect_intro: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()

    run: Mapped[Run] = relationship(lazy="joined")
    book: Mapped[Book | None] = relationship(lazy="joined")


class Pair(Base):
    __tablename__ = "pairs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    user_a_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    user_b_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    streak: Mapped[int] = mapped_column(Integer, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_closed_day: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = _now_col()


class Retelling(Base):
    """Попытка сдать отрезок. Уточнения внутри одной сдачи — в dialog_turns."""

    __tablename__ = "retellings"
    __table_args__ = (Index("ix_retellings_enr_day", "enrollment_id", "user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"))
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("segments.id", ondelete="SET NULL"))
    user_day: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(8))  # voice | text
    via: Mapped[str] = mapped_column(String(8), default="bot")  # bot | webapp
    raw_text: Mapped[str] = mapped_column(Text, default="")
    voice_file_id: Mapped[str | None] = mapped_column(String(256))
    voice_duration_sec: Mapped[int | None] = mapped_column(Integer)
    # pending | accepted | clarify | rejected
    verdict: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    ai_reply: Mapped[str | None] = mapped_column(Text)
    ai_question: Mapped[str | None] = mapped_column(Text)
    note_for_summary: Mapped[str | None] = mapped_column(Text)
    clarify_count: Mapped[int] = mapped_column(Integer, default=0)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str | None] = mapped_column(String(32))
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _now_col()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    turns: Mapped[list[DialogTurn]] = relationship(
        back_populates="retelling", cascade="all, delete-orphan", order_by="DialogTurn.id"
    )


class DialogTurn(Base):
    __tablename__ = "dialog_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    retelling_id: Mapped[int] = mapped_column(ForeignKey("retellings.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(8))  # user | ai
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()

    retelling: Mapped[Retelling] = relationship(back_populates="turns")


class DayResult(Base):
    __tablename__ = "day_results"
    __table_args__ = (UniqueConstraint("enrollment_id", "user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    user_day: Mapped[date] = mapped_column(Date)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("segments.id", ondelete="SET NULL"))
    result: Mapped[str] = mapped_column(String(8))  # done | frozen | missed
    created_at: Mapped[datetime] = _now_col()


class Friendship(Base):
    __tablename__ = "friendships"
    __table_args__ = (UniqueConstraint("user_low_id", "user_high_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_low_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    user_high_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    invited_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now_col()


class FriendNudge(Base):
    __tablename__ = "friend_nudges"
    __table_args__ = (UniqueConstraint("from_user_id", "to_user_id", "user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    user_day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(8), default="friend")  # friend | partner
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now_col()


class Achievement(Base):
    __tablename__ = "achievements"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class UserAchievement(Base):
    __tablename__ = "user_achievements"
    __table_args__ = (UniqueConstraint("user_id", "achievement_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    achievement_code: Mapped[str] = mapped_column(ForeignKey("achievements.code", ondelete="CASCADE"))
    awarded_at: Mapped[datetime] = _now_col()
    context: Mapped[dict | None] = mapped_column(JSONType)


class Notification(Base):
    """Сообщения бота по своей инициативе: для лимитов и идемпотентности рассылок."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("user_id", "user_day", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    user_day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))  # morning | evening | partner | nudge | no_book
    key: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = _now_col()


class Event(Base):
    """Журнал событий для метрик теста."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    type: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _now_col()


class AppState(Base):
    """Небольшое хранилище ключ-значение (якорь ускоренного времени и т.п.)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Purchase(Base):
    """Оплата: разовый забег, абонемент на месяц или год.

    Кнопка «Оплатить» в боте или мини-приложении создаёт заказ (pending) и платёж в ЮKassa;
    после подтверждения ЮKassa заказ становится paid и человек сразу получает доступ.
    """

    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    product: Mapped[str] = mapped_column(String(16))  # run | month | year
    provider: Mapped[str] = mapped_column(String(16))  # yookassa | manual | promo
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    amount: Mapped[int] = mapped_column(Integer)  # копейки
    list_amount: Mapped[int] = mapped_column(Integer, default=0)  # цена без скидки, копейки
    promo_code: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="paid")  # pending | paid | canceled | refunded
    provider_charge_id: Mapped[str | None] = mapped_column(String(128), index=True)  # id платежа ЮKassa
    order_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str | None] = mapped_column(String(64))
    offer_version: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = _now_col()
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Consent(Base):
    """Журнал согласий (152-ФЗ): какой документ, какой версии, когда и где принят."""

    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"))
    doc_type: Mapped[str] = mapped_column(String(16))  # pd | offer | marketing
    doc_version: Mapped[str] = mapped_column(String(16))
    doc_sha256: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(8))  # bot | webapp | web
    email: Mapped[str | None] = mapped_column(String(128))
    accepted_at: Mapped[datetime] = _now_col()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PromoCode(Base):
    """Промокод: скидка и атрибуция (например, блогер, который привёл аудиторию)."""

    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)  # в верхнем регистре
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)  # 100 — бесплатно
    products: Mapped[str] = mapped_column(String(32), default="run,month,year")
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(64))  # чей код (для выплат партнёру)
    # пробный доступ: при скидке 100% абонемент выдаётся на столько дней (вместо 30 или 365)
    trial_days: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col()
