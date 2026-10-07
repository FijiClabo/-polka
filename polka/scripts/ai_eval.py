"""Прогон набора пересказов через каждую модель отдельно (раздел 7.5 ТЗ).

    python -m scripts.ai_eval                 # все настроенные провайдеры
    python -m scripts.ai_eval --provider yandex

Цель: честные пересказы отклонены (rejected) менее чем в 5% случаев.
Результаты сохраняются в tests/ai_eval/results/<дата>_<провайдер>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from ai.checker import CheckInput, check_retelling
from ai.llm import AnthropicProvider, LLMChain, LLMUnavailable, YandexGPTProvider, set_llm
from settings import get_settings

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "tests" / "ai_eval" / "dataset.json"
RESULTS = ROOT / "tests" / "ai_eval" / "results"


def build_input(ds: dict, case: dict) -> CheckInput:
    seg_key = case["segment"]
    if seg_key.startswith("paper:"):
        title, author, seg_title, pages = seg_key[6:].split("|")
        return CheckInput(title=title, author=author, segment_title=seg_title, day_number=12, pages=pages,
                          segment_text=None, retelling=case["retelling"])
    s = ds["segments"][seg_key]
    return CheckInput(title=s["title"], author=s["author"], segment_title=s["segment_title"],
                      day_number=s["day_number"], pages=s["pages"], segment_text=s["text"],
                      retelling=case["retelling"])


async def run_provider(name: str, provider, ds: dict) -> dict:
    set_llm(LLMChain([provider]))
    rows = []
    for case in ds["cases"]:
        t = time.monotonic()
        try:
            v = await check_retelling(build_input(ds, case))
            verdict, reply, question = v.verdict, v.reply, v.question
            if v.fallback:
                verdict = "error"
        except LLMUnavailable as e:
            verdict, reply, question = "error", str(e)[:200], None
        dt = time.monotonic() - t
        ok = verdict in case["ok"]
        rows.append({"id": case["id"], "type": case["type"], "expected": case["expected"], "got": verdict,
                     "ok": ok, "sec": round(dt, 1), "reply": reply, "question": question})
        mark = "✓" if ok else "✗"
        print(f"  {mark} {case['id']:4} {case['type']:16} expected={case['expected']:9} got={verdict:9} {dt:4.1f}s  {reply[:70]}")
    honest = [r for r in rows if r["type"].startswith("honest")]
    false_rej = sum(1 for r in honest if r["got"] == "rejected")
    fake = [r for r in rows if r["type"] in ("fake", "offtopic")]
    caught = sum(1 for r in fake if r["got"] in ("clarify", "rejected"))
    summary = {
        "provider": name,
        "model": getattr(provider, "model", ""),
        "total": len(rows),
        "matched": sum(r["ok"] for r in rows),
        "honest_total": len(honest),
        "honest_false_rejections": false_rej,
        "honest_false_rejection_rate": round(false_rej / max(len(honest), 1), 3),
        "honest_accepted_first_try": sum(1 for r in honest if r["got"] == "accepted"),
        "fake_total": len(fake),
        "fake_caught": caught,
        "errors": sum(1 for r in rows if r["got"] == "error"),
        "avg_sec": round(sum(r["sec"] for r in rows) / max(len(rows), 1), 1),
        "verdicts": dict(Counter(r["got"] for r in rows)),
    }
    return {"summary": summary, "rows": rows}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["anthropic", "yandex"], default=None)
    args = ap.parse_args()
    s = get_settings()
    ds = json.loads(DATASET.read_text(encoding="utf-8"))
    providers = []
    if args.provider in (None, "anthropic") and s.anthropic_api_key:
        providers.append(("anthropic", AnthropicProvider(s)))
    if args.provider in (None, "yandex") and s.yandex_api_key and s.yandex_folder_id:
        providers.append(("yandex", YandexGPTProvider(s)))
    if not providers:
        print("Нет ключей ИИ. Заполните ANTHROPIC_API_KEY и/или YANDEX_API_KEY + YANDEX_FOLDER_ID в .env")
        return
    RESULTS.mkdir(parents=True, exist_ok=True)
    for name, p in providers:
        print(f"\n=== {name} ({getattr(p, 'model', '')}) ===")
        res = await run_provider(name, p, ds)
        sm = res["summary"]
        print(
            f"\nСовпало с ожиданием: {sm['matched']}/{sm['total']}. "
            f"Честные отклонены: {sm['honest_false_rejections']}/{sm['honest_total']} "
            f"({sm['honest_false_rejection_rate'] * 100:.0f}%, цель < 5%). "
            f"Выдуманные пойманы: {sm['fake_caught']}/{sm['fake_total']}. Ошибок: {sm['errors']}. "
            f"Среднее время: {sm['avg_sec']} с."
        )
        out = RESULTS / f"{datetime.now():%Y-%m-%d_%H%M}_{name}.json"
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Сохранено: {out}")


if __name__ == "__main__":
    asyncio.run(main())
