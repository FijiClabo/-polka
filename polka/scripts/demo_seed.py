"""Демо-данные для просмотра мини-приложения без Telegram и без ключей ИИ.

    python -m scripts.demo_seed            # заполнить базу (DATABASE_URL) демо-забегом
    DEV_AUTH_BYPASS=true python main.py    # API без проверки подписи (только локально!)
    cd webapp && VITE_DEV_TG_ID=1001 npm run dev

Создаёт забег, идущий 12-й день: Саша (tg 1001) с напарником Аней, трое друзей,
история пересказов, заморозка и один пропуск. ИИ подменяется заглушкой.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import delete, select

from ai.llm import LLMChain, LLMResult, set_llm
from books.parse import parse_book
from books.samples import chapters_text, make_epub
from core import clock
from core.achievements import ACHIEVEMENTS
from db.models import Achievement, Book, Retelling, Run, User
from db.session import init_engine, session_scope
from services.books import add_paper_book, attach_book, confirm_plan, save_parsed
from services.common import Outbox
from services.flow import submit_retelling
from services.runs import enroll, grant
from services.social import add_friend_by_code, make_pair
from services.users import get_or_create_user

ROOT = Path(__file__).resolve().parent.parent
DEMO = json.loads((ROOT / "tests" / "ai_eval" / "dataset.json").read_text(encoding="utf-8"))

REPLIES = [
    "Хорошо подмечено про пружину — без неё маяк просто фонарь. Как думаешь, почему он не открыл письмо сразу?",
    "Интересно, что ты зацепился за детали. Похоже, герой меняется быстрее, чем сам это замечает.",
    "Засчитано. Сцена с колоколом вышла живой — и правда, лучший способ остановить обман.",
    "Да, мысль про окружение сильнее мотивации тут главная. Что поменяешь у себя?",
]


class DemoLLM:
    name = "demo"
    n = 0

    async def complete(self, system, context, prompt, *, schema, cheap, max_tokens):
        if schema and "summary" in schema.get("properties", {}):
            return LLMResult('{"summary":"Кратко.","retell_prompt":"Что произошло во время шторма? Расскажи своими словами."}', "demo", "demo")
        DemoLLM.n += 1
        reply = REPLIES[DemoLLM.n % len(REPLIES)]
        return LLMResult(json.dumps({"verdict": "accepted", "confidence": 0.9, "reply": reply,
                                     "question": None},
                                    ensure_ascii=False), "demo", "demo")


RETELLS = [c["retelling"] for c in DEMO["cases"] if c["type"] == "honest_detailed"]


def demo_book_bytes() -> bytes:
    chs = chapters_text(30, 30, seed=3)
    seg = DEMO["segments"]
    titles = ["Письмо", "Остров", "Шторм", "Утро", "Ярмарка", "Фальшивый рубль", "Колокол", "Петля привычки",
              "Две минуты", "Окружение", "Скамейка", "Чёрная магия и её разоблачение", "Гости", "Лето"]
    real = [seg["lighthouse"]["text"], seg["fair"]["text"], seg["habits"]["text"]]
    out = []
    for i, (_t, paras) in enumerate(chs):
        title = f"Глава {i + 1}. {titles[i % len(titles)]}"
        text = real[i % 3].split("\n")
        out.append((title, text + paras[: 30 - len(text)]))
    return make_epub("Северный свет", "Анна Аккуратова", out, toc="nav")


async def main() -> None:
    init_engine()
    set_llm(LLMChain([DemoLLM()]))
    today = (datetime.now(UTC) + timedelta(hours=3) - timedelta(hours=4)).date()
    start = today - timedelta(days=11)
    async with session_scope() as s:
        if not await s.scalar(select(Achievement.code).limit(1)):
            for code, title, desc, order in ACHIEVEMENTS:
                s.add(Achievement(code=code, title=title, description=desc, sort_order=order))
        await s.execute(delete(Run).where(Run.title.like("%(демо)%")))
        for tg in (1001, 1002, 1003, 1004, 1005):
            u = await s.scalar(select(User).where(User.tg_id == tg))
            if u:
                await s.execute(delete(Book).where(Book.owner_user_id == u.id))
                await s.delete(u)
    clock.set_fixed(datetime.combine(start - timedelta(days=2), time(9, 0), tzinfo=UTC))
    async with session_scope() as s:
        run = Run(title="Осенний забег (демо)", kind="main", start_date=start, status="active", grace_days=3, price_rub=990)
        s.add(run)
        await s.flush()
        people = {}
        for tg, name in ((1001, "Саша"), (1002, "Аня"), (1003, "Лёша"), (1004, "Катя"), (1005, "Дима")):
            u, _ = await get_or_create_user(s, tg, first_name=name)
            u.webapp_onboarded = tg != 1001  # у Саши покажем приветственные экраны
            u.timezone = "Europe/Moscow"
            enr = await enroll(s, u, run, "invited")
            await grant(s, u, run)
            people[name] = (u, enr)
        u, enr = people["Саша"]
        book = Book(owner_user_id=u.id, source="epub", title="x", parse_status="pending", spine_color="#C8392B")
        s.add(book)
        await s.flush()
        await save_parsed(s, book, parse_book("demo.epub", demo_book_bytes()), "demo")
        await attach_book(s, enr, book)
        await confirm_plan(s, u, enr, 30)
        for name, title, author, pages, days, color in (
            ("Аня", "Атомные привычки", "Джеймс Клир", 320, 21, "#5B63D6"),
            ("Лёша", "Норвежский лес", "Харуки Мураками", 400, 30, "#3E8E6E"),
            ("Катя", "Сто лет одиночества", "Габриэль Гарсиа Маркес", 480, 45, "#F2C14E"),
            ("Дима", "Преступление и наказание", "Фёдор Достоевский", 600, 45, "#7B8C9C"),
        ):
            pu, pe = people[name]
            b = await add_paper_book(s, pu, pe, title, author, pages)
            b.spine_color = color
            await confirm_plan(s, pu, pe, days)
        await make_pair(s, run, people["Саша"][1], people["Аня"][1])
        sasha = people["Саша"][0]
        for name in ("Аня", "Лёша", "Катя", "Дима"):
            await add_friend_by_code(s, people[name][0], sasha.friend_code, is_new_user=False, outbox=Outbox())
        ids = {k: v[0].id for k, v in people.items()}

    # история: Саша — заморозка на 5-й день; Аня, Лёша — без пропусков; Катя — недавно сорвалась; Дима — стрик сгорел
    skip = {"Саша": {4}, "Аня": {4}, "Лёша": set(), "Катя": {7, 8}, "Дима": {9, 10}}
    for d in range(11):
        day = start + timedelta(days=d)
        for name, uid in ids.items():
            if d in skip[name]:
                continue
            clock.set_fixed(datetime.combine(day, time(6 + (uid % 5), 12), tzinfo=UTC))
            await submit_retelling(uid, RETELLS[(d + uid) % len(RETELLS)], outbox=Outbox())
    # сегодня: Аня и Лёша уже сдали
    for name in ("Аня", "Лёша"):
        clock.set_fixed(datetime.combine(today, time(6, 12), tzinfo=UTC))
        await submit_retelling(ids[name], RETELLS[0], outbox=Outbox())
    # прошлые дочитанные книги — чтобы полка и экран финиша были не пустыми
    async with session_scope() as s:
        past = Run(title="Летний забег (демо)", kind="main", start_date=start - timedelta(days=80), status="finished")
        s.add(past)
        await s.flush()
        for i, (title, author, pages, color, days) in enumerate((
            ("Тонкое искусство пофигизма", "Марк Мэнсон", 240, "#5B63D6", 30),
            ("Маленький принц", "Антуан де Сент-Экзюпери", 120, "#3E8E6E", 21),
        )):
            pr = Run(title=f"Прошлый забег {i + 1} (демо)", kind="main", start_date=start - timedelta(days=80 - i * 35),
                     status="finished") if i else past
            if i:
                s.add(pr)
                await s.flush()
            b = Book(owner_user_id=ids["Саша"], source="paper", title=title, author=author, total_pages=pages,
                     parse_status="ok", spine_color=color)
            s.add(b)
            await s.flush()
            from db.models import Enrollment

            s.add(Enrollment(run_id=pr.id, user_id=ids["Саша"], book_id=b.id, plan_days=days, status="finished",
                             plan_start_date=pr.start_date, best_streak=days - 2, streak=days - 2,
                             finished_at=datetime.combine(pr.start_date + timedelta(days=days - 1), time(18), tzinfo=UTC)))
    # закрыть прошедшие дни у всех (обычно это делает планировщик)
    clock.set_fixed(None)
    from db.models import Enrollment as Enr
    from services.progress import close_pending_days

    async with session_scope() as s:
        for e in list(await s.scalars(select(Enr).where(Enr.status.in_(("paid", "active"))))):
            await close_pending_days(s, e, await s.get(User, e.user_id), Outbox())
    async with session_scope() as s:
        n = len(list(await s.scalars(select(Retelling.id))))
    print(f"Демо готово: старт {start}, сегодня день 12, пересказов: {n}. Вход: tg_id 1001 (Саша).")


if __name__ == "__main__":
    asyncio.run(main())
