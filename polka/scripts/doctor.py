"""Самодиагностика: что настроено, что нет и как починить.

    python -m scripts.doctor            # локально
    docker compose exec app python -m scripts.doctor   # на сервере
"""

from __future__ import annotations

import asyncio
import shutil
import sys

import httpx
from sqlalchemy import text

from settings import get_settings

OK, BAD, WARN = "✅", "❌", "⚠️ "
problems = 0


def say(mark: str, title: str, hint: str = "") -> None:
    global problems
    if mark == BAD:
        problems += 1
    print(f"{mark} {title}")
    if hint:
        print(f"     → {hint}")


async def check_db() -> None:
    from db.session import get_engine, init_engine

    init_engine()
    try:
        async with get_engine().connect() as c:
            await c.execute(text("SELECT 1"))
            try:
                rev = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            except Exception:
                rev = None
        say(OK if rev else WARN, f"База данных доступна{f', миграция {rev}' if rev else ''}",
            "" if rev else "Миграции не применены: alembic upgrade head")
    except Exception as e:
        say(BAD, f"База данных недоступна: {e.__class__.__name__}", "Проверь DATABASE_URL и что контейнер db запущен")
    finally:
        await get_engine().dispose()


async def check_bot(s) -> None:
    if not s.bot_token:
        say(BAD, "BOT_TOKEN не задан", "Создай бота в @BotFather и вставь токен в .env")
        return
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            base = (s.telegram_api_base or "https://api.telegram.org").rstrip("/")
            me = (await c.get(f"{base}/bot{s.bot_token}/getMe")).json()
        except httpx.HTTPError as e:
            say(BAD, f"Нет связи с api.telegram.org: {e.__class__.__name__}", "Сервер должен иметь доступ к Telegram")
            return
        if not me.get("ok"):
            say(BAD, "Токен бота не принят Telegram", "Проверь BOT_TOKEN (без пробелов и кавычек)")
            return
        say(OK, f"Бот @{me['result']['username']} на связи")
        info = (await c.get(f"https://api.telegram.org/bot{s.bot_token}/getWebhookInfo")).json().get("result", {})
        url = info.get("url", "")
        if s.bot_mode == "webhook":
            want = f"{s.public_url}{s.webhook_path}"
            if url == want:
                err = info.get("last_error_message")
                say(WARN if err else OK, f"Webhook: {url}" + (f" (последняя ошибка: {err})" if err else ""),
                    "Если ошибка свежая — проверь, что домен открывается по HTTPS" if err else "")
            else:
                say(WARN, f"Webhook ещё не установлен (сейчас: {url or 'пусто'})", "Он ставится при запуске приложения")
        else:
            say(OK if not url else WARN, "Режим polling" + (f", но висит webhook {url}" if url else ""))
    if not s.admin_ids:
        say(WARN, "ADMIN_TG_IDS пуст — админ-команды никому не доступны", "Узнай свой tg_id у @userinfobot")
    else:
        say(OK, f"Админы: {', '.join(map(str, s.admin_ids))}")


async def check_public(s) -> None:
    if not s.public_url.startswith("https://"):
        say(WARN if s.bot_mode == "polling" else BAD, "PUBLIC_URL без https:// — мини-приложение в Telegram не откроется",
            "Укажи домен: PUBLIC_URL=https://твой-домен")
        return
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        try:
            r = await c.get(f"{s.public_url}/health")
            say(OK if r.status_code == 200 else BAD, f"{s.public_url} отвечает ({r.status_code})")
            r = await c.get(f"{s.public_url}/app/")
            say(OK if r.status_code == 200 and "<div id=\"root\">" in r.text else BAD,
                "Мини-приложение отдаётся по /app/", "" if r.status_code == 200 else "Пересобери образ: bash scripts/deploy.sh")
        except httpx.HTTPError as e:
            say(BAD, f"{s.public_url} недоступен: {e.__class__.__name__}",
                "Проверь A-запись домена на IP сервера и открытые порты 80/443")


async def check_ai(s) -> None:
    from ai.llm import build_chain

    chain = build_chain(s)
    if not chain.providers:
        say(BAD, "Нет ни одного ИИ-провайдера", "Заполни ANTHROPIC_API_KEY и/или YANDEX_API_KEY + YANDEX_FOLDER_ID")
        return
    for p in chain.providers:
        try:
            r = await p.complete("Ответь словом ok.", "", "Проверка связи.", schema=None, cheap=True, max_tokens=50)
            say(OK, f"ИИ {p.name}: отвечает ({r.model})")
        except Exception as e:
            say(BAD if p is chain.providers[0] else WARN, f"ИИ {p.name}: ошибка — {str(e)[:160]}",
                "Проверь ключ и баланс. Claude недоступен с российских серверов." if p.name == "anthropic" else "Проверь ключ, роль сервисного аккаунта и ID каталога")
    if len(chain.providers) < 2:
        say(WARN, "Запасного ИИ нет", "Рекомендуется второй провайдер: при сбое основного пересказы проверит он")


async def check_stt(s) -> None:
    from ai.stt import build_stt, pcm_duration, to_pcm

    if not shutil.which("ffmpeg"):
        say(BAD, "ffmpeg не установлен", "В Docker-образе он есть; локально: apt install ffmpeg / brew install ffmpeg")
        return
    providers = build_stt(s)
    if not providers:
        say(WARN, "Распознавание голоса не настроено — голосовые не будут приниматься",
            "Нужен YANDEX_API_KEY (SpeechKit) или WHISPER_API_KEY")
        return
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1",
        "-c:a", "libopus", "-f", "ogg", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ogg, _ = await proc.communicate()
    pcm = await to_pcm(ogg)
    for p in providers:
        try:
            await p.transcribe(pcm)
            say(OK, f"Распознавание речи {p.name}: работает ({pcm_duration(pcm):.0f} с тишины распознано)")
        except Exception as e:
            say(BAD, f"Распознавание речи {p.name}: ошибка — {str(e)[:160]}", "Проверь ключ и роль ai.speechkit-stt.user")


async def check_payments(s) -> None:
    if s.payments_stars:
        say(OK, "Оплата звёздами в Telegram включена")
    if s.payments_rub_in_bot:
        say(WARN, "PAYMENTS_RUB_IN_BOT=true: рубли прямо в боте нарушают правила Telegram для цифровых услуг",
            "Бота могут скрыть или удалить. Рубли — через сайт (YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY)")
    if not s.site_checkout:
        say(WARN, "Оплата рублями на сайте не подключена",
            "Договор с ЮKassa → YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY; уведомления на https://домен/pay/yookassa")
    else:
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.get("https://api.yookassa.ru/v3/me", auth=(s.yookassa_shop_id, s.yookassa_secret_key))
                if r.status_code == 200:
                    me = r.json()
                    test = " (ТЕСТОВЫЙ магазин)" if me.get("test") else ""
                    say(OK, f"ЮKassa: магазин {me.get('account_id')}{test}, статус {me.get('status')}")
                    if s.fiscal_receipts and not (me.get("fiscalization") or {}).get("enabled", me.get("fiscalization_enabled")):
                        say(WARN, "В ЮKassa не включены чеки, а FISCAL_RECEIPTS=true",
                            "Подключи «Чеки от ЮKassa» или поставь FISCAL_RECEIPTS=false (самозанятым — чек в «Мой налог»)")
                else:
                    say(BAD, f"ЮKassa отвечает {r.status_code}", "Проверь shopId и секретный ключ")
            except httpx.HTTPError as e:
                say(BAD, f"Нет связи с api.yookassa.ru: {e.__class__.__name__}", "Проверь сеть сервера")
        if not s.public_url.startswith("https://"):
            say(BAD, "Для оплаты на сайте нужен PUBLIC_URL с https://", "ЮKassa возвращает покупателя на /pay/done")
    missing = [k for k, v in (("SELLER_NAME", s.seller_name), ("SELLER_INN", s.seller_inn), ("SELLER_EMAIL", s.seller_email)) if not v]
    if missing:
        say(BAD if s.site_checkout or s.payments_stars else WARN, f"Не заполнены реквизиты продавца: {', '.join(missing)}",
            "Они нужны в оферте, политике данных и подвале сайта")
    if s.site_checkout and not s.smtp_enabled:
        say(WARN, "Почта не настроена — код активации покупатель увидит только на странице после оплаты",
            "Заполни SMTP_HOST, SMTP_FROM (и логин/пароль), чтобы код дублировался на e-mail")


async def main() -> None:
    s = get_settings()
    print(f"\n=== Самодиагностика «{s.project_name}» ===\n")
    await check_db()
    await check_bot(s)
    await check_public(s)
    await check_ai(s)
    await check_stt(s)
    await check_payments(s)
    if s.fast_day_minutes:
        say(WARN, f"Включено ускоренное время: сутки = {s.fast_day_minutes} мин", "Для настоящего забега: FAST_DAY_MINUTES=0")
    if s.bot_mode == "webhook" and not s.webhook_secret_ok:
        say(BAD, "WEBHOOK_SECRET пустой или из примера — можно подделать запросы Telegram, в том числе «оплату»",
            "Удали строку WEBHOOK_SECRET из .env и запусти bash scripts/deploy.sh — он создаст случайный")
    if s.dev_auth_bypass:
        say(BAD, "DEV_AUTH_BYPASS=true — проверка подписи отключена!", "Только для локального просмотра. На сервере — false")
    print()
    print("Всё готово к работе 🎉" if problems == 0 else f"Нужно исправить пунктов: {problems}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    asyncio.run(main())
