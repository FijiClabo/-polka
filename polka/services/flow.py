"""Сценарии из нескольких транзакций (используются ботом, API и планировщиком)."""

from __future__ import annotations

import logging

from ai.verdict import fallback_accept
from db.models import Enrollment, Retelling, User
from db.session import session_scope
from services.common import Outbox, OutMsg, age
from services.retell import (
    Prepared,
    RetellOutcome,
    build_check_input,
    finish_submission,
    pending_to_retry,
    run_check,
    start_submission,
)

log = logging.getLogger(__name__)
MAX_PENDING_ATTEMPTS = 8


async def submit_retelling(
    user_id: int, text: str, *, source: str = "text", via: str = "bot", voice_file_id: str | None = None,
    voice_duration: int | None = None, outbox: Outbox,
) -> RetellOutcome:
    async with session_scope() as s:
        user = await s.get(User, user_id)
        if user is None:
            return RetellOutcome("no_run")
        prep = await start_submission(
            s, user, text, source=source, via=via, voice_file_id=voice_file_id, voice_duration=voice_duration,
            outbox=outbox,
        )
    if isinstance(prep, RetellOutcome):
        return prep
    verdict = await run_check(prep)
    async with session_scope() as s:
        return await finish_submission(s, prep.retelling_id, verdict, outbox)


async def process_pending_queue(outbox: Outbox) -> int:
    """Доделать отложенные проверки. Результат — сообщением пользователю."""
    from datetime import timedelta

    async with session_scope() as s:
        ids = await pending_to_retry(s)
    done = 0
    for rid in ids:
        async with session_scope() as s:
            r = await s.get(Retelling, rid)
            if r is None or r.verdict != "pending":
                continue
            prep = Prepared(rid, await build_check_input(s, r))
            give_up = r.pending_attempts >= MAX_PENDING_ATTEMPTS or age(r.created_at) > timedelta(hours=6)
        if give_up:
            verdict = fallback_accept()
        else:
            verdict = await run_check(prep)
        local = Outbox()
        async with session_scope() as s:
            out = await finish_submission(s, rid, verdict, local)
            if verdict is not None:
                r = await s.get(Retelling, rid)
                enr = await s.get(Enrollment, r.enrollment_id)
                user = await s.get(User, enr.user_id)
                local.messages.insert(0, OutMsg(user.tg_id, verdict_text(out), kind="reply",
                                                buttons=[[{"text": "Открыть приложение", "webapp": "today"}]]))
                done += 1
        outbox.extend(local)
    return done


def verdict_text(out: RetellOutcome) -> str:
    import texts

    if out.status == "accepted":
        return texts.accepted(out.reply, out.question, out.streak, out.streak_grew, verified=out.verified,
                              partner_name=out.partner_name, partner_done=out.partner_done,
                              pair_streak=out.pair_streak, can_more=out.can_submit_more, next_title=out.next_title)
    if out.status == "clarify":
        return texts.clarify(out.reply, out.question)
    if out.status == "rejected":
        return texts.rejected(out.reply)
    if out.status == "queued":
        return texts.QUEUED
    if out.status == "checking":
        return texts.CHECKING
    if out.status == "rate_limited":
        return texts.RATE_LIMITED
    if out.status == "too_short":
        return texts.too_short_hint(out.segment_title)
    return texts.state_message(out.status, next_title=out.next_title)


