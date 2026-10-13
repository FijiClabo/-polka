#!/usr/bin/env bash
# Промежуточный сервер (релей) к Telegram Bot API — для бота на сервере в России.
#
# С российских хостингов api.telegram.org часто не открывается. Релей ставится на недорогой сервер за рубежом
# (Ubuntu 22.04/24.04, 1 ГБ) и пересылает запросы бота в Telegram. Пускает только ваш основной сервер (по IP),
# соединение защищено HTTPS-сертификатом.
#
# Что нужно заранее:
#   1) сервер за рубежом и его IP;
#   2) поддомен, например tg.твой-домен.ru, с A-записью на IP этого сервера;
#   3) IP основного сервера в России.
#
# Запуск на зарубежном сервере под root:
#   curl -fsSL https://raw.githubusercontent.com/FijiClabo/-polka/claude/telegram-book-app-ip11fm/dochitka/scripts/relay_setup.sh -o relay_setup.sh
#   bash relay_setup.sh tg.твой-домен.ru IP_ОСНОВНОГО_СЕРВЕРА
#
# Потом в .env основного сервера:
#   TELEGRAM_API_BASE=https://tg.твой-домен.ru
#   BOT_MODE=polling
# и bash scripts/deploy.sh.
set -euo pipefail

DOMAIN="${1:-}"
MAIN_IP="${2:-}"
if [[ -z "$DOMAIN" || -z "$MAIN_IP" ]]; then
  echo "Использование: bash relay_setup.sh tg.твой-домен.ru IP_ОСНОВНОГО_СЕРВЕРА" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Запусти под root: sudo bash relay_setup.sh $DOMAIN $MAIN_IP" >&2
  exit 1
fi

echo "[1/4] Ставлю Caddy…"
apt-get update -qq
apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg ufw >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi

echo "[2/4] Настраиваю релей ${DOMAIN} → api.telegram.org (пускаю только ${MAIN_IP})…"
cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
	@main remote_ip ${MAIN_IP}
	handle @main {
		reverse_proxy https://api.telegram.org {
			header_up Host api.telegram.org
			transport http {
				read_timeout 70s
			}
		}
	}
	respond "Forbidden" 403
	log {
		output discard
	}
}
EOF
systemctl enable caddy >/dev/null
systemctl restart caddy

echo "[3/4] Открываю порты 80 и 443, остальное закрываю…"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "[4/4] Проверяю, что Telegram отвечает отсюда…"
if curl -fsS -m 10 https://api.telegram.org >/dev/null 2>&1 || curl -sS -m 10 -o /dev/null -w "%{http_code}" https://api.telegram.org | grep -qE "^(200|302|404)$"; then
  echo "  [ок] api.telegram.org доступен с этого сервера"
else
  echo "  [ошибка] api.telegram.org не отвечает и отсюда — выбери сервер в другой стране" >&2
fi

cat <<EOF

Готово. Сертификат для ${DOMAIN} Caddy получит сам за минуту, если A-запись уже указывает на этот сервер.

Дальше на основном сервере в .env:
  TELEGRAM_API_BASE=https://${DOMAIN}
  BOT_MODE=polling
и запусти: bash scripts/deploy.sh — проверка в конце покажет, видит ли бот Telegram.

Логи запросов релей не пишет. Через него проходят сообщения бота, поэтому в уведомлении в Роскомнадзор
отметь трансграничную передачу (страна сервера релея).
EOF
