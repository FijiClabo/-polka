"""Проверка пересказа, краткие содержания отрезков, вступление к конспекту."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ai import prompts
from ai.llm import LLMUnavailable, get_llm
from ai.verdict import Verdict, VerdictParseError, apply_rules, fallback_accept, parse_verdict
from core.rules import MAX_CLARIFY
from settings import get_settings

log = logging.getLogger(__name__)

FULL_TEXT_LIMIT = 36_000  # ≈ 20 условных страниц
HEAD_TAIL = 5_000
PREV_SUMMARY_LIMIT = 6_000


@dataclass
class CheckInput:
    title: str
    author: str
    segment_title: str
    day_number: int
    pages: str
    segment_text: str | None
    segment_summary: str | None = None
    previous_summaries: list[str] = field(default_factory=list)
    retelling: str = ""
    dialog: list[tuple[str, str]] = field(default_factory=list)  # (user|ai, text)
    clarify_count: int = 0


def _segment_text_for_prompt(text: str | None, summary: str | None) -> str | None:
    if not text:
        return None
    if len(text) <= FULL_TEXT_LIMIT or not summary:
        return text
    return (
        f"[Отрезок длинный, ниже краткое содержание, начало и конец]\n<summary>{summary}</summary>\n"
        f"<beginning>{text[:HEAD_TAIL]}</beginning>\n…\n<ending>{text[-HEAD_TAIL:]}</ending>"
    )


def _previous(summaries: list[str]) -> str | None:
    if not summaries:
        return None
    out: list[str] = []
    total = 0
    for s in reversed(summaries):  # приоритет последним отрезкам
        if total + len(s) > PREV_SUMMARY_LIMIT:
            break
        out.append(s)
        total += len(s)
    return "\n".join(reversed(out)) or None


async def check_retelling(inp: CheckInput) -> Verdict:
    """Вердикт по пересказу.

    LLMUnavailable пробрасывается наружу: вызывающий ставит пересказ в очередь («проверю чуть позже»).
    Невалидный JSON → один повторный запрос → при повторной ошибке accepted без сверки.
    """
    s = get_settings()
    chain = get_llm()
    if not chain.available:
        raise LLMUnavailable("no providers configured")
    has_text = bool(inp.segment_text)
    context = prompts.verdict_context(
        title=inp.title, author=inp.author, segment_title=inp.segment_title, day_number=inp.day_number,
        pages=inp.pages, segment_text=_segment_text_for_prompt(inp.segment_text, inp.segment_summary),
        previous_summary=_previous(inp.previous_summaries),
    )
    prompt = prompts.verdict_prompt(
        retelling=inp.retelling, dialog=inp.dialog, clarify_count=inp.clarify_count, max_clarify=MAX_CLARIFY
    )
    system = prompts.verdict_system(s.project_name)
    last_provider = None
    for attempt in range(2):
        res = await chain.complete(system, context, prompt, schema=prompts.VERDICT_SCHEMA, max_tokens=4000)
        last_provider = res.provider
        try:
            v = parse_verdict(res.text)
        except VerdictParseError as e:
            log.warning("invalid verdict JSON from %s (attempt %s): %s", res.provider, attempt + 1, e)
            continue
        v.provider = res.provider
        return apply_rules(v, has_text=has_text, clarify_count=inp.clarify_count)
    v = fallback_accept()  # v.fallback=True → вызывающий пишет событие ai_error
    v.provider = last_provider
    return v


async def summarize_segment(title: str, author: str, segment_title: str, text: str) -> tuple[str, str] | None:
    chain = get_llm()
    if not chain.available or not text:
        return None
    import json

    res = await chain.complete(
        prompts.SUMMARY_SYSTEM, "", prompts.summary_prompt(title, author, segment_title, text[:60_000]),
        schema=prompts.SUMMARY_SCHEMA, cheap=True, max_tokens=1500,
    )
    raw = res.text.strip().strip("`")
    if raw.startswith("json"):
        raw = raw[4:]
    try:
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start : end + 1])
        summary = str(obj.get("summary", "")).strip()[:3000]
        prompt = str(obj.get("retell_prompt", "")).strip()[:200]
    except (ValueError, AttributeError):
        return None
    if not summary:
        return None
    return summary, prompt


async def conspect_intro(title: str, author: str, notes: list[str]) -> str | None:
    chain = get_llm()
    notes = [n for n in notes if n]
    if not chain.available or len(notes) < 3:
        return None
    import json

    try:
        res = await chain.complete(
            prompts.CONSPECT_SYSTEM, "", prompts.conspect_prompt(title, author, notes),
            schema=prompts.CONSPECT_SCHEMA, cheap=True, max_tokens=1200,
        )
        raw = res.text
        obj = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        return str(obj.get("intro", "")).strip()[:2000] or None
    except (LLMUnavailable, ValueError):
        return None
