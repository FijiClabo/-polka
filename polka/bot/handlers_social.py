"""Пары, друзья, толчки, финиш с карточкой."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select

import texts
from bot.common import load_user
from bot.ui import app_kb, deep_link, flush, kb, share_link
from db.models import Book, Enrollment, Retelling, User
from db.session import session_scope
from services.common import Outbox, log_event
from services.runs import current_enrollment
from services.social import ensure_pair_code, friend_ids, get_pair_for, nudge, nudged_today, public_status

router = Router(name="social")

STATUS_WORD = {
    "done": "сдано", "reading": "ещё читает", "burned": "стрик сгорел", "idle": "не в забеге",
    "finished": "книга дочитана", "waiting": "ждёт старта",
}


@router.message(Command("pair"))
async def cmd_pair(message: Message) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        enr = await current_enrollment(s, user.id)
        if enr is None or enr.status not in ("invited", "paid", "active"):
            await message.answer("Напарник появляется в забеге. Сейчас ты не участвуешь ни в одном.")
            return
        pair, partner, p_enr = await get_pair_for(s, enr)
        if pair and partner:
            st = await public_status(s, partner)
            book = st.book_title or "книга ещё не выбрана"
            text = (f"Твой напарник — <b>{texts.e(partner.display_name)}</b>.\n"
                    f"Читает: {texts.e(book)} · сегодня: {STATUS_WORD.get(st.today, st.today)}\n"
                    f"Общий стрик: <b>{pair.streak}</b>")
            rows = []
            if st.today != "done":
                rows.append([{"text": "Напомнить напарнику", "callback": f"nudge:{partner.id}:p"}])
            rows.append([{"text": "Открыть", "webapp": "friends"}])
            await message.answer(text, reply_markup=kb(rows))
            return
        code = await ensure_pair_code(s, enr)
        name = user.display_name
    link = deep_link(f"p_{code}")
    await message.answer(
        "Позови напарника из своего забега — общий стрик растёт, только если сдали оба. "
        f"Перешли ссылку:\n{link}",
        reply_markup=kb([[{"text": "Отправить приглашение", "url": share_link(link, texts.pair_invite_text(name))}]]),
    )


@router.message(Command("friends"))
async def cmd_friends(message: Message) -> None:
    async with session_scope() as s:
        user, _ = await load_user(s, message.from_user)
        ids = await friend_ids(s, user.id)
        nudged = await nudged_today(s, user)
        rows_text, rows_kb = [], []
        for fid in ids[:30]:
            f = await s.get(User, fid)
            if not f:
                continue
            st = await public_status(s, f)
            book = f" — {texts.e(st.book_title)}" if st.book_title else ""
            rows_text.append(f"• <b>{texts.e(f.display_name)}</b>{book} · стрик {st.streak} · {STATUS_WORD.get(st.today, '')}")
            if st.today in ("reading", "burned") and f.id not in nudged:
                rows_kb.append([{"text": f"Толкнуть: {f.display_name[:20]}", "callback": f"nudge:{f.id}:f"}])
        code, name = user.friend_code, user.display_name
        await log_event(s, "friend_invite_shared", user.id, via="bot")
    link = deep_link(f"f_{code}")
    head = "Твои друзья:\n" + "\n".join(rows_text) if rows_text else "Пока друзей нет — позови кого-нибудь читать вместе."
    rows_kb.append([{"text": "Позвать друга", "url": share_link(link, texts.friend_invite_text(name))}])
    rows_kb.append([{"text": "Открыть в приложении", "webapp": "friends"}])
    await message.answer(f"{head}\n\nТвоя личная ссылка:\n{link}", reply_markup=kb(rows_kb))


@router.callback_query(F.data.startswith("nudge:"))
async def cb_nudge(call: CallbackQuery, bot: Bot) -> None:
    _, uid, kind = call.data.split(":")
    outbox = Outbox()
    async with session_scope() as s:
        user, _ = await load_user(s, call.from_user)
        target = await s.get(User, int(uid))
        res = await nudge(s, user, target, outbox, kind="partner" if kind == "p" else "friend") if target else "not_found"
    msg = {"ok": "Отправлено.", "already": "Сегодня толчок уже был — хватит одного.", "done": "Там день уже сдан.",
           "disabled": "Этот человек отключил толчки."}.get(res, "Не получилось.")
    await call.answer(msg, show_alert=False)
    await flush(bot, outbox)


async def send_finish(bot: Bot, chat_id: int, user_id: int) -> None:
    """Сообщение о финише + карточка для шеринга."""
    from share.cards import render_card

    async with session_scope() as s:
        user = await s.get(User, user_id)
        enr = await s.scalar(
            select(Enrollment).where(Enrollment.user_id == user_id, Enrollment.status == "finished")
            .order_by(Enrollment.finished_at.desc()).limit(1)
        )
        if enr is None:
            return
        book = await s.get(Book, enr.book_id)
        retells = await s.scalar(select(func.count(Retelling.id)).where(Retelling.enrollment_id == enr.id,
                                                                     Retelling.verdict == "accepted"))
        _, partner, _ = await get_pair_for(s, enr)
        text = texts.finished(book.title, enr.plan_days or 0, enr.best_streak, retells or 0,
                              partner.display_name if partner else None)
        png = render_card("finish", user=user, enr=enr, book=book, partner=partner, retells=retells or 0)
        await log_event(s, "share_generated", user.id, type="finish", via="bot")
        from services.billing import subscription_active

        subscribed = subscription_active(user) or user.run_credits > 0
        upsell = texts.after_finish(enr.run.kind, subscription_active(user), user.run_credits)
    await bot.send_message(chat_id, text, reply_markup=app_kb("Итоги и полка", "finish"))
    await bot.send_photo(chat_id, BufferedInputFile(png, "finish.png"),
                         caption="Карточка для сторис — перешли друзьям или сохрани.")
    rows = [[{"text": "Следующая книга", "callback": "next:run"}]]
    if not subscribed:
        rows.append([{"text": "Тарифы", "webapp": "pay"}])
    await bot.send_message(chat_id, upsell, reply_markup=kb(rows))
