"""Отчёт для разбора: что делают люди, где бросают, что говорят.

Обезличенно: вместо имён, username и Telegram ID — короткий код участника (u3f9a1c), одинаковый между отчётами.
Тексты пересказов не хранятся вовсе, поэтому их в отчёте нет. Свободные ответы из опросов — с замазанными
@username, почтой, телефонами и ссылками.

Архив: report.md (читать человеку и Claude), users.csv, events.csv, feedback.csv, README.txt.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import clock
from core.days import local_now
from db.models import Book, DayResult, Enrollment, Event, Feedback, Retelling, User
from services import analytics
from services.analytics import _aware as aware
from services.feedback import SURVEYS, choice_label
from settings import get_settings

# события, из которых складывается путь человека (остальное — в events.csv)
JOURNEY = {
    "start": "старт", "consent_pd": "согласие", "onboarding_done": "знакомство",
    "book_parsed": "книга файлом", "book_paper_added": "книга без файла", "book_parse_failed": "файл не подошёл",
    "together_joined": "взял план друга", "club_joined": "вступил в клуб", "club_created": "создал клуб",
    "plan_confirmed": "план", "paywall_shown": "тарифы", "promo_applied": "промокод", "paid": "доступ",
    "sprint_started": "спринт", "day_done": "сдано", "day_missed": "пропуск", "day_frozen": "заморозка",
    "day_ahead": "день заранее", "read_more": "ещё отрезок", "pace_partial": "прочитано не всё",
    "pace_changed": "темп", "pace_pause": "пауза", "pair_created": "напарник", "friend_added": "друг",
    "friend_nudge_sent": "толкнул друга", "feedback": "отзыв", "finish": "ФИНИШ", "dropped": "выбыл",
    "refund": "возврат", "streak_lost": "стрик сгорел", "survey_sent": None,
}
REPEATABLE = {"day_done", "day_missed", "day_frozen", "day_ahead", "read_more", "friend_nudge_sent", "pace_partial"}
_ID_KEYS = {"inviter_id", "friend_id", "user_id", "partner_id", "target_id"}
_DROP_KEYS = {"retelling_id"}

_MASKS = [
    (re.compile(r"https?://\S+"), "[ссылка]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[почта]"),
    (re.compile(r"@\w{3,}"), "@[ник]"),
    (re.compile(r"\+?\d[\d\s()-]{8,}\d"), "[телефон]"),
]


def mask(text: str | None) -> str:
    out = text or ""
    for rx, repl in _MASKS:
        out = rx.sub(repl, out)
    return out


class Pseudo:
    def __init__(self) -> None:
        s = get_settings()
        self.key = (s.bot_token or s.project_name or "dochitka").encode()

    def __call__(self, uid: int | None) -> str:
        if uid is None:
            return "—"
        return "u" + hmac.new(self.key, str(uid).encode(), hashlib.sha256).hexdigest()[:6]


def _pct(a: int, b: int) -> str:
    return f"{round(a / b * 100)}%" if b else "—"


def _clean_payload(p: dict | None, pid: Pseudo) -> dict:
    out = {}
    for k, v in (p or {}).items():
        if k in _DROP_KEYS:
            continue
        if k in _ID_KEYS and isinstance(v, int):
            v = pid(v)
        out[k] = v
    return out


def _table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return lines


def _journey_line(evs: list[Event]) -> str:
    """«д0 старт → согласие → книга файлом → план 21 д · минуты → д1 сдано ×3 → д5 пропуск ×2 → …»"""
    if not evs:
        return ""
    t0 = evs[0].created_at
    parts: list[str] = []
    last_key, count, last_day = None, 0, None
    for ev in evs:
        label = JOURNEY.get(ev.type)
        if not label:
            continue
        p = ev.payload or {}
        if ev.type == "plan_confirmed":
            label = f"план {p.get('days', '?')} д" + (f" · {p['mode']}" if p.get("mode") and p["mode"] != "days" else "") \
                + (f" {p['minutes']} мин" if p.get("minutes") else "")
        elif ev.type == "pace_changed":
            label = f"темп: {p.get('mode')}" + (f" {p['minutes']} мин" if p.get("minutes") else "")
        elif ev.type == "pace_pause":
            label = f"пауза {p.get('days')} д"
        elif ev.type == "feedback":
            label = f"отзыв {p.get('kind')}" + (f" {p['score']}" if p.get("score") is not None else "") \
                + (f" {p['choice']}" if p.get("choice") else "")
        day = (ev.created_at - t0).days
        key = (ev.type, label)
        if ev.type in REPEATABLE and key == last_key:
            count += 1
            parts[-1] = f"{parts[-1].split(' ×')[0]} ×{count}"
            continue
        last_key, count = key, 1
        prefix = f"д{day} " if day != last_day else ""
        last_day = day
        parts.append(prefix + label)
    return " → ".join(parts)


async def build_report(s: AsyncSession, days: int | None = None) -> tuple[bytes, str]:
    """ZIP с отчётом и короткая подпись для сообщения."""
    now = clock.real_now()
    since = now - timedelta(days=days) if days else None
    pid = Pseudo()

    users = list(await s.scalars(select(User).order_by(User.id)))
    q = select(Event).order_by(Event.id)
    if since:
        q = q.where(Event.created_at >= since)
    events = list(await s.scalars(q))
    enrs = list(await s.scalars(select(Enrollment)))
    feedback = list(await s.scalars(select(Feedback).order_by(Feedback.id)))
    rets = list(await s.scalars(select(Retelling)))
    drs = list(await s.scalars(select(DayResult)))
    books = list(await s.scalars(select(Book)))
    ov = await analytics.overview(s)

    by_user: dict[int, list[Event]] = defaultdict(list)
    for ev in events:
        if ev.user_id:
            by_user[ev.user_id].append(ev)
    types_by_user: dict[int, set[str]] = {u: {e.type for e in evs} for u, evs in by_user.items()}
    enr_user = {e.id: e.user_id for e in enrs}
    users_by_id = {u.id: u for u in users}

    def users_with(t: str) -> int:
        return sum(1 for ts in types_by_user.values() if t in ts)

    md: list[str] = [
        f"# Дочитка: отчёт для разбора — {now.date().isoformat()}",
        "",
        f"Период: {'последние ' + str(days) + ' дн.' if days else 'всё время'}. Людей в базе: {len(users)}, "
        f"активных в периоде: {len(by_user)}. Участники обезличены (u + 6 знаков), тексты пересказов не хранятся.",
        "",
        "## 1. Воронка",
        *_table(["Шаг", "Людей"], [[f["title"], f["n"]] for f in ov["funnel"]]),
        "",
        "## 2. Удержание",
        *_table(["Сколько дней сдали", "Людей"], [[r["title"], r["n"]] for r in ov["retention"]]),
        "",
        f"Вместе (с напарником или в клубе): {ov['retention_split']['together']} чел., в одиночку: "
        f"{ov['retention_split']['solo']} чел.",
        *_table(["Дошли до", "Вместе", "В одиночку"],
                [[r["title"], f"{r['together']}%", f"{r['solo']}%"] for r in ov["retention_split"]["rows"]]),
        "",
    ]

    # --- как читают и пересказывают
    period_rets = [r for r in rets if not since or aware(r.created_at) >= since]
    verdicts = Counter(r.verdict for r in period_rets)
    src = Counter(r.source for r in period_rets)
    via = Counter(r.via for r in period_rets)
    hours: Counter = Counter()
    for r in period_rets:
        u = users_by_id.get(enr_user.get(r.enrollment_id))
        if u:
            h = local_now(r.created_at, u.timezone).hour
            hours[f"{h // 4 * 4:02d}–{h // 4 * 4 + 4:02d}"] += 1
    period_drs = [d for d in drs if not since or aware(d.created_at) >= since]
    day_res = Counter(d.result for d in period_drs)
    attempts = [r.attempt_no for r in period_rets if r.verdict == "accepted"]
    md += [
        "## 3. Как читают и пересказывают",
        f"Пересказов: {len(period_rets)} · голосом {_pct(src.get('voice', 0), len(period_rets))}, "
        f"текстом {_pct(src.get('text', 0), len(period_rets))} · из бота {via.get('bot', 0)}, из приложения "
        f"{via.get('webapp', 0)}.",
        f"Вердикты: засчитано {verdicts.get('accepted', 0)}, уточнение {verdicts.get('clarify', 0)}, "
        f"не засчитано {verdicts.get('rejected', 0)}, ждут {verdicts.get('pending', 0)}. Средняя попытка до зачёта: "
        f"{round(statistics.mean(attempts), 2) if attempts else '—'}.",
        "Время пересказа (местное): " + ", ".join(f"{k} — {v}" for k, v in sorted(hours.items())) if hours else
        "Время пересказа: данных нет.",
        f"Итоги дней: сдано {day_res.get('done', 0)}, заранее {day_res.get('ahead', 0)}, заморозка "
        f"{day_res.get('frozen', 0)}, пропуск {day_res.get('missed', 0)}.",
        "",
    ]

    # --- темп
    plan_modes = Counter((e.payload or {}).get("mode", "days") for e in events if e.type == "plan_confirmed")
    plan_minutes = Counter((e.payload or {}).get("minutes") for e in events if e.type == "plan_confirmed"
                           and (e.payload or {}).get("minutes"))
    plan_days = [(e.payload or {}).get("days") for e in events if e.type == "plan_confirmed" and (e.payload or {}).get("days")]
    partial = [e.payload or {} for e in events if e.type == "pace_partial"]
    md += [
        "## 4. Темп",
        f"Как выбирают срок: {', '.join(f'{k} — {v}' for k, v in plan_modes.most_common()) or 'нет данных'}"
        + (f"; минут в день: {', '.join(f'{k} — {v}' for k, v in sorted(plan_minutes.items()))}" if plan_minutes else "")
        + (f"; медиана срока {statistics.median(plan_days)} дн." if plan_days else "") + ".",
        f"«Ещё отрезок» (прочитано больше): {users_with('read_more')} чел., "
        f"{sum(1 for e in events if e.type == 'read_more')} раз. Дней «заранее»: {day_res.get('ahead', 0)}.",
        f"«Прочитано не всё»: {users_with('pace_partial')} чел., {len(partial)} раз "
        f"(остаток к следующему — {sum(1 for p in partial if p.get('rest') == 'tomorrow')}, "
        f"отдельным днём — {sum(1 for p in partial if p.get('rest') == 'day')}).",
        f"Смена темпа в пути: {users_with('pace_changed')} чел.; пауза: {users_with('pace_pause')} чел.",
        "",
    ]

    # --- книги
    src_books = Counter(b.source for b in books)
    pages = [b.total_pages for b in books if b.total_pages]
    titles = Counter(b.title for b in books if b.source != "shared" and b.title)
    md += [
        "## 5. Книги",
        f"Файлом: {src_books.get('epub', 0) + src_books.get('fb2', 0)}, без файла: {src_books.get('paper', 0)}, "
        f"копий плана друга: {src_books.get('shared', 0)}. Медиана объёма: {statistics.median(pages) if pages else '—'} стр.",
        "Чаще всего читают: " + "; ".join(f"«{t}» ×{n}" for t, n in titles.most_common(15)) if titles else "",
        "",
    ]

    # --- вместе
    social = ["pair_created", "together_joined", "club_created", "club_joined", "friend_added", "friend_request",
              "friend_nudge_sent", "club_nudge", "friend_shelf_viewed", "friend_same_book", "friend_invite_shared"]
    md += ["## 6. Вместе", *_table(["Событие", "Людей", "Раз"],
                                    [[t, users_with(t), sum(1 for e in events if e.type == t)] for t in social]), ""]

    # --- где и чем пользуются
    screens = Counter((e.payload or {}).get("screen") for e in events if e.type in ("screen", "webapp_open"))
    taps = Counter((e.payload or {}).get("target") for e in events if e.type == "tap")
    cmds = Counter((e.payload or {}).get("name") for e in events if e.type == "bot_command")
    btns = Counter((e.payload or {}).get("name") for e in events if e.type == "bot_button")
    webapp_users = sum(1 for ts in types_by_user.values() if ts & {"screen", "webapp_open"})
    md += [
        "## 7. Где и чем пользуются",
        f"Открывали мини-приложение: {webapp_users} из {len(by_user)} активных.",
        "Экраны: " + ", ".join(f"{k} {v}" for k, v in screens.most_common(20) if k),
        "Нажатия в приложении: " + (", ".join(f"{k} {v}" for k, v in taps.most_common(20) if k) or "—"),
        "Команды бота: " + (", ".join(f"/{k} {v}" for k, v in cmds.most_common(20) if k) or "—"),
        "Кнопки бота: " + (", ".join(f"{k} {v}" for k, v in btns.most_common(25) if k) or "—"),
        "",
    ]

    # --- деньги
    tot = ov["sales"]["total"]
    md += [
        "## 8. Деньги",
        f"Оплат: {tot['count']} на {tot['rub']:.0f} ₽, платящих: {tot['payers']}, возвратов: {tot['refunds']}. "
        f"Расход на ИИ за всё время: {ov['ai']['total'].get('rub', 0)} ₽, на активного читателя в неделю: "
        f"{ov['ai'].get('per_reader_week') or '—'} ₽.",
        "Промокоды: " + ("; ".join(f"{p['code']} — пришли {p['came']}, применили {p['used']}, оплат {p['purchases']}"
                                   for p in ov["promos"][:15]) or "—"),
        "",
    ]

    # --- опросы
    fb_period = [f for f in feedback if not since or aware(f.created_at) >= since]
    day3 = [f.score for f in fb_period if f.kind == "day3" and f.score is not None]
    pmf = Counter(f.choice for f in fb_period if f.kind == "pmf" and f.choice)
    nps = [f.score for f in fb_period if f.kind == "finish" and f.score is not None]
    drop = Counter(f.choice for f in fb_period if f.kind == "drop" and f.choice)
    nps_v = round((sum(1 for x in nps if x >= 9) - sum(1 for x in nps if x <= 6)) / len(nps) * 100) if nps else None
    md += [
        "## 9. Что говорят",
        f"Первые 3 дня (1–5): {len(day3)} ответов, среднее {round(statistics.mean(day3), 2) if day3 else '—'}, "
        f"распределение {dict(sorted(Counter(day3).items()))}.",
        f"Неделя, «расстроишься без Дочитки?»: {sum(pmf.values())} ответов — очень {pmf.get('very', 0)} "
        f"({_pct(pmf.get('very', 0), sum(pmf.values()))}), немного {pmf.get('some', 0)}, нет {pmf.get('no', 0)}. "
        "Ориентир: 40% «очень» — продукт нужен.",
        f"После финиша, NPS: {nps_v if nps_v is not None else '—'} ({len(nps)} ответов).",
        "Почему встало чтение: " + (", ".join(f"{choice_label('drop', k)} — {v}" for k, v in drop.most_common()) or "—"),
        "",
        "Свободные ответы:",
    ]
    for f in fb_period:
        if f.text:
            extra = " · ".join(x for x in (
                str(f.score) if f.score is not None else "", choice_label(f.kind, f.choice) or "") if x)
            md.append(f"- {pid(f.user_id)} · {f.kind}{' · ' + extra if extra else ''}: {mask(f.text)}")
    md.append("")

    # --- пути
    recent = sorted(by_user.items(), key=lambda kv: kv[1][-1].created_at, reverse=True)[:80]
    md += ["## 10. Пути участников (последние 80 активных)", ""]
    for uid, evs in recent:
        line = _journey_line(evs)
        if line:
            md.append(f"- **{pid(uid)}**: {line}")
    md += [
        "",
        "## 11. Ориентиры пилота (из PRODUCT.md)",
        "- Держит ли: 7 дней сдали ≥ 50% начавших, 14 — ≥ 30%.",
        "- Помогает ли «вместе»: удержание вместе заметно выше, чем в одиночку.",
        "- Платят ли: следующую книгу берут ≥ 25% сдавших 7+ дней.",
        "- Понятно ли: на опросах и интервью говорят «план до финиша, каждый день спросят».",
        "- Клубы: 5 из 10 ведущих пробуют, 2 из 5 платят. Полки друзей смотрят ≥ 20% активных.",
        "",
    ]

    # --- CSV
    def csv_bytes(header: list[str], rows: list[list]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        return "﻿" + buf.getvalue()

    res_by_user: dict[int, Counter] = defaultdict(Counter)
    for d in drs:
        uid = enr_user.get(d.enrollment_id)
        if uid:
            res_by_user[uid][d.result] += 1
    acc_by_user = Counter(enr_user.get(r.enrollment_id) for r in rets if r.verdict == "accepted")
    fb_by_user: dict[int, dict] = defaultdict(dict)
    for f in feedback:
        if f.user_id:
            fb_by_user[f.user_id][f.kind] = f.score if f.score is not None else f.choice
    last_enr: dict[int, Enrollment] = {}
    for e in sorted(enrs, key=lambda x: x.id):
        last_enr[e.user_id] = e
    book_by_id = {b.id: b for b in books}
    user_rows = []
    for u in users:
        e = last_enr.get(u.id)
        b = book_by_id.get(e.book_id) if e and e.book_id else None
        ts = types_by_user.get(u.id, set())
        plan_ev = next((x for x in reversed(by_user.get(u.id, [])) if x.type == "plan_confirmed"), None)
        user_rows.append([
            pid(u.id), u.created_at.date().isoformat(), (u.source or "").split(":")[0], u.onboarding_step,
            b.source if b else "", (plan_ev.payload or {}).get("mode", "") if plan_ev else "", e.plan_days if e else "",
            e.status if e else "", acc_by_user.get(u.id, 0), res_by_user[u.id].get("done", 0),
            res_by_user[u.id].get("ahead", 0), res_by_user[u.id].get("frozen", 0), res_by_user[u.id].get("missed", 0),
            e.best_streak if e else 0, "yes" if e and e.pair_id else "", "yes" if e and e.club_id else "",
            "yes" if ts & {"screen", "webapp_open"} else "", sum(1 for x in by_user.get(u.id, []) if x.type == "read_more"),
            sum(1 for x in by_user.get(u.id, []) if x.type == "pace_partial"), "yes" if "pace_changed" in ts else "",
            "yes" if "pace_pause" in ts else "", fb_by_user[u.id].get("day3", ""), fb_by_user[u.id].get("pmf", ""),
            fb_by_user[u.id].get("finish", ""), fb_by_user[u.id].get("drop", ""),
            (now - aware(u.last_seen_at)).days if u.last_seen_at else "",
        ])
    users_csv = csv_bytes(
        ["user", "created", "source", "onboarding", "book", "plan_mode", "plan_days", "status", "segments_done",
         "days_done", "days_ahead", "days_frozen", "days_missed", "best_streak", "pair", "club", "used_webapp",
         "read_more", "partial", "pace_changed", "paused", "day3_score", "pmf", "nps", "drop_reason", "days_since_seen"],
        user_rows)
    events_csv = csv_bytes(
        ["user", "created_at", "type", "payload"],
        [[pid(e.user_id), e.created_at.isoformat(timespec="seconds"), e.type,
          json.dumps(_clean_payload(e.payload, pid), ensure_ascii=False)] for e in events])
    feedback_csv = csv_bytes(
        ["user", "created_at", "kind", "score", "choice", "text"],
        [[pid(f.user_id), f.created_at.isoformat(timespec="seconds"), f.kind, f.score if f.score is not None else "",
          choice_label(f.kind, f.choice) or "", mask(f.text)] for f in fb_period])
    readme = (
        "Отчёт «Дочитки» для разбора.\n\n"
        "report.md — сводка: воронка, удержание, темп, книги, «вместе», экраны и кнопки, деньги, опросы, пути.\n"
        "users.csv — по строке на участника, events.csv — все события, feedback.csv — ответы на опросы.\n\n"
        "Участники обезличены: вместо имени и Telegram — код вида u3f9a1c (одинаковый во всех отчётах).\n"
        "Тексты пересказов не хранятся. В свободных ответах замазаны @username, почта, телефоны и ссылки,\n"
        "но перед отправкой всё же просмотри feedback.csv: люди иногда пишут о себе.\n\n"
        "Как разобрать: пришли архив Claude и напиши «Разбери отчёт Дочитки: где бросают, что мешает, что\n"
        "поменять в продукте в первую очередь».\n"
    )
    survey_q = "\n".join(f"- {k}: {sv.question}" for k, sv in SURVEYS.items())
    md += ["## Вопросы опросов", survey_q, ""]

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", readme)
        zf.writestr("report.md", "\n".join(md))
        zf.writestr("users.csv", users_csv)
        zf.writestr("events.csv", events_csv)
        zf.writestr("feedback.csv", feedback_csv)
    caption = (f"Отчёт для разбора: {len(by_user)} активных, {len(events)} событий, "
               f"{sum(1 for f in fb_period if f.text)} отзывов. Без имён и Telegram ID — можно прислать Claude.")
    return zbuf.getvalue(), caption


def report_filename(days: int | None = None) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"dochitka_report_{stamp}{'_' + str(days) + 'd' if days else ''}.zip"
