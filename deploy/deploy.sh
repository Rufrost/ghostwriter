#!/usr/bin/env bash
# Установка Ghostwriter на чистый Linux-сервер (Ubuntu 22.04/24.04, Debian 12).
#
# Запуск от root из распакованного каталога проекта:
#   DOMAIN=bot.example.com EMAIL=you@example.com bash deploy/deploy.sh
#
# Переменные:
#   DOMAIN        — домен для HTTPS (обязателен, если нужен Caddy)
#   EMAIL         — email для Let's Encrypt
#   APP_DIR       — куда ставить (по умолчанию /opt/ghostwriter)
#   WITH_CADDY    — 1 (по умолчанию) — поставить Caddy и получить сертификат
#   WITH_DOCKER   — 1 — не ставить systemd-сервис, только подготовить каталог
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ghostwriter}"
SERVICE_USER="${SERVICE_USER:-ghostwriter}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
WITH_CADDY="${WITH_CADDY:-1}"
WITH_DOCKER="${WITH_DOCKER:-0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "запускать нужно от root (sudo -i)"
[[ -f "$SRC_DIR/app/main.py" ]] || die "запустите скрипт из каталога проекта"

# ---------------------------------------------------------------- 1. пакеты
log "Устанавливаю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates rsync \
    >/dev/null

# ---------------------------------------------------------------- 2. пользователь
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "Создаю пользователя $SERVICE_USER"
    useradd -r -m -d "$APP_DIR" -s /usr/sbin/nologin "$SERVICE_USER"
fi

# ---------------------------------------------------------------- 3. файлы
log "Копирую файлы в $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude '.venv' --exclude 'data' --exclude '.git' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.env' --exclude '*.tar.gz' \
    "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# ---------------------------------------------------------------- 4. .env
if [[ ! -f "$APP_DIR/.env" ]]; then
    log "Создаю .env из шаблона — отредактируйте его перед запуском"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    if [[ -n "$DOMAIN" ]]; then
        sed -i "s|^THREADS_REDIRECT_URI=.*|THREADS_REDIRECT_URI=https://$DOMAIN/auth/threads/callback|" "$APP_DIR/.env"
        sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$DOMAIN|" "$APP_DIR/.env"
        sed -i "s|^WEB_AUTH_KEY=.*|WEB_AUTH_KEY=$(openssl rand -hex 24)|" "$APP_DIR/.env"
    fi
    chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    NEED_ENV=1
else
    NEED_ENV=0
fi

# ---------------------------------------------------------------- 5. venv + зависимости
log "Ставлю зависимости в venv"
if [[ "$WITH_DOCKER" != "1" ]]; then
    sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
    sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
    sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

# ---------------------------------------------------------------- 6. systemd
if [[ "$WITH_DOCKER" != "1" ]]; then
    log "Ставлю systemd-сервис"
    sed "s|/opt/ghostwriter|$APP_DIR|g; s|^User=.*|User=$SERVICE_USER|; s|^Group=.*|Group=$SERVICE_USER|" \
        "$APP_DIR/deploy/ghostwriter.service" > /etc/systemd/system/ghostwriter.service
    systemctl daemon-reload
    systemctl enable ghostwriter >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- 7. Caddy
if [[ "$WITH_CADDY" == "1" && -n "$DOMAIN" ]]; then
    if ! command -v caddy >/dev/null 2>&1; then
        log "Устанавливаю Caddy (автоматический HTTPS)"
        apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
        curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
            | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
            | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        apt-get update -qq
        apt-get install -y -qq caddy >/dev/null
    fi
    log "Настраиваю Caddy для $DOMAIN"
    if [[ -n "$EMAIL" ]]; then
        GLOBAL=$'{\n\temail '"$EMAIL"$'\n}\n'
    else
        GLOBAL=""
    fi
    cat > /etc/caddy/Caddyfile <<EOF
$GLOBAL
$DOMAIN {
    reverse_proxy 127.0.0.1:8080
    encode gzip
}
EOF
    systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

# ---------------------------------------------------------------- 8. финал
echo
log "Готово."
if [[ "$NEED_ENV" == "1" ]]; then
    cat <<EOF

  ⚠️  Заполните секреты, иначе сервис не заработает:

      nano $APP_DIR/.env

  Обязательные строки:
      TELEGRAM_BOT_TOKEN=...
      TELEGRAM_ADMIN_IDS=...
      LLM_API_KEY=...
      THREADS_APP_ID=...
      THREADS_APP_SECRET=...
      PUBLIC_BASE_URL=https://${DOMAIN:-ВАШ-ДОМЕН}
      THREADS_REDIRECT_URI=https://${DOMAIN:-ВАШ-ДОМЕН}/auth/threads/callback

  Затем:
      systemctl restart ghostwriter
      systemctl status ghostwriter --no-pager
      journalctl -u ghostwriter -f

EOF
else
    echo "  .env уже существует — не трогаю. Перезапуск: systemctl restart ghostwriter"
fi

if [[ -n "$DOMAIN" ]]; then
    echo "  Проверка снаружи: https://$DOMAIN/health"
    echo "  DNS-запись A для $DOMAIN должна указывать на IP этого сервера."
fi
