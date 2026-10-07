#!/usr/bin/env bash
# Локальный запуск на своём компьютере (без домена и HTTPS):
#   бот работает в режиме polling, мини-приложение — на http://localhost:5173/app/
#   (в Telegram мини-приложение без HTTPS не откроется — для этого нужен сервер или туннель).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { cp .env.example .env; echo "Создан .env — впиши хотя бы BOT_TOKEN"; }
export BOT_MODE=polling
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://dochitka:dochitka@localhost:5432/dochitka}"

if ! docker ps --format '{{.Names}}' | grep -q '^dochitka-dev-db$'; then
  echo "==> Запускаю Postgres в Docker (контейнер dochitka-dev-db)"
  docker start dochitka-dev-db >/dev/null 2>&1 || docker run -d --name dochitka-dev-db -p 5432:5432 \
    -e POSTGRES_USER=dochitka -e POSTGRES_PASSWORD=dochitka -e POSTGRES_DB=dochitka postgres:16-alpine >/dev/null
  sleep 5
fi

[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements-dev.txt
alembic upgrade head

(cd webapp && npm install --no-audit --no-fund >/dev/null && npm run build >/dev/null)
echo "==> Бэкенд: http://localhost:8000  (Ctrl+C — остановить)"
python main.py
