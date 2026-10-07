#!/usr/bin/env bash
# Восстановление базы из бэкапа. ВНИМАНИЕ: текущие данные будут заменены.
#   bash scripts/restore.sh backups/dochitka_20261007_0330.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."
f="${1:?Укажи файл бэкапа}"
read -r -p "Заменить текущую базу данными из $f? (yes/no) " ok
[ "$ok" = "yes" ] || exit 1
docker compose stop app
docker compose exec -T db psql -U dochitka -d postgres -c "DROP DATABASE IF EXISTS dochitka;" -c "CREATE DATABASE dochitka OWNER dochitka;"
gunzip -c "$f" | docker compose exec -T db psql -U dochitka -d dochitka -q
docker compose start app
echo "Готово."
