"""Общие фикстуры для тестов с базой: SQLite во временной папке (или Postgres из TEST_DATABASE_URL)."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import text

from ai.llm import LLMChain, LLMError, LLMResult, set_llm
from core import clock
from core.achievements import ACHIEVEMENTS
from db import session as dbs
from db.models import Achievement, Base, Run
from services.books import add_paper_book, confirm_plan
from services.common import Outbox
from services.runs import enroll, grant
from services.users import get_or_create_user

MSK = timezone(timedelta(hours=3))


async def setup_db(tmp_path) -> None:
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = dbs.init_engine(url)
    async with engine.begin() as conn:
        if url.startswith("postgresql"):
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    async with dbs.session_scope() as s:
        for code, title, desc, order in ACHIEVEMENTS:
            s.add(Achievement(code=code, title=title, description=desc, sort_order=order))


def at(day: date, hh: int = 10, mm: int = 0) -> datetime:
    """Момент по Москве."""
    return datetime.combine(day, time(hh, mm), tzinfo=MSK).astimezone(UTC)


def set_now(day: date, hh: int = 10, mm: int = 0) -> None:
    clock.set_fixed(at(day, hh, mm))


class FakeLLM:
    """Ответ зависит от маркера в тексте пересказа."""

    name = "fake"

    def __init__(self):
        self.down = False
        self.calls = 0

    async def complete(self, system, context, prompt, *, schema, cheap, max_tokens):
        self.calls += 1
        if self.down:
            raise LLMError("down")
        if schema and "summary" in schema.get("properties", {}):
            return LLMResult('{"summary":"Кратко.","retell_prompt":"Что произошло?"}', "fake", "f")
        last = prompt.split("<retelling>")[-1]
        if "ЧУШЬ" in last:
            v = '{"verdict":"rejected","confidence":0.9,"reply":"Не похоже.","question":null,"note_for_summary":""}'
        elif "ОБЩЕЕ" in last:
            v = '{"verdict":"clarify","confidence":0.4,"reply":"А подробнее?","question":"Что сделал герой?","note_for_summary":""}'
        else:
            v = '{"verdict":"accepted","confidence":0.9,"reply":"Засчитано.","question":null,"note_for_summary":"Заметка."}'
        return LLMResult(v, "fake", "f")


def install_fake_llm() -> FakeLLM:
    f = FakeLLM()
    set_llm(LLMChain([f]))
    return f


async def make_participant(s, run: Run, tg_id: int, name: str, *, pages: int = 210, days: int = 21,
                           title: str = "Книга", author: str = "Автор"):
    user, _ = await get_or_create_user(s, tg_id, first_name=name)
    user.timezone = "Europe/Moscow"
    enr = await enroll(s, user, run, "invited")
    await grant(s, user, run)
    await add_paper_book(s, user, enr, title, author, pages)
    await confirm_plan(s, user, enr, days)
    return user, enr


RETELL_OK = "Сегодня я прочитал отрезок, герой уехал из города, встретил старого друга и поговорил с ним о прошлом. ok"
RETELL_GENERAL = "ОБЩЕЕ впечатление: было интересно, герой переживал, мне понравилось, хорошая глава про жизнь"
RETELL_BAD = "ЧУШЬ полная, не читал вообще ничего, просто пишу текст чтобы засчитали этот день"


async def submit(user_id: int, text: str = RETELL_OK):
    from services.flow import submit_retelling

    out = Outbox()
    res = await submit_retelling(user_id, text, outbox=out)
    return res, out
