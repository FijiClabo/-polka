"""Админ-команды ведущего: забег, оплата, пары, статистика, ручное засчитывание, рассылка, экспорт.

Доступны только tg_id из ADMIN_TG_IDS и ведущему текущего забега.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import select

import texts
from bot.common import is_admin, load_user
from bot.ui import flush
from core import clock
from db.models import Enrollment, Retelling, Run, Segment, User
from db.session import session_scope
from services.admin import export_zip, find_user, stats, stats_text
from services.common import Outbox, OutMsg, log_event
from services.retell import override
from services.runs import current_main_run, grant, refund
from services.social import auto_pairs, make_pair, public_status
from settings import get_settings

router = Router(name="admin")

ADMIN_HELP = """<b>Команды ведущего</b>
/run_new Название | 2026-10-20 | 990 — создать забег (дату можно словом «сегодня»/«завтра»)
/run_date 2026-10-20 — дата старта текущего забега
/run_info — текущий забег
/grant @user — отметить оплату и выдать доступ
/refund @user — возврат, рассылки отключаются
/pair_set @a @b — назначить пару
/pairs_auto — разбить половину оплативших без пары на пары
/user @user — статус и последние пересказы (с id)
/override &lt;id&gt; accepted — засчитать пересказ вручную
/stats — сводка по забегу
/broadcast текст — сообщение всем участникам
/export — CSV для анализа теста"""


async def _is_host(message: Message) -> bool:
    if is_admin(message.from_user.id):
        return True
    async with session_scope() as s:
        run = await current_main_run(s)
        if run and run.host_user_id:
            host = await s.get(User, run.host_user_id)
            return bool(host and host.tg_id == message.from_user.id)
    return False


async def _guard(message: Message) -> bool:
    if not await _is_host(message):
        await message.answer("Эта команда только для ведущего.")
        return False
    return True


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if await _guard(message):
        await message.answer(ADMIN_HELP)


@router.message(Command("run_new"))
async def cmd_run_new(message: Message, command: CommandObject) -> None:
    if not await _guard(message):
        return
    parts = [p.strip() for p in (command.args or "").split("|")]
    if not parts or not parts[0]:
        await message.answer("Формат: /run_new Название | 2026-10-20 | 990")
        return
    try:
        raw_date = parts[1].lower() if len(parts) > 1 else ""
        if raw_date in ("сегодня", "today"):
            start = clock.now().date()
        elif raw_date in ("завтра", "tomorrow"):
            start = clock.now().date() + timedelta(days=1)
        else:
            start = date.fromisoformat(parts[1]) if raw_date else None
        price = int(parts[2]) if len(parts) > 2 and parts[2] else 990
    except ValueError:
        await message.answer("Дата в формате ГГГГ-ММ-ДД, цена — числом.")
        return
    async with session_scope() as s:
        host, _ = await load_user(s, message.from_user)
        old = list(await s.scalars(select(Run).where(Run.kind == "main", Run.status == "open")))
        for r in old:
            if r.start_date and r.start_date < clock.now().date():
                r.status = "active"
        run = Run(title=parts[0][:200], kind="main", start_date=start, price_rub=price, status="open",
                  host_user_id=host.id, grace_days=3)
        s.add(run)
        await s.flush()
        await log_event(s, "run_created", host.id, run.id)
        rid = run.id
    await message.answer(f"Забег «{texts.e(parts[0])}» создан (id {rid}). Старт: {texts.d(start)}. "
                         "Новые участники записываются в него автоматически, оплату отмечай /grant.")


@router.message(Command("run_date"))
async def cmd_run_date(message: Message, command: CommandObject) -> None:
    if not await _guard(message):
        return
    try:
        start = date.fromisoformat((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: /run_date 2026-10-20")
        return
    async with session_scope() as s:
        run = await current_main_run(s)
        if run is None:
            await message.answer("Нет открытого забега. Создай: /run_new")
            return
        run.start_date = start
        enrs = list(await s.scalars(select(Enrollment).where(Enrollment.run_id == run.id)))
        moved = 0
        for e in enrs:
            if e.status in ("paid", "active") and e.plan_confirmed_at and (e.plan_start_date is None or e.last_closed_day is None):
                e.plan_start_date = start
                moved += 1
    await message.answer(f"Старт забега: {texts.d(start)}. Обновлены планы: {moved}.")


@router.message(Command("run_info"))
async def cmd_run_info(message: Message) -> None:
    if not await _guard(message):
        return
    async with session_scope() as s:
        run = await current_main_run(s)
        if run is None:
            await message.answer("Открытого забега нет. /run_new Название | 2026-10-20 | 990")
            return
        st = await stats(s, run)
    clock_note = ""
    if clock.is_fast() or clock.offset_seconds():
        clock_note = f"\n🕐 Часы бота (тестовый режим): {clock.now():%d.%m %H:%M} UTC"
    await message.answer(f"Забег #{run.id}, старт {texts.d(run.start_date)}, цена {run.price_rub} ₽{clock_note}\n\n{stats_text(st)}")


async def _target(message: Message, ref: str) -> tuple[User | None, Run | None]:
    async with session_scope() as s:
        u = await find_user(s, ref)
        run = await current_main_run(s)
        if u:
            s.expunge(u)
        if run:
            s.expunge(run)
    return u, run


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await _guard(message):
        return
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Формат: /grant @username или /grant 123456789 (tg_id)")
        return
    async with session_scope() as s:
        u = await find_user(s, ref)
        run = await current_main_run(s)
        if not u:
            await message.answer("Не нашёл пользователя. Он должен хотя бы раз нажать /start в боте.")
            return
        if not run:
            await message.answer("Нет открытого забега.")
            return
        enr = await grant(s, u, run)
        start = enr.plan_start_date or run.start_date
        tg_id, name = u.tg_id, u.display_name
        has_plan = bool(enr.plan_days)
    note = "" if has_plan else "\n\nОсталось добавить книгу — пришли файл epub/fb2 или выбери бумажную."
    await bot.send_message(tg_id, f"Оплата получена, спасибо! {texts.status_in_list(start)}{note}")
    await message.answer(f"Доступ выдан: {texts.e(name)}.")


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await _guard(message):
        return
    async with session_scope() as s:
        u = await find_user(s, (command.args or "").strip())
        run = await current_main_run(s)
        enr = await s.scalar(select(Enrollment).where(Enrollment.user_id == u.id, Enrollment.run_id == run.id)) if u and run else None
        if not enr:
            await message.answer("Не нашёл участие.")
            return
        await refund(s, enr)
        tg_id, name = u.tg_id, u.display_name
    await bot.send_message(tg_id, texts.state_message("refunded"))
    await message.answer(f"Возврат оформлен: {texts.e(name)}.")


@router.message(Command("pair_set"))
async def cmd_pair_set(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await _guard(message):
        return
    refs = (command.args or "").split()
    if len(refs) != 2:
        await message.answer("Формат: /pair_set @a @b")
        return
    outbox = Outbox()
    async with session_scope() as s:
        run = await current_main_run(s)
        ua, ub = await find_user(s, refs[0]), await find_user(s, refs[1])
        if not (run and ua and ub):
            await message.answer("Не нашёл забег или пользователей.")
            return
        ea = await s.scalar(select(Enrollment).where(Enrollment.user_id == ua.id, Enrollment.run_id == run.id))
        eb = await s.scalar(select(Enrollment).where(Enrollment.user_id == ub.id, Enrollment.run_id == run.id))
        if not (ea and eb):
            await message.answer("Оба должны быть в текущем забеге.")
            return
        pair = await make_pair(s, run, ea, eb)
        if not pair:
            await message.answer("Не получилось: у кого-то уже есть напарник.")
            return
        outbox.add(OutMsg(ua.tg_id, texts.pair_joined(ub.display_name)))
        outbox.add(OutMsg(ub.tg_id, texts.pair_joined(ua.display_name)))
    await flush(bot, outbox)
    await message.answer("Пара создана.")


@router.message(Command("pairs_auto"))
async def cmd_pairs_auto(message: Message, bot: Bot) -> None:
    if not await _guard(message):
        return
    outbox = Outbox()
    async with session_scope() as s:
        run = await current_main_run(s)
        if not run:
            await message.answer("Нет открытого забега.")
            return
        pairs = await auto_pairs(s, run)
        for p in pairs:
            a, b = await s.get(User, p.user_a_id), await s.get(User, p.user_b_id)
            outbox.add(OutMsg(a.tg_id, texts.pair_joined(b.display_name)))
            outbox.add(OutMsg(b.tg_id, texts.pair_joined(a.display_name)))
    await flush(bot, outbox)
    await message.answer(f"Создано пар: {len(pairs)}. Остальные участники без пары — контрольная группа.")


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject) -> None:
    if not await _guard(message):
        return
    async with session_scope() as s:
        u = await find_user(s, (command.args or "").strip())
        if not u:
            await message.answer("Не нашёл пользователя.")
            return
        st = await public_status(s, u)
        enrs = list(await s.scalars(select(Enrollment).where(Enrollment.user_id == u.id)))
        lines = [f"<b>{texts.e(u.display_name)}</b> @{texts.e(u.tg_username or '-')} · tg_id {u.tg_id} · {texts.e(u.timezone)}",
                 f"Книга: {texts.e(st.book_title or '—')} · стрик {st.streak} · сегодня: {st.today}"]
        for e in enrs:
            lines.append(f"Участие #{e.id}: забег {e.run_id}, {e.status}, план {e.plan_days or '—'} дн., старт {e.plan_start_date or '—'}")
        rows = await s.execute(
            select(Retelling, Segment.day_number).join(Segment, Segment.id == Retelling.segment_id, isouter=True)
            .where(Retelling.enrollment_id.in_([e.id for e in enrs] or [-1])).order_by(Retelling.id.desc()).limit(10)
        )
        lines.append("\nПоследние пересказы:")
        for r, dn in rows.all():
            lines.append(f"#{r.id} · день {dn} · {r.verdict}{' (без сверки)' if not r.verified else ''} · "
                         f"{texts.e((r.raw_text or '')[:80])}")
    await message.answer("\n".join(lines))


@router.message(Command("override"))
async def cmd_override(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await _guard(message):
        return
    parts = (command.args or "").split()
    if not parts or not parts[0].isdigit():
        await message.answer("Формат: /override 123 accepted")
        return
    outbox = Outbox()
    async with session_scope() as s:
        res = await override(s, int(parts[0]), outbox)
    await flush(bot, outbox)
    await message.answer("Засчитано, стрик пересчитан." if res else "Пересказ не найден.")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not await _guard(message):
        return
    async with session_scope() as s:
        run = await current_main_run(s)
        if not run:
            await message.answer("Нет открытого забега.")
            return
        st = await stats(s, run)
    await message.answer(stats_text(st))


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await _guard(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /broadcast текст сообщения")
        return
    async with session_scope() as s:
        run = await current_main_run(s)
        ids = list(await s.scalars(
            select(User.tg_id).join(Enrollment, Enrollment.user_id == User.id).where(
                Enrollment.run_id == run.id, Enrollment.status.in_(("invited", "paid", "active", "finished"))
            )
        )) if run else []
    outbox = Outbox()
    for tg in ids:
        outbox.add(OutMsg(tg, texts.e(text)))
    await message.answer(f"Отправляю {len(ids)} участникам…")
    await flush(bot, outbox)
    await message.answer("Готово.")


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not await _guard(message):
        return
    async with session_scope() as s:
        run = await current_main_run(s)
        if not run:
            run = await s.scalar(select(Run).where(Run.kind == "main").order_by(Run.id.desc()).limit(1))
        if not run:
            await message.answer("Забегов ещё не было.")
            return
        data = await export_zip(s, run)
    await message.answer_document(BufferedInputFile(data, f"polka_run{run.id}_{date.today()}.zip"),
                                  caption="participants, retellings, days, events (CSV) + metrics.txt")


@router.message(Command("timewarp"))
async def cmd_timewarp(message: Message, command: CommandObject) -> None:
    """Только для тестового прогона (ALLOW_TIMEWARP=true): сдвинуть часы бота вперёд на N часов."""
    if not await _guard(message):
        return
    if not getattr(get_settings(), "allow_timewarp", False):
        await message.answer("Выключено. Включается переменной ALLOW_TIMEWARP=true (только для тестов).")
        return
    try:
        hours = float((command.args or "0").replace(",", "."))
    except ValueError:
        hours = 0
    clock.shift(hours * 3600)
    from services.common import save_clock_offset

    await save_clock_offset()
    await asyncio.sleep(0)
    await message.answer(f"Часы сдвинуты. Сейчас для бота: {clock.now():%Y-%m-%d %H:%M} UTC")
