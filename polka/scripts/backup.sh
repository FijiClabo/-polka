#!/usr/bin/env bash
# Бэкап базы в backups/ (хранятся последние 14). Восстановление: bash scripts/restore.sh <файл>
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
f="backups/dochitka_$(date +%Y%m%d_%H%M).sql.gz"
docker compose exec -T db pg_dump -U dochitka -d dochitka --no-owner | gzip > "$f"
echo "Бэкап: $f ($(du -h "$f" | cut -f1))"
ls -1t backups/dochitka_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
