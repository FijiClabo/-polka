"""Планировщик: простой цикл раз в N секунд.

Каждый проход идемпотентен (уникальные ключи уведомлений, last_closed_day), поэтому
перезапуск или пропущенные проходы ничего не ломают: всё догоняется на следующем.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import and_, or_, select

import texts
from ai.checker import summarize_segment
from ai.llm import LLMUnavailable, get_llm
from books.plan import segment_minutes
from bot.ui import flush
from core import clock
from core.days import clock_minutes_in_user_day, minutes_since_day_start, plan_day_number
from db.models import Book, DayResult, Enrollment, Retelling, Run, Segment, User
from db.session import session_scope
from services.books import remove_file
from services.common import Outbox, OutMsg, allow_initiative, allow_once, log_event, today_for
from services.flow import process_pending_queue
from services.progress import close_pending_days, load_view
from services.social import get_pair_for, public_status
from settings import get_settings

log = logging.getLogger(__name__)


def _due(user: User, t) -> bool:
    return minutes_since_day_start(clock.now(), user.timezone) >= clock_minutes_in_user_day(t)


async def tick(bot: Bot) -> None:
    """Каждый шаг — отдельно: сбой одного (например, в очереди пересказов) не останавливает остальные,
    в том числе удаление старых текстов, которое мы обещаем в политике."""
    outbox = Outbox()
    await _safe(_forget_stale_texts())
    await _safe(_activate_runs())
    await _safe(_stuck_books())
    await _safe(_daily(outbox))
    await _safe(_sales(outbox))
    await _safe(process_pending_queue(outbox))
    await _safe(flush(bot, outbox))
    await _safe(_summaries(limit=5))
    await _safe(_flush_ai_usage())


async def _flush_ai_usage() -> None:
    from ai.usage import flush

    await flush()


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("scheduler step failed")


async def _stuck_books() -> None:
    """Разбор, прерванный перезапуском сервера, не должен висеть вечно: помечаем «не удалось» — можно загрузить снова."""
    async with session_scope() as s:
        books = list(await s.scalars(
            select(Book).where(Book.parse_status == "pending", Book.created_at < clock.now() - timedelta(minutes=15))
        ))
        for b in books:
            b.parse_status = "failed"
            b.parse_error = "interrupted"
            remove_file(b.file_path)  # исходный файл после неудачного разбора не храним
            b.file_path = None


async def _activate_runs() -> None:
    async with session_scope() as s:
        runs = list(await s.scalars(select(Run).where(Run.kind == "main", Run.status == "open", Run.start_date.is_not(None))))
        for r in runs:
            if r.start_date <= clock.now().date():
                r.status = "active"


async def _daily(outbox: Outbox) -> None:
    async with session_scope() as s:
        ids = list(await s.scalars(select(Enrollment.id).where(Enrollment.status.in_(("paid", "active")))))
    for eid in ids:
        try:
            async with session_scope() as s:
                enr = await s.scalar(select(Enrollment).where(Enrollment.id == eid).with_for_update(of=Enrollment))
                if enr is None or enr.status not in ("paid", "active"):
                    continue
                user = await s.get(User, enr.user_id)
                if user is None:
                    continue
                await close_pending_days(s, enr, user, outbox)  # дни закрываются и у тех, кто заблокировал бота
                if not user.bot_blocked:
                    await _morning_evening(s, enr, user, outbox)
        except Exception:
            log.exception("daily job failed for enrollment %s", eid)


async def _morning_evening(s, enr: Enrollment, user: User, outbox: Outbox) -> None:
    run = enr.run
    today = today_for(user)
    # книга не добавлена / план не подтверждён, а забег уже идёт — раз в день напоминаем
    if not enr.plan_days or enr.plan_start_date is None:
        if run.kind == "main" and run.start_date and today >= run.start_date and enr.status in ("paid", "active"):
            if _due(user, user.morning_time) and await allow_initiative(s, user, "no_book"):
                outbox.add(OutMsg(user.tg_id, texts.NO_BOOK_REMINDER, kind="no_book",
                                  buttons=[[{"text": "Добавить книгу", "webapp": "book"}]]))
        return
    if plan_day_number(enr.plan_start_date, today) < 1:
        return
    view = await load_view(s, user, enr)
    if view.state not in ("to_read", "clarify", "checking", "done_today"):
        return

    if _due(user, user.morning_time) and view.state in ("to_read", "clarify") and view.segment:
        if await allow_initiative(s, user, "morning"):
            seg = view.segment
            book = view.book
            paper = book.source == "paper"
            minutes = segment_minutes(seg.word_count, seg.page_to - seg.page_from + 1, paper)
            yesterday = await s.scalar(
                select(DayResult.result).where(DayResult.enrollment_id == enr.id, DayResult.user_day == today - timedelta(days=1))
            )
            _, partner, _ = await get_pair_for(s, enr)
            text = texts.morning(
                user.display_name, view.plan_day, enr.plan_days, seg.title, f"стр. {seg.page_from}–{seg.page_to}", minutes,
                catching_up=view.catching_up, yesterday=yesterday if yesterday in ("frozen", "missed") else None,
                streak=enr.streak, partner=partner.display_name if partner else None, run_started=view.plan_day == 1,
            )
            rows = []
            if book.has_text:
                rows.append([{"text": "Читать отрезок", "webapp": f"read?d={seg.day_number}"}])
            rows.append([{"text": "Открыть приложение", "webapp": "today"}])
            outbox.add(OutMsg(user.tg_id, text, buttons=rows, kind="morning"))
            await log_event(s, "segment_sent", user.id, enr.run_id, day=seg.day_number)
        return

    if _due(user, user.evening_time) and view.state in ("to_read", "clarify") and not view.done_today:
        if await allow_initiative(s, user, "evening"):
            _, partner, _ = await get_pair_for(s, enr)
            p_done = False
            if partner:
                st = await public_status(s, partner)
                p_done = st.today == "done"
            outbox.add(OutMsg(user.tg_id, texts.evening(partner.display_name if partner else None, p_done),
                              buttons=[[{"text": "Открыть отрезок", "webapp": "today"}]], kind="evening"))


async def _sales(outbox: Outbox) -> None:
    """Напоминания, которые помогают продажам: каждое уходит человеку один раз и не раньше утра."""
    from services.billing import subscription_active
    from services.runs import sprint_used

    now = clock.now()
    try:
        async with session_scope() as s:
            # план собран, а доступ так и не открыт — одно напоминание на следующий день
            rows = (await s.execute(
                select(Enrollment, User).join(User, User.id == Enrollment.user_id).where(
                    Enrollment.status == "invited", Enrollment.plan_confirmed_at.is_not(None),
                    Enrollment.plan_confirmed_at < now - timedelta(hours=20),
                    Enrollment.plan_confirmed_at > now - timedelta(days=7),
                    User.bot_blocked.is_(False),
                )
            )).all()
            for enr, user in rows:
                if not _due(user, user.morning_time) or subscription_active(user) or user.run_credits > 0:
                    continue
                if not await allow_once(s, user, "paywall", f"paywall:{enr.id}"):
                    continue
                book = await s.get(Book, enr.book_id) if enr.book_id else None
                buttons = [[{"text": "Открыть забег", "webapp": "pay"}]]
                if not await sprint_used(s, user.id):
                    buttons.append([{"text": "Сначала спринт на 7 дней — бесплатно", "callback": "sprint:start"}])
                outbox.add(OutMsg(user.tg_id, texts.paywall_nudge(book.title if book else None),
                                  buttons=buttons, kind="paywall"))
                await log_event(s, "paywall_nudge", user.id, enr.run_id)

            # абонемент без автопродления заканчивается: напоминания за 7 дней и за день
            users = list(await s.scalars(
                select(User).where(
                    User.subscription_until.is_not(None), User.subscription_until > now,
                    User.subscription_until < now + timedelta(days=7),
                    User.bot_blocked.is_(False),
                )
            ))
            for user in users:
                if not _due(user, user.morning_time):
                    continue
                left = user.subscription_until - now if user.subscription_until.tzinfo else \
                    user.subscription_until.replace(tzinfo=now.tzinfo) - now
                stage = "1" if left <= timedelta(days=1, hours=12) else "7"
                key = f"sub{stage}:{user.subscription_until.date().isoformat()}"
                if not await allow_once(s, user, "sub", key):
                    continue
                user.sub_reminded_at = now
                outbox.add(OutMsg(user.tg_id, texts.sub_expiring(user.subscription_until),
                                  buttons=[[{"text": "Продлить", "webapp": "pay"}]], kind="sub"))
                await log_event(s, "sub_expiring_sent", user.id, stage=stage)
    except Exception:
        log.exception("sales job failed")
    try:
        from services.payments import expire_stale_orders

        async with session_scope() as s:
            await expire_stale_orders(s)
    except Exception:
        log.exception("stale orders job failed")


async def _summaries(limit: int) -> None:
    """Краткие содержания отрезков — в фоне, дешёвой моделью, по несколько за проход."""
    if not get_llm().available:
        return
    async with session_scope() as s:
        rows = (await s.execute(
            select(Segment.id, Book.title, Book.author, Segment.title, Segment.text)
            .join(Book, Book.id == Segment.book_id)
            .join(Enrollment, Enrollment.book_id == Book.id)
            .where(Segment.summary.is_(None), Segment.text.is_not(None), Enrollment.status.in_(("paid", "active")),
                   Book.deleted_at.is_(None))
            .order_by(Segment.book_id, Segment.day_number).limit(limit)
        )).all()
    for sid, btitle, bauthor, stitle, text in rows:
        try:
            res = await summarize_segment(btitle, bauthor, stitle, text)
        except LLMUnavailable:
            return
        except Exception:
            log.exception("summary failed for segment %s", sid)
            continue
        if not res:
            continue
        async with session_scope() as s:
            seg = await s.get(Segment, sid)
            if seg:
                seg.summary, seg.retell_prompt = res[0], (res[1] or None)


async def _forget_stale_texts() -> None:
    """Тексты пересказов не храним: уточнение без ответа — не дольше двух дней,
    а уже проверенные (в том числе сохранённые старыми версиями) — стираем сразу."""
    from services.retell import forget_texts

    async with session_scope() as s:
        stale = list(await s.scalars(
            select(Retelling).where(
                Retelling.raw_text != "",
                or_(
                    Retelling.verdict.in_(("accepted", "rejected")),
                    and_(Retelling.verdict == "clarify", Retelling.created_at < clock.now() - timedelta(days=2)),
                ),
            ).limit(200)
        ))
        for r in stale:
            await forget_texts(s, r)


async def run_scheduler(bot: Bot, stop: asyncio.Event) -> None:
    interval = max(5, get_settings().tick_seconds)
    if clock.is_fast():
        interval = min(interval, 10)
    log.info("scheduler started, every %ss", interval)
    while not stop.is_set():
        try:
            await tick(bot)
        except Exception:
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
