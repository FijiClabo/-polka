#!/usr/bin/env bash
# Бэкап базы и своих фото участников в backups/ (хранятся последние 14).
# Восстановление: bash scripts/restore.sh backups/dochitka_<дата>.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077  # в бэкапе личные данные — читать только root
mkdir -p backups
stamp="$(date +%Y%m%d_%H%M)"
f="backups/dochitka_${stamp}.sql.gz"
docker compose exec -T db pg_dump -U dochitka -d dochitka --no-owner | gzip > "$f"
echo "Бэкап: $f ($(du -h "$f" | cut -f1))"
if [ -d data/avatars ] && [ -n "$(ls -A data/avatars 2>/dev/null)" ]; then
  tar -czf "backups/dochitka_${stamp}.avatars.tar.gz" -C data avatars
  echo "Фото: backups/dochitka_${stamp}.avatars.tar.gz"
fi
ls -1t backups/dochitka_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t backups/dochitka_*.avatars.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
