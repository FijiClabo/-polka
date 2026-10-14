#!/usr/bin/env bash
# Автообновление без ключей и секретов: сервер сам раз в 5 минут смотрит ветку на GitHub и, если в ней новая
# версия и тесты на GitHub прошли («Тесты и сборка»), забирает её и перезапускает сервис.
#
#   bash scripts/autoupdate.sh --install   # включить (ставит задачу в cron и проверяет один раз)
#   bash scripts/autoupdate.sh --off       # выключить
#   bash scripts/autoupdate.sh             # проверить сейчас (так его и вызывает cron)
#
# Журнал: backups/autoupdate.log
set -euo pipefail
cd "$(dirname "$0")/.."
APP=$(pwd)
REPO_API="${DOCHITKA_REPO_API:-https://api.github.com/repos/FijiClabo/-polka}"
CHECK_NAME="Тесты и сборка"
LOG="$APP/backups/autoupdate.log"
mkdir -p "$APP/backups"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

if [ "${1:-}" = "--install" ] || [ "${1:-}" = "--off" ]; then
  line="*/5 * * * * bash $APP/scripts/autoupdate.sh >> $LOG 2>&1"
  { crontab -l 2>/dev/null | grep -v 'scripts/autoupdate.sh' || true
    [ "$1" = "--install" ] && echo "$line"; } | crontab -
  # прежний способ (GitHub заходил на сервер по ключу) больше не нужен — ключ убираем
  if [ -f /root/.ssh/authorized_keys ] && grep -q 'dochitka-ci' /root/.ssh/authorized_keys; then
    sed -i '/dochitka-ci/d' /root/.ssh/authorized_keys
    rm -f /root/.ssh/dochitka_ci /root/.ssh/dochitka_ci.pub
    echo "  [ок] старый ключ для GitHub удалён — секреты DEPLOY_* в GitHub больше не нужны"
  fi
  if [ "$1" = "--off" ]; then echo "  [ок] автообновление выключено"; exit 0; fi
  echo "  [ок] автообновление включено: сервер проверяет GitHub раз в 5 минут (журнал: $LOG)"
  exec bash "$APP/scripts/autoupdate.sh"
fi

exec 9>/tmp/dochitka-deploy.lock
flock -n 9 || exit 0  # обновление уже идёт

branch=$(git -C .. rev-parse --abbrev-ref HEAD)
git -C .. fetch --quiet origin "$branch"
local_sha=$(git -C .. rev-parse HEAD)
remote_sha=$(git -C .. rev-parse "origin/$branch")
[ "$local_sha" = "$remote_sha" ] && exit 0

if git -C .. diff --quiet "$local_sha" "$remote_sha" -- dochitka; then
  # изменения вне приложения (например, в описании репозитория) — просто забираем, перезапуск не нужен
  git -C .. reset --quiet --hard "origin/$branch"
  exit 0
fi
if true; then
  # меняется само приложение — выкладываем, только если тесты на GitHub прошли
  conclusion=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
      "$REPO_API/commits/$remote_sha/check-runs?per_page=100" 2>/dev/null \
    | jq -r --arg n "$CHECK_NAME" '[.check_runs[] | select(.name == $n)] | sort_by(.started_at) | last
        | if . == null then "none" elif .status != "completed" then "pending" else (.conclusion // "pending") end' \
    2>/dev/null || echo "error")
  case "$conclusion" in
    success) ;;
    pending|none) exit 0 ;;  # тесты ещё идут — проверим через 5 минут
    error) log "GitHub не ответил про тесты для ${remote_sha:0:7} — попробую позже"; exit 0 ;;
    *)
      if [ ! -f "$APP/backups/.failed_$remote_sha" ]; then
        log "тесты для ${remote_sha:0:7} не прошли ($conclusion) — эту версию не выкладываю"
        touch "$APP/backups/.failed_$remote_sha"
      fi
      exit 0 ;;
  esac
fi

log "обновляю ${local_sha:0:7} → ${remote_sha:0:7}: $(git -C .. log -1 --format=%s "$remote_sha")"
git -C .. reset --quiet --hard "origin/$branch"  # .env, data/ и backups/ не в git — не трогаются
if bash scripts/deploy.sh > "$APP/backups/last_deploy.log" 2>&1; then
  log "готово: $(tail -n 3 "$APP/backups/last_deploy.log" | tr '\n' ' ')"
else
  log "обновление не удалось — подробности: $APP/backups/last_deploy.log"
fi
