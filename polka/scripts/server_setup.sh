#!/usr/bin/env bash
# Подготовка чистого сервера Ubuntu 22.04/24.04: Docker, файрвол, автозапуск бэкапа.
# Запуск (из папки проекта):  sudo bash scripts/server_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then echo "Запусти через sudo: sudo bash scripts/server_setup.sh"; exit 1; fi

echo "==> Обновляю систему"
apt-get update -y && apt-get upgrade -y
apt-get install -y ca-certificates curl git ufw cron

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Ставлю Docker"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==> Файрвол: открываю только SSH, HTTP и HTTPS"
ufw allow OpenSSH || ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "==> Файл подкачки 1 ГБ (на маленьких серверах помогает сборке)"
if ! swapon --show | grep -q swapfile && [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Ежедневный бэкап базы в 03:30"
PROJECT_DIR="$(pwd)"
( crontab -l 2>/dev/null | grep -v 'scripts/backup.sh' ; echo "30 3 * * * cd $PROJECT_DIR && bash scripts/backup.sh >> $PROJECT_DIR/backups/cron.log 2>&1" ) | crontab -
mkdir -p backups data

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "==> Создан файл .env — заполни его:  nano .env"
fi
echo
echo "Готово. Дальше:  nano .env  →  bash scripts/deploy.sh"
