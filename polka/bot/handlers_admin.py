"""Админ-команды ведущего: забег, оплата, пары, статистика, ручное засчитывание, рассылка, экспорт.

Доступны только tg_id из ADMIN_TG_IDS и ведущему текущего забега.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import func, select

import texts
from bot.common import is_admin, load_user
from bot.ui import deep_link, flush
from core import clock
from db.models import Consent, Enrollment, Event, PromoCode, Purchase, Retelling, Run, Segment, User
from db.session import session_scope
from services import billing
from services.admin import export_zip, find_user, stats, stats_text
from services.common import Outbox, OutMsg, log_event
from services.retell import override
from services.runs import current_main_run, grant, refund
from services.social import auto_pairs, make_pair, public_status
from settings import get_settings

router = Router(name="admin")

STATUS_RU = {"invited": "ждёт оплаты", "paid": "оплачен", "active": "идёт", "finished": "дочитан",
             "dropped": "выбыл", "refunded": "отменён"}
VERDICT_RU = {"accepted": "засчитан", "rejected": "не засчитан", "clarify": "уточнение", "pending": "в очереди"}
TODAY_RU = {"done": "сдано", "reading": "ещё читает", "burned": "стрик сгорел", "idle": "не в забеге",
            "finished": "книга дочитана", "waiting": "ждёт старта"}

ADMIN_HELP = """<b>Команды ведущего</b>
/run_new Название | 2026-10-20 | 990 — создать забег (дату можно словом «сегодня»/«завтра»)
/run_date 2026-10-20 — дата старта текущего забега
/run_info — текущий забег
/grant @user [run|month|year] — выдать доступ вручную (оплата переводом, подарок)
/promo_new КОД 20 [лимит] [чей] — промокод со скидкой (100 — бесплатно)
/promos — промокоды: сколько пришло, оплатило, выручка
/sales — продажи, воронка, источники
/promo_off КОД — выключить промокод
/refund_done ID — отметить, что деньги по заказу вернули в кабинете ЮKassa (доступ закроется)
/refund @user — возврат в групповом забеге
/pair_set @a @b — назначить пару
/pairs_auto — разбить половину оплативших без пары на пары
/user @user — статус и последние вердикты по пересказам (с id, без текстов)
/override &lt;id&gt; accepted — засчитать пересказ вручную
/stats — сводка по забегу
/broadcast текст — сообщение всем, кто дал согласие (/broadcast group текст — только групповому забегу)
/run_close — закрыть набор в групповой забег
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
    link = deep_link("group")
    await message.answer(f"Групповой забег «{texts.e(parts[0])}» создан (id {rid}). Старт: {texts.d(start)}.\n"
                         f"Ссылка для участников: {link or 't.me/<бот>?start=group'}\n"
                         "В группу попадают только по этой ссылке, остальные читают в личных забегах. "
                         "Оплату вручную отмечай /grant, закрыть набор — /run_close.")


@router.message(Command("run_close"))
async def cmd_run_close(message: Message) -> None:
    """Закрыть набор в групповой забег: новые участники по ссылке больше не попадут, идущие продолжают."""
    if not await _guard(message):
        return
    async with session_scope() as s:
        run = await current_main_run(s)
        if run is None:
            await message.answer("Открытого группового забега нет.")
            return
        run.status = "finished"
        await log_event(s, "run_closed", None, run.id)
        title = run.title
    await message.answer(f"Набор в «{texts.e(title)}» закрыт. Участники дочитывают как обычно.")


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
        clock_note = f"\nЧасы бота (тестовый режим): {clock.now():%d.%m %H:%M} UTC"
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


@router.message(Command("grant", "gift"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot) -> None:
    """Выдать доступ вручную: /grant @user [run|month|year]. В групповом забеге — отметить оплату забега."""
    if not await _guard(message):
        return
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Формат: /grant @username [run|month|year] — по умолчанию один забег")
        return
    product = parts[1].lower() if len(parts) > 1 else "run"
    if product not in ("run", "month", "year"):
        await message.answer("Тариф: run (забег), month (месяц) или year (год)")
        return
    outbox = Outbox()
    async with session_scope() as s:
        u = await find_user(s, parts[0])
        if not u:
            await message.answer("Пользователь не найден. Нужен хотя бы один /start в боте с этого аккаунта.")
            return
        cohort = await current_main_run(s)
        cohort_enr = None
        if cohort is not None and product == "run":
            cohort_enr = await s.scalar(select(Enrollment).where(Enrollment.run_id == cohort.id, Enrollment.user_id == u.id))
        if cohort_enr is not None:
            await grant(s, u, cohort)
            start = cohort_enr.plan_start_date or cohort.start_date
        else:
            granted = await billing.grant_manual(s, u, product, outbox)
            start = None
        tg_id, uid, name = u.tg_id, u.id, u.display_name
    if cohort_enr is not None:
        await bot.send_message(tg_id, f"Оплата получена, спасибо! {texts.status_in_list(start)}")
    else:
        from bot.handlers_pay import after_payment_message

        await after_payment_message(bot, tg_id, uid, product, bool(getattr(granted, "activated_now", None)))
    await flush(bot, outbox)
    await message.answer(f"Доступ выдан: {texts.e(name)} — {billing.product_title(product).lower()}.")


@router.message(Command("promo_new"))
async def cmd_promo_new(message: Message, command: CommandObject) -> None:
    """/promo_new КОД СКИДКА% [лимит] [чей] — например: /promo_new KNIGA20 20 100 @blogger"""
    if not await _guard(message):
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[1].rstrip("%").isdigit():
        await message.answer("Формат: /promo_new КОД СКИДКА [лимит] [чей]\nНапример: /promo_new KNIGA20 20 100 @blogger\n"
                             "Скидка 100 — бесплатный доступ. Ссылка для рекламы: t.me/<бот>?start=promo_КОД")
        return
    code = billing.normalize_code(parts[0])
    pct = max(0, min(100, int(parts[1].rstrip("%"))))
    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    owner = next((x for x in parts[2:] if not x.isdigit()), None)
    async with session_scope() as s:
        p = await s.get(PromoCode, code)
        if p is None:
            p = PromoCode(code=code)
            s.add(p)
        p.discount_percent, p.max_uses, p.owner, p.active = pct, limit, owner, True
    from bot.ui import deep_link

    await message.answer(f"Промокод {code}: −{pct}%{f', до {limit} использований' if limit else ''}"
                         f"{f', чей: {texts.e(owner)}' if owner else ''}.\nСсылка: {deep_link('promo_' + code)}")


@router.message(Command("promo_off"))
async def cmd_promo_off(message: Message, command: CommandObject) -> None:
    if not await _guard(message):
        return
    code = billing.normalize_code(command.args)
    async with session_scope() as s:
        p = await s.get(PromoCode, code)
        if p:
            p.active = False
    await message.answer(f"Промокод {code} выключен." if p else "Нет такого промокода.")


@router.message(Command("promos"))
async def cmd_promos(message: Message) -> None:
    if not await _guard(message):
        return
    async with session_scope() as s:
        codes = list(await s.scalars(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(30)))
        lines = ["<b>Промокоды</b> (использований · выручка)"]
        for c in codes:
            rows = list(await s.scalars(select(Purchase).where(Purchase.promo_code == c.code, Purchase.status == "paid")))
            rub = sum(p.amount for p in rows if p.currency == "RUB") / 100
            users = await s.scalar(select(func.count(User.id)).where(User.source == f"promo:{c.code}"))
            lines.append(f"{c.code}{'' if c.active else ' (выключен)'} −{c.discount_percent}% · пришли {users or 0} · оплат {c.used}"
                         f" · {rub:.0f} ₽{f' · {texts.e(c.owner)}' if c.owner else ''}")
    await message.answer("\n".join(lines) if codes else "Промокодов пока нет. Создать: /promo_new КОД 20")


@router.message(Command("sales"))
async def cmd_sales(message: Message, bot: Bot) -> None:
    """Продажи и воронка: старт → онбординг → книга → план → оплата → дочитали."""
    if not await _guard(message):
        return
    async with session_scope() as s:
        day, week, total = (await billing.sales_summary(s, 1), await billing.sales_summary(s, 7),
                            await billing.sales_summary(s))

        async def users_with(event: str) -> int:
            return await s.scalar(select(func.count(func.distinct(Event.user_id))).where(Event.type == event)) or 0

        funnel = [("Нажали /start", await users_with("start")), ("Прошли онбординг", await users_with("onboarding_done")),
                  ("Загрузили или добавили книгу", await users_with("book_parsed") + await users_with("book_paper_added")),
                  ("Выбрали план", await users_with("plan_confirmed")), ("Увидели тарифы", await users_with("paywall_shown")),
                  ("Оплатили", total["payers"]), ("Сдали первый день", await users_with("day_done")),
                  ("Дочитали книгу", await users_with("finish"))]
        sources = (await s.execute(
            select(User.source, func.count(User.id)).group_by(User.source).order_by(func.count(User.id).desc()).limit(8)
        )).all()

    def line(title: str, x: dict) -> str:
        prod = ", ".join(f"{billing.product_title(k).lower()}: {v}" for k, v in x["by_product"].items()) or "—"
        n = x["count"]
        return f"{title}: {n} {texts.plural(n, 'оплата', 'оплаты', 'оплат')} · {x['rub']:.0f} ₽ ({prod})"

    text = ["<b>Продажи</b>", line("Сегодня", day), line("7 дней", week), line("Всего", total),
            f"Возвратов: {total['refunds']}",
            "", "<b>Воронка</b>"]
    text += [f"{t}: {n}" for t, n in funnel]
    text += ["", "<b>Источники</b>"] + [f"{texts.e(src or 'без метки')}: {n}" for src, n in sources]
    await message.answer("\n".join(text))


@router.message(Command("refund_done"))
async def cmd_refund_done(message: Message, command: CommandObject, bot: Bot) -> None:
    """Отметить, что деньги по платежу возвращены вручную (в кабинете ЮKassa): /refund_done ID"""
    if not await _guard(message):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /refund_done номер_заказа (он есть в уведомлении об оплате: «заказ #…»)")
        return
    async with session_scope() as s:
        p = await billing.mark_refunded(s, int(arg))
        tg = (await s.get(User, p.user_id)).tg_id if p and p.user_id else None
    if p is None:
        await message.answer("Платёж не найден.")
        return
    if tg:
        await bot.send_message(tg, "Оплата отменена, доступ по ней закрыт. Вопросы — /paysupport.")
    await message.answer(f"Платёж #{arg} отмечен как возвращённый, доступ закрыт.")


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await _guard(message):
        return
    async with session_scope() as s:
        u = await find_user(s, (command.args or "").strip())
        run = await current_main_run(s)
        enr = await s.scalar(select(Enrollment).where(Enrollment.user_id == u.id, Enrollment.run_id == run.id)) if u and run else None
        if not enr:
            await message.answer("Участие не найдено.")
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
            await message.answer("Забег или пользователи не найдены.")
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
            await message.answer("Пользователь не найден.")
            return
        st = await public_status(s, u)
        enrs = list(await s.scalars(select(Enrollment).where(Enrollment.user_id == u.id)))
        lines = [f"<b>{texts.e(u.display_name)}</b> @{texts.e(u.tg_username or '-')} · tg_id {u.tg_id} · {texts.e(u.timezone)}",
                 f"Книга: {texts.e(st.book_title or '—')} · стрик {st.streak} · сегодня: {TODAY_RU.get(st.today, st.today)}"]
        for e in enrs:
            lines.append(f"Участие #{e.id}: забег {e.run_id}, {STATUS_RU.get(e.status, e.status)}, "
                         f"план {e.plan_days or '—'} дн., старт {e.plan_start_date or '—'}")
        rows = await s.execute(
            select(Retelling, Segment.day_number).join(Segment, Segment.id == Retelling.segment_id, isouter=True)
            .where(Retelling.enrollment_id.in_([e.id for e in enrs] or [-1])).order_by(Retelling.id.desc()).limit(10)
        )
        lines.append("\nПоследние пересказы (тексты не храним):")
        for r, dn in rows.all():
            lines.append(f"#{r.id} · день {dn} · {VERDICT_RU.get(r.verdict, r.verdict)}{' (без сверки)' if not r.verified else ''}")
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
    group_only = text.startswith("group ")
    if group_only:
        text = text[6:].strip()
    async with session_scope() as s:
        if group_only:
            run = await current_main_run(s)
            ids = list(await s.scalars(
                select(User.tg_id).join(Enrollment, Enrollment.user_id == User.id).where(
                    Enrollment.run_id == run.id, Enrollment.status.in_(("invited", "paid", "active", "finished"))
                )
            )) if run else []
        else:
            # всем, кто дал согласие на обработку данных (личные забеги — тоже)
            ids = list(await s.scalars(
                select(User.tg_id).join(Consent, Consent.user_id == User.id)
                .where(Consent.doc_type == "pd", Consent.revoked_at.is_(None)).distinct()
            ))
    outbox = Outbox()
    for tg in ids:
        outbox.add(OutMsg(tg, texts.e(text)))
    await message.answer(f"Отправляю сообщение, получателей: {len(ids)}…")
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
    await message.answer_document(BufferedInputFile(data, f"dochitka_run{run.id}_{date.today()}.zip"),
                                  caption="Участники, пересказы (без текстов), дни, события — CSV и сводка metrics.txt")


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
