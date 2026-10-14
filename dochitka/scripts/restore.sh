#!/usr/bin/env bash
# Восстановление базы (и фото участников, если рядом есть архив) из бэкапа. ВНИМАНИЕ: текущие данные будут заменены.
#   bash scripts/restore.sh backups/dochitka_20261007_0330.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."
f="${1:?Укажи файл бэкапа}"
read -r -p "Заменить текущую базу данными из $f? (yes/no) " ok
[ "$ok" = "yes" ] || exit 1
docker compose stop app
docker compose exec -T db psql -U dochitka -d postgres -c "DROP DATABASE IF EXISTS dochitka;" -c "CREATE DATABASE dochitka OWNER dochitka;"
gunzip -c "$f" | docker compose exec -T db psql -U dochitka -d dochitka -q
avatars="${f%.sql.gz}.avatars.tar.gz"
if [ -f "$avatars" ]; then
  mkdir -p data && rm -rf data/avatars && tar -xzf "$avatars" -C data
  chown -R 10001:10001 data/avatars 2>/dev/null || true
  echo "Фото участников восстановлены."
fi
docker compose start app
echo "Готово."
