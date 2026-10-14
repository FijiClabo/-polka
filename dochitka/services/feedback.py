"""Короткие опросы в боте и отзывы: что ценят, что мешает, почему бросают.

Каждый опрос уходит человеку один раз: после 3-го и 7-го сданного дня, после финиша и когда чтение встало.
Ответы — оценка или вариант кнопкой и, по желанию, пара слов. Свободные ответы сразу пересылаются ведущему.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.models import DayResult, Enrollment, Feedback, User
from services.common import Outbox, OutMsg, allow_once, log_event
from settings import get_settings


@dataclass(frozen=True)
class Survey:
    question: str
    options: list[tuple[str, str]]  # (значение, подпись)
    follow_up: str  # вопрос после ответа кнопкой


SURVEYS: dict[str, Survey] = {
    "day3": Survey(
        "Три дня позади. Как тебе? Оцени от 1 до 5.",
        [(str(i), str(i)) for i in range(1, 6)],
        "Спасибо! Что мешает или чего не хватает? Ответь одним сообщением — можно голосом. Я читаю всё.",
    ),
    "pmf": Survey(
        "Неделя с Дочиткой. Представь, что её больше нет. Насколько расстроишься?",
        [("very", "Очень"), ("some", "Немного"), ("no", "Не расстроюсь")],
        "Спасибо! А что в Дочитке для тебя главное? Пара слов — очень поможет.",
    ),
    "finish": Survey(
        "Книга дочитана! Насколько вероятно, что посоветуешь Дочитку другу? 0 — точно нет, 10 — точно да.",
        [(str(i), str(i)) for i in range(0, 11)],
        "Спасибо! Что стоит поменять в первую очередь?",
    ),
    "drop": Survey(
        "Похоже, чтение встало. Что помешало? Это поможет сделать Дочитку удобнее.",
        [("time", "Нет времени"), ("book", "Книга не зашла"), ("retell", "Неудобно пересказывать"),
         ("forget", "Забываю"), ("other", "Другое")],
        "Спасибо. Расскажи подробнее, если хочется, — одним сообщением.",
    ),
}
SKIP_TEXT = "Пропустить"


def survey_buttons(kind: str) -> list[list[dict]]:
    sv = SURVEYS[kind]
    opts = [{"text": label, "callback": f"fb:{kind}:{value}"} for value, label in sv.options]
    if kind == "finish":  # 0–10: два ряда
        return [opts[:6], opts[6:]]
    if len(opts) <= 5 and all(len(o["text"]) <= 2 for o in opts):
        return [opts]
    return [[o] for o in opts]


def survey_message(user: User, kind: str) -> OutMsg:
    return OutMsg(user.tg_id, SURVEYS[kind].question, kind="survey", buttons=survey_buttons(kind))


async def offer(session: AsyncSession, user: User, kind: str, key: str, outbox: Outbox | None) -> bool:
    """Отправить опрос один раз (ключ — на всё время)."""
    if outbox is None or kind not in SURVEYS or user.bot_blocked:
        return False
    if not await allow_once(session, user, "survey", key):
        return False
    outbox.add(survey_message(user, kind))
    await log_event(session, "survey_sent", user.id, kind=kind)
    return True


async def after_accept(session: AsyncSession, user: User, enr: Enrollment, outbox: Outbox | None, *,
                       first_today: bool, finished: bool) -> None:
    """Опросы по ходу чтения: после 3-го и 7-го сданного дня и после финиша."""
    if finished:
        await offer(session, user, "finish", f"survey:finish:{enr.id}", outbox)
        return
    if not first_today:
        return
    done = await session.scalar(
        select(func.count(DayResult.id)).where(DayResult.enrollment_id == enr.id, DayResult.result == "done")
    ) or 0
    if done == 3:
        await offer(session, user, "day3", "survey:day3", outbox)
    elif done == 7:
        await offer(session, user, "pmf", "survey:pmf", outbox)


def _score(kind: str, value: str) -> tuple[int | None, str | None]:
    if kind in ("day3", "finish"):
        try:
            return int(value), None
        except ValueError:
            return None, None
    return None, value[:32]


async def answer(session: AsyncSession, user: User, kind: str, value: str) -> Feedback | None:
    """Ответ кнопкой. Повторное нажатие той же кнопки ничего не меняет."""
    if kind not in SURVEYS or value not in {v for v, _ in SURVEYS[kind].options}:
        return None
    score, choice = _score(kind, value)
    fb = await session.scalar(
        select(Feedback).where(Feedback.user_id == user.id, Feedback.kind == kind).order_by(Feedback.id.desc()).limit(1)
    )
    if fb is None or (fb.score, fb.choice) != (score, choice) or fb.text:
        fb = Feedback(user_id=user.id, kind=kind, score=score, choice=choice)
        session.add(fb)
        await session.flush()
        await log_event(session, "feedback", user.id, kind=kind, score=score, choice=choice)
    return fb


async def add_text(session: AsyncSession, user: User, kind: str, text: str, feedback_id: int | None,
                   outbox: Outbox | None) -> Feedback:
    """Свободный ответ: дописывается к ответу кнопкой или становится отдельным отзывом. Уходит ведущему."""
    text = text.strip()[:2000]
    fb = await session.get(Feedback, feedback_id) if feedback_id else None
    if fb is None or fb.user_id != user.id:
        fb = Feedback(user_id=user.id, kind=kind)
        session.add(fb)
    fb.text = text
    await session.flush()
    await log_event(session, "feedback", user.id, kind=fb.kind, score=fb.score, choice=fb.choice, text=True,
                    length=len(text))
    if outbox is not None:
        from services.users import who

        for admin in get_settings().admin_ids:
            if admin != user.tg_id:
                outbox.add(OutMsg(admin, texts.feedback_for_admin(who(user), fb.kind, fb.score, fb.choice, text),
                                  kind="admin"))
    return fb


def choice_label(kind: str, value: str | None) -> str | None:
    if value is None or kind not in SURVEYS:
        return value
    return dict(SURVEYS[kind].options).get(value, value)
