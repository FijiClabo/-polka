"""Разбор ответа ИИ и применение правил вердикта поверх него."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from core.rules import MAX_CLARIFY

VERDICTS = ("accepted", "clarify", "rejected")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class VerdictParseError(ValueError):
    pass


@dataclass
class Verdict:
    verdict: str
    confidence: float
    reply: str
    question: str | None
    note_for_summary: str
    verified: bool = True
    provider: str | None = None
    fallback: bool = False  # засчитано автоматически из-за сбоя ИИ


def _extract_json(raw: str) -> dict:
    s = (raw or "").strip()
    s = _FENCE.sub("", s).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise VerdictParseError("no json object") from None
        try:
            obj = json.loads(s[start : end + 1])
        except json.JSONDecodeError as e:
            raise VerdictParseError(str(e)) from e
    if not isinstance(obj, dict):
        raise VerdictParseError("json is not an object")
    return obj


def _clean(s: object, limit: int) -> str:
    if not isinstance(s, str):
        return ""
    s = re.sub(r"!{2,}", "!", s.strip())
    return s[:limit]


def parse_verdict(raw: str) -> Verdict:
    obj = _extract_json(raw)
    v = str(obj.get("verdict", "")).strip().lower()
    if v not in VERDICTS:
        raise VerdictParseError(f"bad verdict {v!r}")
    try:
        conf = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    q = obj.get("question")
    question = _clean(q, 400) if isinstance(q, str) and q.strip() and q.strip().lower() != "null" else None
    reply = _clean(obj.get("reply"), 700)
    if not reply:
        raise VerdictParseError("empty reply")
    return Verdict(
        verdict=v,
        confidence=max(0.0, min(1.0, conf)),
        reply=reply,
        question=question,
        note_for_summary=_clean(obj.get("note_for_summary"), 600),
    )


GENERIC_QUESTION = "Что в этом отрезке запомнилось больше всего — какой момент или мысль?"


def apply_rules(v: Verdict, *, has_text: bool, clarify_count: int) -> Verdict:
    """Жёсткие правила поверх ответа модели (раздел 7.3 ТЗ).

    - без текста отрезка (бумажная книга) — никогда не rejected, максимум 2 уточнения,
      дальше accepted с пометкой «без сверки»;
    - не больше MAX_CLARIFY уточнений на сдачу: третья реплика — accepted, если не явный rejected.
    """
    if v.verdict == "rejected" and not has_text:
        v.verdict = "clarify" if clarify_count < MAX_CLARIFY else "accepted"
    if v.verdict == "clarify" and clarify_count >= MAX_CLARIFY:
        v.verdict = "accepted"
        if v.question and v.question in v.reply:
            v.reply = v.reply.replace(v.question, "").strip() or "Засчитываю. Спасибо, что рассказал."
        v.question = None
    if v.verdict == "clarify" and not v.question:
        v.question = GENERIC_QUESTION
    if v.verdict == "rejected":
        v.question = None
        v.note_for_summary = ""
    v.verified = has_text
    return v


def fallback_accept(reason: str = "ai_error") -> Verdict:
    """Пользователь не должен страдать из-за сбоя ИИ."""
    return Verdict(
        verdict="accepted",
        confidence=0.0,
        reply="Сегодня проверял без сверки с текстом — техника капризничала, но твой день на месте.",
        question=None,
        note_for_summary="",
        verified=False,
        provider=None,
        fallback=True,
    )
