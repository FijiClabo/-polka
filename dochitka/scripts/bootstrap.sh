#!/usr/bin/env bash
# =============================================================================
#  Дочитка: установка на чистый сервер одной командой
#
#  На новом сервере Ubuntu 22.04/24.04 (2 ГБ памяти, 20 ГБ диска) под root:
#    curl -fsSL https://raw.githubusercontent.com/FijiClabo/-polka/claude/telegram-book-app-ip11fm/dochitka/scripts/bootstrap.sh -o bootstrap.sh
#    bash bootstrap.sh
#
#  Скрипт сам: защитит сервер, поставит Docker, скачает код, спросит настройки и запишет .env,
#  проверит домен и связь с Telegram, запустит бота и сайт, включит ежедневный бэкап
#  и (по желанию) даст ключ, чтобы GitHub сам выкладывал обновления.
#
#  Повторный запуск безопасен: сделанное пропускается, прошлые ответы подставляются по умолчанию.
#  Без вопросов (например, из Claude Code): NONINTERACTIVE=1 DOMAIN=... BOT_TOKEN=... ADMIN_TG_IDS=... bash bootstrap.sh
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/FijiClabo/-polka.git}"
REPO_SSH="${REPO_SSH:-git@github.com:FijiClabo/-polka.git}"
BRANCH="${BRANCH:-claude/telegram-book-app-ip11fm}"
ROOT="${ROOT:-/opt/dochitka}"
APP="$ROOT/dochitka"
NONINTERACTIVE="${NONINTERACTIVE:-0}"
LOG=/var/log/dochitka-bootstrap.log

# --------------------------------------------------------------------------- вывод и вопросы

if [ "$NONINTERACTIVE" != 1 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then TTY=/dev/tty; else TTY=""; NONINTERACTIVE=1; fi

out()  { printf '%s\n' "$*" | tee -a "$LOG"; }
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; printf '\n==> %s\n' "$*" >>"$LOG"; }
ok()   { out "  [ок] $*"; }
warn() { out "  [внимание] $*"; }
die()  { out ""; out "[ошибка] $*"; exit 1; }
tty()  { if [ -n "$TTY" ]; then printf '%s' "$*" >"$TTY"; fi; }

# ask ПЕРЕМЕННАЯ "Вопрос" "по умолчанию"  — ответ из окружения (если задан) или с клавиатуры
ask() {
  local __var=$1 q=$2 def=${3-} ans="${!1-}"
  if [ -n "$ans" ]; then printf -v "$__var" '%s' "$ans"; return; fi
  if [ "$NONINTERACTIVE" = 1 ]; then printf -v "$__var" '%s' "$def"; return; fi
  if [ -n "$def" ]; then tty "$q [$def]: "; else tty "$q: "; fi
  IFS= read -r ans <"$TTY" || ans=""
  printf -v "$__var" '%s' "${ans:-$def}"
}

# ask_secret — то же, но без эха; по умолчанию — уже сохранённое значение (показывается замазанным)
ask_secret() {
  local __var=$1 q=$2 def=${3-} ans="${!1-}"
  if [ -n "$ans" ]; then printf -v "$__var" '%s' "$ans"; return; fi
  if [ "$NONINTERACTIVE" = 1 ]; then printf -v "$__var" '%s' "$def"; return; fi
  if [ -n "$def" ]; then tty "$q [сохранён, Enter — оставить]: "; else tty "$q: "; fi
  IFS= read -rs ans <"$TTY" || ans=""
  tty $'\n'
  printf -v "$__var" '%s' "${ans:-$def}"
}

# yes_no "Вопрос" y|n — 0, если «да»
yes_no() {
  local q=$1 def=${2:-y} ans
  if [ "$NONINTERACTIVE" = 1 ]; then [ "$def" = y ]; return; fi
  tty "$q [$( [ "$def" = y ] && echo 'Д/н' || echo 'д/Н' )]: "
  IFS= read -r ans <"$TTY" || ans=""
  ans=${ans:-$def}
  case "$ans" in [yYдД]*) return 0 ;; *) return 1 ;; esac
}

pause_tty() { if [ "$NONINTERACTIVE" != 1 ]; then tty "$1 — нажми Enter, когда готово "; IFS= read -r _ <"$TTY" || true; fi; }

# --------------------------------------------------------------------------- .env

env_get() { [ -f "$APP/.env" ] && grep -m1 "^$1=" "$APP/.env" | cut -d= -f2- || true; }

env_set() {  # замена строки KEY=… без sed: значения могут содержать / & | и пробелы
  local key=$1 val=$2 f="$APP/.env" tmp line found=0
  tmp=$(mktemp)
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ $line == "$key="* ]]; then printf '%s=%s\n' "$key" "$val" >>"$tmp"; found=1
    else printf '%s\n' "$line" >>"$tmp"; fi
  done <"$f"
  [ "$found" = 1 ] || printf '%s=%s\n' "$key" "$val" >>"$tmp"
  cat "$tmp" >"$f"
  rm -f "$tmp"
}

# --------------------------------------------------------------------------- 0. проверки

[ "$(id -u)" -eq 0 ] || die "Запусти под root: sudo bash bootstrap.sh"
touch "$LOG" && chmod 600 "$LOG"
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = ubuntu ] || warn "Скрипт проверен на Ubuntu 22.04/24.04, у тебя: ${PRETTY_NAME:-неизвестно}"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
[ "$mem_mb" -ge 1800 ] || warn "Памяти ${mem_mb} МБ — нужно от 2 ГБ, иначе сборка может упасть (добавлю подкачку)"
disk_gb=$(df -BG / | awk 'NR==2 {gsub("G","",$4); print $4}')
[ "${disk_gb:-0}" -ge 8 ] || warn "Свободно ${disk_gb} ГБ на диске — нужно от 8 ГБ"

say "Дочитка: установка (журнал — $LOG)"

# --------------------------------------------------------------------------- 1. система и защита

say "1/8 Обновляю систему и ставлю защиту"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get -y -qq -o Dpkg::Options::=--force-confold upgrade >/dev/null
apt-get install -y -qq ca-certificates curl git ufw fail2ban unattended-upgrades openssl dnsutils cron jq >/dev/null
ok "пакеты обновлены"

# обновления безопасности ставятся сами
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
ok "обновления безопасности — автоматически"

# защита от подбора пароля SSH
cat >/etc/fail2ban/jail.d/dochitka-sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban >/dev/null 2>&1 && systemctl restart fail2ban
ok "fail2ban: 5 неверных попыток входа — бан на час"

# файрвол: только SSH и сайт
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
for p in $(ss -tlnpH 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}' | sort -u); do
  ufw allow "$p/tcp" >/dev/null  # SSH на нестандартном порту не должен закрыться
done
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
ok "файрвол: открыты только SSH, 80 и 443"

# подкачка на маленьких серверах
if ! swapon --show | grep -q . && [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >>/etc/fstab
  ok "подкачка 2 ГБ"
fi

# вход по SSH: пароли подбирают боты круглые сутки. Выключаем их, только если ты сам сейчас вошёл по ключу —
# иначе можно потерять доступ к серверу
client_ip=${SSH_CLIENT%% *}
login_method=""
if [ -n "$client_ip" ]; then
  login_method=$( (journalctl -u ssh -u sshd --since "-6h" --no-pager 2>/dev/null; cat /var/log/auth.log 2>/dev/null) \
    | grep "Accepted .* for root from $client_ip " | tail -n1 | awk '{for(i=1;i<=NF;i++) if($i=="Accepted") print $(i+1)}' || true)
fi
if [ "$login_method" = publickey ]; then
  if yes_no "Ты вошёл по SSH-ключу. Выключить вход по паролю (рекомендую)?" y; then
    cat >/etc/ssh/sshd_config.d/60-dochitka.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    if sshd -t 2>/dev/null; then systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true; ok "вход только по ключу"
    else rm -f /etc/ssh/sshd_config.d/60-dochitka.conf; warn "настройку SSH не применил — проверка sshd -t не прошла"; fi
  fi
else
  warn "Ты вошёл по паролю — он оставлен (от подбора защищает fail2ban). Лучше перейти на SSH-ключ: LAUNCH.md, затем запусти скрипт ещё раз"
fi

# --------------------------------------------------------------------------- 2. Docker

say "2/8 Docker"
if ! command -v docker >/dev/null 2>&1; then
  if curl -fsSL --max-time 30 https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh >/dev/null 2>&1; then
    ok "Docker поставлен с docker.com"
  else
    warn "docker.com недоступен — ставлю Docker из репозитория Ubuntu"
    apt-get install -y -qq docker.io docker-compose-v2 >/dev/null || apt-get install -y -qq docker.io docker-compose-plugin >/dev/null
  fi
fi
systemctl enable --now docker >/dev/null 2>&1
docker compose version >/dev/null 2>&1 || die "Не нашёл docker compose. Поставь пакет docker-compose-v2 и запусти скрипт ещё раз"

# Docker Hub из России бывает недоступен — тогда образы берём через зеркала
hub=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://registry-1.docker.io/v2/ || echo 000)
if [ "$hub" != 401 ] && [ "$hub" != 200 ] && [ ! -f /etc/docker/daemon.json ]; then
  cat >/etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://mirror.gcr.io", "https://dockerhub.timeweb.cloud"],
  "log-driver": "json-file",
  "log-opts": {"max-size": "20m", "max-file": "5"}
}
EOF
  systemctl restart docker
  ok "Docker Hub недоступен (код $hub) — включил зеркала mirror.gcr.io и dockerhub.timeweb.cloud"
else
  ok "Docker готов: $(docker --version | cut -d, -f1)"
fi

# --------------------------------------------------------------------------- 3. код

say "3/8 Код проекта → $ROOT"
if [ -d "$ROOT/.git" ]; then
  git -C "$ROOT" fetch --quiet origin "$BRANCH" && git -C "$ROOT" checkout --quiet "$BRANCH" && git -C "$ROOT" pull --quiet --ff-only \
    && ok "код обновлён ($(git -C "$ROOT" log -1 --format=%h))" || warn "не удалось обновить код — продолжаю с тем, что есть"
elif git clone --quiet -b "$BRANCH" "$REPO_URL" "$ROOT" 2>/dev/null; then
  ok "код скачан ($(git -C "$ROOT" log -1 --format=%h))"
else
  # закрытый репозиторий: ключ только для чтения (Deploy key)
  warn "Репозиторий не скачивается по HTTPS — похоже, он закрытый. Нужен ключ только для чтения."
  key=/root/.ssh/dochitka_repo
  [ -f "$key" ] || ssh-keygen -q -t ed25519 -N "" -C "dochitka-repo-readonly" -f "$key"
  tty $'\nGitHub → репозиторий → Settings → Deploy keys → Add deploy key.\nTitle: server, Key — эта строка (галочку Allow write access НЕ ставить):\n\n'
  tty "$(cat "$key.pub")"$'\n\n'
  pause_tty "Добавь ключ на GitHub"
  export GIT_SSH_COMMAND="ssh -i $key -o StrictHostKeyChecking=accept-new"
  git clone --quiet -b "$BRANCH" "$REPO_SSH" "$ROOT" || die "Не получилось скачать код даже с ключом. Проверь, что ключ добавлен в Deploy keys"
  git -C "$ROOT" config core.sshCommand "ssh -i $key -o StrictHostKeyChecking=accept-new"
  ok "код скачан по ключу"
fi
[ -f "$APP/docker-compose.yml" ] || die "В $APP нет docker-compose.yml — не та ветка?"

# --------------------------------------------------------------------------- 4. настройки

say "4/8 Настройки (.env)"
if [ ! -f "$APP/.env" ]; then
  cp "$APP/.env.example" "$APP/.env"
  ok "создан .env из примера"
fi
chmod 600 "$APP/.env"

pw=$(env_get POSTGRES_PASSWORD)
if [ -z "$pw" ] || [[ $pw == change-me* ]]; then env_set POSTGRES_PASSWORD "$(openssl rand -hex 24)"; ok "придуман пароль базы"; fi
ws=$(env_get WEBHOOK_SECRET)
if [ ${#ws} -lt 16 ] || [[ $ws == change-me* ]]; then env_set WEBHOOK_SECRET "$(openssl rand -hex 32)"; ok "придуман секрет вебхука"; fi

def_domain=$(env_get DOMAIN); [[ $def_domain == *example* ]] && def_domain=""
tty $'\nОтвечай на вопросы. Enter — оставить значение в скобках.\n\n'

while :; do
  ask DOMAIN "Домен сайта и мини-приложения (например app.dochitka.ru, без https://)" "$def_domain"
  DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN#http://}; DOMAIN=${DOMAIN%%/*}
  [[ $DOMAIN =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] && break
  [ "$NONINTERACTIVE" = 1 ] && die "DOMAIN не задан или неверный"
  tty "  Похоже на опечатку. Нужен домен вида app.dochitka.ru"$'\n'; DOMAIN=""
done
env_set DOMAIN "$DOMAIN"
env_set PUBLIC_URL "https://$DOMAIN"

while :; do
  ask_secret BOT_TOKEN "Токен бота от @BotFather" "$(env_get BOT_TOKEN)"
  [[ $BOT_TOKEN =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] && break
  [ "$NONINTERACTIVE" = 1 ] && die "BOT_TOKEN не задан или неверный"
  tty "  Токен выглядит как 123456789:AA… — скопируй его из @BotFather целиком"$'\n'; BOT_TOKEN=""
done
env_set BOT_TOKEN "$BOT_TOKEN"

while :; do
  ask ADMIN_TG_IDS "Твой Telegram ID — число от @userinfobot (несколько — через запятую)" "$(env_get ADMIN_TG_IDS)"
  ADMIN_TG_IDS=${ADMIN_TG_IDS// /}
  [[ $ADMIN_TG_IDS =~ ^[0-9]{5,15}(,[0-9]{5,15})*$ ]] && break
  [ "$NONINTERACTIVE" = 1 ] && die "ADMIN_TG_IDS не задан или неверный"
  tty "  Нужны только цифры, например 123456789"$'\n'; ADMIN_TG_IDS=""
done
env_set ADMIN_TG_IDS "$ADMIN_TG_IDS"

ask SUPPORT_CONTACT "Контакт для вопросов участников (например @твой_username)" "$(env_get SUPPORT_CONTACT)"
env_set SUPPORT_CONTACT "$SUPPORT_CONTACT"

tty $'\nИИ-проверка пересказов и голос — Yandex Cloud (ключ и ID каталога, см. LAUNCH.md). Можно пропустить и вписать позже.\n'
ask_secret YANDEX_API_KEY "API-ключ Yandex Cloud (AQVN…)" "$(env_get YANDEX_API_KEY)"
ask YANDEX_FOLDER_ID "ID каталога Yandex Cloud (b1g…)" "$(env_get YANDEX_FOLDER_ID)"
env_set YANDEX_API_KEY "$YANDEX_API_KEY"
env_set YANDEX_FOLDER_ID "$YANDEX_FOLDER_ID"
# только YandexGPT: Anthropic не обслуживает Россию (anthropic.com/supported-countries), а данные не уходят за рубеж
env_set LLM_PROVIDERS "yandex"
env_set STT_PROVIDER yandex
# модель проверки: lite — примерно в 4 раза дешевле (для теста), pro — точнее вердикты
cur_model=$(env_get YANDEXGPT_MODEL)
case "$cur_model" in yandexgpt-lite/*) def_model=lite ;; yandexgpt/*) def_model=pro ;; *) def_model=lite ;; esac
ask GPT_MODEL "Модель проверки пересказов: lite — дешевле в ~4 раза, pro — точнее (lite/pro)" "$def_model"
case "${GPT_MODEL,,}" in pro) env_set YANDEXGPT_MODEL "yandexgpt/latest" ;; *) env_set YANDEXGPT_MODEL "yandexgpt-lite/latest" ;; esac
[ -n "$YANDEX_API_KEY" ] && [ -n "$YANDEX_FOLDER_ID" ] || warn "ключа Yandex Cloud нет — пересказы будут засчитываться без проверки ИИ, пока его не впишешь"

tty $'\nПродавец — для оферты, политики и чеков (самозанятый: ФИО и ИНН из «Мой налог»). Можно заполнить позже.\n'
ask SELLER_NAME "ФИО продавца" "$(env_get SELLER_NAME)"
def_status=$(env_get SELLER_STATUS); [ "$def_status" = "ИП" ] && [ -z "$(env_get SELLER_INN)" ] && def_status="самозанятый (плательщик НПД)"
ask SELLER_STATUS "Статус" "${def_status:-самозанятый (плательщик НПД)}"
ask SELLER_INN "ИНН" "$(env_get SELLER_INN)"
ask SELLER_EMAIL "E-mail для обращений и претензий" "$(env_get SELLER_EMAIL)"
env_set SELLER_NAME "$SELLER_NAME"; env_set SELLER_STATUS "$SELLER_STATUS"
env_set SELLER_INN "$SELLER_INN"; env_set SELLER_EMAIL "$SELLER_EMAIL"
[ -n "$SELLER_NAME" ] && [ -n "$SELLER_INN" ] && [ -n "$SELLER_EMAIL" ] || warn "без ФИО, ИНН и e-mail в оферте будут пустые места — до приёма денег заполни их и запусти скрипт ещё раз"

pi=$(env_get PAYMENT_INFO)
if [ -n "$SUPPORT_CONTACT" ] && { [ -z "$pi" ] || [[ $pi == "Оплата скоро появится"* ]]; }; then
  env_set PAYMENT_INFO "Оплата переводом: напиши ${SUPPORT_CONTACT} — пришлём реквизиты и чек, доступ откроем в течение дня."
fi
env_set BOT_MODE polling
# страна сервера — попадает в политику и согласие: не Россия означает тестовый запуск с трансграничной передачей
cc=$(curl -fsS --max-time 8 https://ipinfo.io/country 2>/dev/null | tr -d '[:space:]' || true)
case "$cc" in
  RU) def_country="Россия" ;; NL) def_country="Нидерланды" ;; DE) def_country="Германия" ;; FI) def_country="Финляндия" ;;
  KZ) def_country="Казахстан" ;; *) def_country=$(env_get SERVER_COUNTRY) ;;
esac
ask SERVER_COUNTRY "Страна, где стоит этот сервер (для политики обработки данных)" "${def_country:-Россия}"
env_set SERVER_COUNTRY "$SERVER_COUNTRY"
case "$SERVER_COUNTRY" in
  Россия|россия|РФ) ;;
  *) warn "сервер не в России: это допустимо только для теста — политика и согласие честно скажут, где данные. До продаж перенеси базу в РФ (LAUNCH.md)" ;;
esac
env_set SPRINT_FOR_EVERYONE "$(env_get SPRINT_FOR_EVERYONE | grep -E '^(true|false)$' || echo true)"
chmod 600 "$APP/.env"
ok "настройки записаны в $APP/.env (доступ только у root)"

# --------------------------------------------------------------------------- 5. домен и Telegram

say "5/8 Проверяю домен и связь с Telegram"
ip=$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 8 https://ipv4.icanhazip.com 2>/dev/null || hostname -I | awk '{print $1}')
ip=${ip//[[:space:]]/}
dns=$(dig +short A "$DOMAIN" @1.1.1.1 2>/dev/null | tail -n1 || true)
[ -z "$dns" ] && dns=$(dig +short A "$DOMAIN" 2>/dev/null | tail -n1 || true)
if [ "$dns" = "$ip" ]; then
  ok "$DOMAIN смотрит на этот сервер ($ip)"
else
  warn "$DOMAIN смотрит на «${dns:-никуда}», а IP сервера — $ip"
  tty "  В DNS у регистратора домена добавь A-запись: $DOMAIN → $ip (обновляется от 5 минут до пары часов)."$'\n'
  tty "  Без этого сайт и мини-приложение не откроются: HTTPS-сертификат выдаётся только на правильный адрес."$'\n'
  if [ "$NONINTERACTIVE" != 1 ] && yes_no "Подождать, пока DNS обновится (проверяю раз в 30 секунд до 20 минут)?" y; then
    for _ in $(seq 1 40); do
      sleep 30
      dns=$(dig +short A "$DOMAIN" @1.1.1.1 2>/dev/null | tail -n1 || true)
      [ "$dns" = "$ip" ] && { ok "DNS обновился"; break; }
      tty "."
    done
    [ "$dns" = "$ip" ] || warn "DNS ещё не обновился — продолжаю: сертификат Caddy получит сам, когда домен заработает"
  fi
fi

tg_check() {  # $1 — адрес Bot API; печатает username бота или ничего
  curl -fsS --max-time 12 "$1/bot$BOT_TOKEN/getMe" 2>/dev/null | jq -r 'select(.ok==true) | .result.username' 2>/dev/null || true
}
use_relay() {  # $1 — домен релея; записывает его в .env, если через него бот на связи
  local d=${1#https://}; d=${d%%/*}
  bot_name=$(tg_check "https://$d")
  if [ -n "$bot_name" ]; then env_set TELEGRAM_API_BASE "https://$d"; ok "Telegram подключён через релей $d"
  else warn "через релей $d бот не отвечает — проверь, что релей настроен на IP этого сервера ($ip) и что tg-домен смотрит на релей"; fi
}
api_base=$(env_get TELEGRAM_API_BASE)
bot_name=""
if [ -z "$api_base" ]; then
  # с февраля 2026 Telegram в России замедляют (сильнее всего — голосовые и файлы), поэтому релей стоит
  # подключить сразу, даже если напрямую сервер Telegram видит
  ask RELAY_DOMAIN "Домен релея к Telegram, если он настроен (например tg.dochitka.ru), или Enter — напрямую" ""
  [ -n "${RELAY_DOMAIN:-}" ] && use_relay "$RELAY_DOMAIN"
fi
if [ -z "$bot_name" ]; then
  api_base=$(env_get TELEGRAM_API_BASE)
  bot_name=$(tg_check "${api_base:-https://api.telegram.org}")
  if [ -z "$bot_name" ] && [ -z "$api_base" ]; then
    warn "Сервер не достаёт до api.telegram.org — нужен релей за рубежом (шаг 6 в PILOT.md, 15 минут), потом запусти скрипт ещё раз"
  elif [ -z "$bot_name" ]; then
    warn "релей ${api_base} не отвечает или токен неверный — проверь релей и BOT_TOKEN"
  fi
fi
[ -n "$bot_name" ] && ok "бот @$bot_name на связи" || warn "бот пока не на связи — сайт запущу, а бот заработает, как только появится связь с Telegram"

# --------------------------------------------------------------------------- 6. запуск

say "6/8 Собираю и запускаю (первый раз — 5–10 минут)"
mkdir -p "$APP/data" "$APP/backups"
(cd "$APP" && bash scripts/deploy.sh) || die "Запуск не удался. Логи: cd $APP && docker compose logs --tail 100 app"

# --------------------------------------------------------------------------- 7. бэкапы

say "7/8 Ежедневный бэкап базы"
chmod 700 "$APP/backups"
# на новом сервере crontab пустой: crontab -l и grep -v возвращают ошибку — это не повод останавливать установку
cron_line="30 3 * * * cd $APP && bash scripts/backup.sh >> $APP/backups/cron.log 2>&1"
{ crontab -l 2>/dev/null | grep -v 'scripts/backup.sh' || true; echo "$cron_line"; } | crontab -
ok "каждую ночь в 03:30, хранятся 14 последних (папка $APP/backups)"

# --------------------------------------------------------------------------- 8. автообновление из GitHub

say "8/8 Автообновление: GitHub сам выкладывает новую версию после каждого изменения кода"
ci_key=/root/.ssh/dochitka_ci
if grep -q 'dochitka-ci' /root/.ssh/authorized_keys 2>/dev/null; then
  ok "уже подключено (ключ dochitka-ci в authorized_keys)"
elif yes_no "Подключить автообновление из GitHub (рекомендую)?" y; then
  install -d -m 700 /root/.ssh
  [ -f "$ci_key" ] || ssh-keygen -q -t ed25519 -N "" -C "dochitka-ci" -f "$ci_key"
  # этот ключ умеет только одно — запустить обновление (scripts/ci_deploy.sh), ни консоли, ни туннелей
  echo "command=\"bash $APP/scripts/ci_deploy.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat "$ci_key.pub")" >>/root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  host_key=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)
  tty $'\n  GitHub → репозиторий → Settings → Secrets and variables → Actions → New repository secret.\n  Создай три секрета:\n\n'
  tty "  DEPLOY_HOST          = $ip"$'\n'
  tty "  DEPLOY_KNOWN_HOSTS   = $ip $host_key"$'\n'
  tty "  DEPLOY_SSH_KEY       = всё между строками ----- ниже, включая BEGIN и END:"$'\n\n'
  if [ -n "$TTY" ]; then cat "$ci_key" >"$TTY"; fi  # закрытый ключ — только на экран, не в журнал
  tty $'\n  Ключ можно показать снова: cat /root/.ssh/dochitka_ci\n'
  ok "ключ для GitHub создан: он может только запускать обновление"
fi

# --------------------------------------------------------------------------- итог

say "Готово"
out "  Сайт:            https://$DOMAIN"
[ -n "${bot_name:-}" ] && out "  Бот:             https://t.me/$bot_name"
out "  Мини-приложение: https://$DOMAIN/app/"
out "  Настройки:       $APP/.env   (поменял — запусти: cd $APP && bash scripts/deploy.sh)"
out "  Логи:            cd $APP && bash scripts/logs.sh"
out "  Диагностика:     cd $APP && docker compose exec app python -m scripts.doctor"
out ""
out "  Что осталось сделать руками — в LAUNCH.md: Mini App в @BotFather, самозанятость,"
out "  уведомление в Роскомнадзор, первые тестеры. Оплату ЮKassa можно подключить позже."
