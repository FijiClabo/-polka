#!/usr/bin/env bash
# Обновление по команде из GitHub Actions. Запускается ТОЛЬКО через ключ dochitka-ci:
# в /root/.ssh/authorized_keys у него стоит command="bash …/ci_deploy.sh" — этот ключ не даёт консоли,
# туннелей и других команд, только забрать свежий код и перезапустить сервис.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${SSH_ORIGINAL_COMMAND:-deploy}" in
  deploy) ;;
  *) echo "Разрешена только команда deploy"; exit 1 ;;
esac

# два обновления одновременно не запускаем
exec 9>/tmp/dochitka-deploy.lock
flock -n 9 || { echo "Обновление уже идёт — пропускаю"; exit 0; }

branch=$(git -C .. rev-parse --abbrev-ref HEAD)
echo "==> Забираю $branch"
git -C .. fetch --quiet origin "$branch"
# .env, data/ и backups/ не в git — они не трогаются
git -C .. reset --quiet --hard "origin/$branch"
echo "    версия $(git -C .. log -1 --format='%h %s')"

bash scripts/deploy.sh 2>&1 | tail -n 80
