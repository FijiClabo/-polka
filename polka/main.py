"""Точка входа: API мини-приложения + бот + планировщик в одном процессе.

    python main.py                 # BOT_MODE=polling — локально, без HTTPS
    BOT_MODE=webhook python main.py  # на сервере за Caddy (HTTPS)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from api.pages import router as pages_router
from api.routes import public_router, set_bot
from api.routes import router as api_router
from bot.app import create_bot, create_dispatcher, setup_bot
from db.session import get_engine, init_engine
from jobs.scheduler import run_scheduler
from services.common import init_clock
from settings import get_settings

log = logging.getLogger("dochitka")
WEBAPP_DIST = Path(__file__).parent / "webapp" / "dist"

_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_engine()
    await init_clock(s.fast_day_minutes)
    s.data_dir.mkdir(parents=True, exist_ok=True)

    stop = asyncio.Event()
    app.state.bot = None
    if s.bot_token:
        bot = create_bot()
        dp = create_dispatcher()
        app.state.bot, app.state.dp = bot, dp
        set_bot(bot)
        await setup_bot(bot)
        if s.bot_mode == "webhook":
            if not s.public_url.startswith("https://"):
                raise RuntimeError("Для BOT_MODE=webhook нужен PUBLIC_URL с https://")
            await bot.set_webhook(
                f"{s.public_url}{s.webhook_path}", secret_token=s.webhook_secret or None,
                allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False,
            )
            log.info("webhook set: %s%s", s.public_url, s.webhook_path)
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            _spawn(dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types()))
            log.info("polling started")
        _spawn(run_scheduler(bot, stop))
    else:
        log.warning("BOT_TOKEN не задан — работает только API")
    try:
        yield
    finally:
        stop.set()
        if app.state.bot is not None:
            if s.bot_mode != "webhook":
                try:
                    await app.state.dp.stop_polling()
                except Exception:
                    pass
            await app.state.bot.session.close()
        for t in list(_tasks):
            t.cancel()
        await get_engine().dispose()


app = FastAPI(title="Dochitka", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(api_router)
app.include_router(public_router)
app.include_router(pages_router)


@app.post(get_settings().webhook_path)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    s = get_settings()
    if s.webhook_secret and x_telegram_bot_api_secret_token != s.webhook_secret:
        raise HTTPException(status_code=403)
    bot, dp = request.app.state.bot, request.app.state.dp
    update = Update.model_validate(await request.json(), context={"bot": bot})
    # отвечаем Telegram сразу, обработка (в т.ч. проверка ИИ) идёт в фоне
    _spawn(dp.feed_update(bot, update))
    return {"ok": True}


@app.get("/health")
async def health():
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        db = "ok"
    except Exception as e:  # pragma: no cover
        db = f"error: {e.__class__.__name__}"
    return JSONResponse({"ok": db == "ok", "db": db}, status_code=200 if db == "ok" else 503)


# --------------------------------------------------------------------------- мини-приложение (статика)

if WEBAPP_DIST.exists():
    app.mount("/app/assets", StaticFiles(directory=WEBAPP_DIST / "assets"), name="assets")

    @app.get("/app/{path:path}")
    async def webapp(path: str):
        f = WEBAPP_DIST / path
        if path and f.is_file() and WEBAPP_DIST in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(WEBAPP_DIST / "index.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    s = get_settings()
    uvicorn.run("main:app", host=s.host, port=s.port, proxy_headers=True, forwarded_allow_ips="*", log_level="info")
