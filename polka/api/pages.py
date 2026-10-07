"""Публичные страницы: лендинг, оферта, политика обработки данных.

Шаблоны лежат в web/*.html, значения подставляются из настроек — цены, продавец, ссылка на бота —
так что после смены цены или реквизитов в .env страницы обновляются сами.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

import texts
from db.session import session_scope
from settings import get_settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
router = APIRouter()

_SRC = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Z_]+)\s*\}\}")
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
           "декабря")


def _bot_link(src: str | None) -> str:
    from bot.ui import bot_username

    name = bot_username() or get_settings().bot_username
    if not name:
        return "#"
    tag = src if src and _SRC.match(src) else "landing"
    return f"https://t.me/{name}?start=src_{tag}"


def page_values(src: str | None = None) -> dict[str, str]:
    from bot.ui import bot_username

    s = get_settings()
    bot = bot_username() or s.bot_username
    seller = ", ".join(x for x in (
        s.seller_name, s.seller_status if s.seller_name else "", f"ИНН {s.seller_inn}" if s.seller_inn else "",
        f"ОГРН {s.seller_ogrn}" if s.seller_ogrn else "",
    ) if x)
    support = s.seller_email or (f"@{bot}" if bot else "")
    d = s.legal_docs_date
    domain = s.public_url.removeprefix("https://").removeprefix("http://") or "—"
    return {
        "PROJECT": s.project_name,
        "BOT_URL": _bot_link(src),
        "BOT_USERNAME": f"@{bot}" if bot else "—",
        "BOT_NAME": bot or "",
        "BOT_LINK": f"https://t.me/{bot}" if bot else "#",
        "PRICE_RUN": texts.rub(s.price_run_rub),
        "PRICE_MONTH": texts.rub(s.price_month_rub),
        "PRICE_YEAR": texts.rub(s.price_year_rub),
        "PRICE_RUN_DAY": texts.rub(s.price_run_rub / 30),
        "CONSENT_URL": "/consent",
        "OFFER_VERSION": s.offer_version,
        "OFFER_URL": "/offer",
        "PRIVACY_URL": "/privacy",
        "SUPPORT": support or "—",
        "SELLER_LINE": seller or s.project_name,
        "SELLER_NAME": s.seller_name or "—",
        "SELLER_STATUS": s.seller_status,
        "SELLER_INN": s.seller_inn or "—",
        "SELLER_OGRN": s.seller_ogrn,
        "SELLER_OGRN_LINE": f", ОГРН {s.seller_ogrn}" if s.seller_ogrn else "",
        "SELLER_EMAIL": s.seller_email or "—",
        "SELLER_PHONE": s.seller_phone,
        "DOMAIN": domain,
        "DATE": f"{d.day} {_MONTHS[d.month - 1]} {d.year} г.",
    }


_DATA_IF = re.compile(r'<(?P<tag>[a-z0-9]+)(?P<attrs>[^>]*?)\sdata-if="(?P<key>[A-Z_]+)"(?P<rest>[^>]*)>(?P<body>.*?)</(?P=tag)>',
                      re.S)

_DOC_STYLE = """
:root{--bg:#F3EEE6;--card:#FBF8F3;--text:#2A2623;--muted:#786F67;--accent:#B5573F;--line:rgba(42,38,35,.12);color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:400 16px/1.7 "Onest",system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.doc{max-width:760px;margin:0 auto;padding:24px 20px 72px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:20px;font-size:15px}
.top a{color:var(--muted);text-decoration:none}
a{color:var(--accent);text-underline-offset:3px}
h1,h2,h3{font-family:"Cormorant Garamond","Cormorant",Georgia,serif;font-weight:600;letter-spacing:-.01em}
h1{font-size:clamp(30px,7vw,42px);line-height:1.1;margin:8px 0 10px}
h2{font-size:25px;line-height:1.25;margin:40px 0 10px}
h3{font-size:20px;margin:24px 0 8px}
.meta{color:var(--muted);font-size:14px}
.short,.toc{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px 20px;margin:18px 0}
.short p:first-child{margin-top:0}
.toc ol,.toc ul{margin:6px 0;padding-left:22px}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:15px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top}
th{background:var(--card);font-weight:500}
li{margin:4px 0}
hr{border:0;border-top:1px solid var(--line);margin:32px 0}
@media print{.top{display:none}body{background:#fff}}
"""


def _apply_data_if(body: str, values: dict[str, str]) -> str:
    """Элемент с data-if="KEY" остаётся, только если значение KEY не пустое."""
    def repl(m: re.Match) -> str:
        if not values.get(m.group("key"), "").strip():
            return ""
        tag = m.group("tag")
        return f"<{tag}{m.group('attrs')}{m.group('rest')}>{m.group('body')}</{tag}>"

    return _DATA_IF.sub(repl, body)


def render(name: str, src: str | None = None) -> str:
    path = WEB_DIR / name
    if not path.is_file():
        raise HTTPException(404)
    values = page_values(src)
    raw = _apply_data_if(path.read_text(encoding="utf-8"), values)
    body = _PLACEHOLDER.sub(lambda m: html.escape(values.get(m.group(1), "")), raw)
    if "<html" not in body[:500].lower():
        project = html.escape(values["PROJECT"])
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
        title = re.sub(r"<[^>]+>", "", h1.group(1)).strip() if h1 else project
        body = (
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="theme-color" content="#F3EEE6"><title>{title}</title>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600'
            '&family=Onest:wght@400;500&display=swap">'
            f'<style>{_DOC_STYLE}</style></head>'
            f'<body><div class="doc"><div class="top"><a href="/">← {project}</a>'
            '<a href="javascript:print()">Печать / PDF</a></div>'
            f"{body}</div></body></html>"
        )
    return body


def _page(name: str, src: str | None = None) -> HTMLResponse:
    return HTMLResponse(render(name, src), headers={"Cache-Control": "public, max-age=300"})


@router.get("/", include_in_schema=False)
async def landing(request: Request):
    if not (WEB_DIR / "landing.html").is_file():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/app/")
    return _page("landing.html", request.query_params.get("src") or request.query_params.get("utm_source"))


@router.get("/offer", include_in_schema=False)
async def offer():
    return _page("offer.html")


@router.get("/privacy", include_in_schema=False)
async def privacy():
    return _page("privacy.html")


@router.get("/consent", include_in_schema=False)
async def consent():
    return _page("consent.html")


@router.get("/pay/done", include_in_schema=False)
async def pay_done():
    return _page("done.html")


# --------------------------------------------------------------------------- уведомления ЮKassa


@router.get("/pay/status", include_in_schema=False)
async def pay_status(order: str):
    """Для страницы «спасибо»: статус заказа (без личных данных)."""
    from services import payments
    from services.common import Outbox

    if not re.fullmatch(r"[0-9a-f]{32}", order or ""):
        raise HTTPException(404)
    out = Outbox()
    async with session_scope() as s:
        p, just_paid = await payments.refresh_order(s, order, out)
        if p is None:
            raise HTTPException(404)
        status = p.status
    await _after_paid(p if just_paid else None, out)
    return {"status": status}


@router.post("/pay/yookassa", include_in_schema=False)
async def yookassa_webhook(request: Request):
    """Уведомления ЮKassa. Статус платежа всегда перечитывается из API — подделка ничего не даст."""
    from services import payments
    from services.common import Outbox

    if not get_settings().payments_enabled:
        raise HTTPException(404)
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(400) from e
    out = Outbox()
    async with session_scope() as s:
        paid = await payments.handle_notification(s, body, out)
    await _after_paid(paid, out)
    return {"ok": True}


async def _after_paid(p, out) -> None:
    from api.routes import _after_paid_message, _flush

    await _flush(out)
    if p is not None:
        await _after_paid_message(p)


@router.get("/robots.txt", include_in_schema=False)
async def robots():
    return Response("User-agent: *\nDisallow: /api/\nDisallow: /app/\n", media_type="text/plain")
