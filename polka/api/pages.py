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
from pydantic import BaseModel, Field

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


def _buy_link(product: str, src: str | None) -> str:
    tag = f"&src={src}" if src and _SRC.match(src) else ""
    return f"/buy?product={product}{tag}"


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
        "PRICE_RUN": texts.rub(s.price_run_rub),
        "PRICE_MONTH": texts.rub(s.price_month_rub),
        "PRICE_YEAR": texts.rub(s.price_year_rub),
        "PRICE_RUN_DAY": texts.rub(s.price_run_rub / 30),
        "PRICE_RUN_STARS": f"{s.price_run_stars} ⭐",
        "PRICE_MONTH_STARS": f"{s.price_month_stars} ⭐",
        "PRICE_YEAR_STARS": f"{s.price_year_stars} ⭐",
        "CONSENT_URL": "/consent",
        "BUY_URL": "/buy" + (f"?src={src}" if src and _SRC.match(src) else ""),
        "BUY_RUN_URL": _buy_link("run", src),
        "BUY_MONTH_URL": _buy_link("month", src),
        "BUY_YEAR_URL": _buy_link("year", src),
        "OFFER_VERSION": s.offer_version,
        "CODE_DAYS": str(s.code_valid_days),
        "REFUND_DAYS": str(s.refund_days),
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
:root{--bg:#0e0e10;--card:#1a1a1d;--text:#f4f1ec;--muted:#b0aba3;--accent:#ffb547;--line:rgba(244,241,236,.1);color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:400 16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.doc{max-width:780px;margin:0 auto;padding:20px 16px 64px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}
.top a{color:var(--muted);text-decoration:none;font-size:15px}
a{color:var(--accent)}
h1{font-size:clamp(24px,6vw,34px);line-height:1.2;margin:8px 0}
h2{font-size:20px;line-height:1.3;margin:36px 0 10px}
h3{font-size:17px;margin:22px 0 8px}
.meta{color:var(--muted);font-size:14px}
.short{background:var(--card);border-radius:18px;padding:14px 18px;margin:16px 0}
.short p:first-child{margin-top:0}
.toc{background:var(--card);border-radius:18px;padding:12px 18px;margin:16px 0}
.toc ol,.toc ul{margin:6px 0;padding-left:22px}
.table{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:15px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:var(--card);font-weight:600}
li{margin:4px 0}
hr{border:0;border-top:1px solid var(--line);margin:32px 0}
@media print{:root{--bg:#fff;--text:#111;--muted:#444;--card:#f4f4f4;--accent:#0645ad;color-scheme:light}}
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
            f'<meta name="theme-color" content="#0e0e10"><title>{title}</title><style>{_DOC_STYLE}</style></head>'
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


@router.get("/buy", include_in_schema=False)
async def buy(request: Request):
    return _page("buy.html", request.query_params.get("src"))


@router.get("/pay/done", include_in_schema=False)
async def pay_done():
    return _page("done.html")


# --------------------------------------------------------------------------- оплата на сайте (ЮKassa)

_hits: dict[str, list[float]] = {}


def _rate_limited(ip: str, limit: int = 10, window: float = 60.0) -> bool:
    import time

    t = time.monotonic()
    hits = [h for h in _hits.get(ip, []) if t - h < window]
    hits.append(t)
    _hits[ip] = hits
    if len(_hits) > 10000:
        _hits.clear()
    return len(hits) > limit


class OrderBody(BaseModel):
    product: str = Field(pattern=r"^(run|month|year)$")
    email: str = Field(min_length=5, max_length=128)
    promo: str | None = Field(default=None, max_length=32)
    src: str | None = Field(default=None, max_length=32)
    agree_offer: bool = False
    agree_pd: bool = False


@router.get("/pay/prices", include_in_schema=False)
async def pay_prices(promo: str | None = None):
    from services import billing

    async with session_scope() as s:
        prices = await billing.prices_with_promo(s, promo)
    return {
        "enabled": get_settings().site_checkout,
        "prices": {k: {"rub": v.rub, "list_rub": v.list_rub, "discount": v.discount, "promo": v.promo, "free": v.free}
                   for k, v in prices.items()},
    }


@router.post("/pay/create", include_in_schema=False)
async def pay_create(body: OrderBody, request: Request):
    from services.site_orders import OrderError, create_order, create_payment

    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
    if _rate_limited(ip):
        raise HTTPException(429, "Слишком много попыток — подожди минуту.")
    base = get_settings().public_url or str(request.base_url).rstrip("/")
    try:
        async with session_scope() as s:
            order = await create_order(s, product=body.product, email=body.email, promo=body.promo, source=body.src,
                                       agree_offer=body.agree_offer, agree_pd=body.agree_pd)
            url = await create_payment(order, f"{base}/pay/done?order={order.order_id}")
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    return {"url": url, "order": order.order_id}


@router.get("/pay/status", include_in_schema=False)
async def pay_status(order: str):
    from services.common import Outbox
    from services.site_orders import activation_link, refresh_order

    if not re.fullmatch(r"[0-9a-f]{32}", order or ""):
        raise HTTPException(404)
    out = Outbox()
    async with session_scope() as s:
        p, just_paid = await refresh_order(s, order, out)
        if p is None:
            raise HTTPException(404)
        data = {"status": p.status, "product": p.product}
        if p.status in ("paid", "refund_requested") and p.activation_code:
            data |= {"code": p.activation_code, "link": activation_link(p.activation_code),
                     "activated": p.user_id is not None}
    await _after_paid(p if just_paid else None, out)
    return data


@router.post("/pay/yookassa", include_in_schema=False)
async def yookassa_webhook(request: Request):
    """Уведомления ЮKassa. Статус платежа всегда перечитывается из API — подделка ничего не даст."""
    from services.common import Outbox
    from services.site_orders import handle_notification

    if not get_settings().site_checkout:
        raise HTTPException(404)
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(400) from e
    out = Outbox()
    async with session_scope() as s:
        paid = await handle_notification(s, body, out)
    await _after_paid(paid, out)
    return {"ok": True}


async def _after_paid(p, out) -> None:
    from api.routes import _flush
    from services.site_orders import email_code

    if p is not None:
        await email_code(p)
    await _flush(out)


@router.get("/robots.txt", include_in_schema=False)
async def robots():
    return Response("User-agent: *\nDisallow: /api/\nDisallow: /app/\n", media_type="text/plain")
