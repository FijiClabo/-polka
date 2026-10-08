#!/usr/bin/env bash
# Развёртывание и обновление: забрать код, собрать, перезапустить, проверить.
#   bash scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "Нет файла .env. Сначала: cp .env.example .env && nano .env"; exit 1; }
grep -q '^BOT_TOKEN=.\+' .env || { echo "В .env не заполнен BOT_TOKEN"; exit 1; }
grep -q '^DOMAIN=.\+' .env || { echo "В .env не заполнен DOMAIN"; exit 1; }
# секрет вебхука: если пустой или из примера — создаём случайный (без него можно подделать «оплату»)
if ! grep -qE '^WEBHOOK_SECRET=[A-Za-z0-9_-]{16,}$' .env || grep -q '^WEBHOOK_SECRET=change-me' .env; then
  secret="$(openssl rand -hex 32)"
  if grep -q '^WEBHOOK_SECRET=' .env; then sed -i "s/^WEBHOOK_SECRET=.*/WEBHOOK_SECRET=${secret}/" .env
  else echo "WEBHOOK_SECRET=${secret}" >> .env; fi
  echo "==> Создан случайный WEBHOOK_SECRET"
fi

if [ -d ../.git ] || [ -d .git ]; then
  echo "==> Забираю обновления из git"
  git pull --ff-only || echo "(git pull не удался — продолжаю с текущим кодом)"
fi

mkdir -p data backups
if docker compose ps --status running 2>/dev/null | grep -q db; then
  echo "==> Бэкап базы перед обновлением"
  bash scripts/backup.sh || true
fi

echo "==> Собираю и запускаю"
docker compose up -d --build

echo "==> Жду, пока приложение поднимется"
for i in $(seq 1 60); do
  if docker compose exec -T app curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "Приложение работает."
    break
  fi
  sleep 2
  if [ "$i" = 60 ]; then
    echo "Приложение не поднялось. Последние логи:"
    docker compose logs --tail 80 app
    exit 1
  fi
done

docker image prune -f >/dev/null 2>&1 || true
echo
echo "==> Самодиагностика"
docker compose exec -T app python -m scripts.doctor || true
echo
echo "Логи в реальном времени:  bash scripts/logs.sh"
