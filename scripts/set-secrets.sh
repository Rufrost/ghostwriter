#!/usr/bin/env bash
# Записывает секреты в .env и перезапускает сервис.
#
# Пример:
#   sudo bash scripts/set-secrets.sh \
#     TELEGRAM_BOT_TOKEN=123456:AA... \
#     TELEGRAM_ADMIN_IDS=111222333 \
#     LLM_API_KEY=sk-... \
#     THREADS_APP_ID=1234567890 \
#     THREADS_APP_SECRET=abcdef...
#
# Дополнительно можно передать:
#   LLM_PROVIDER=openai LLM_BASE_URL=https://api.deepseek.com/v1
#   WRITER_MODEL=deepseek-chat STRATEGIST_MODEL=deepseek-chat
#   POSTING_TIMES=09:00,13:00,19:00 AUTO_APPROVE=false
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/ghostwriter/.env}"
[[ $# -gt 0 ]] || { echo "Нужно передать хотя бы одну пару КЛЮЧ=значение"; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Нет файла $ENV_FILE"; exit 1; }

mask() {
    local v="$1"
    if [[ ${#v} -gt 10 ]]; then
        printf '%s…%s (%d симв.)' "${v:0:4}" "${v: -4}" "${#v}"
    else
        printf '%s' "$v"
    fi
}

for pair in "$@"; do
    case "$pair" in
        *=*) ;;
        *) echo "Неверный аргумент: $pair (ожидается КЛЮЧ=значение)"; exit 1 ;;
    esac

    key="${pair%%=*}"
    value="${pair#*=}"

    if [[ ! "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
        echo "Недопустимый ключ: $key"; exit 1
    fi
    case "$value" in
        *"|"*) echo "Значение для $key содержит '|' — недопустимо"; exit 1 ;;
    esac
    if [[ "$value" == *"
"* ]]; then
        echo "Значение для $key содержит перевод строки — недопустимо"; exit 1
    fi

    if grep -qE "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
    printf '  %s = %s\n' "$key" "$(mask "$value")"
done

chown ghostwriter:ghostwriter "$ENV_FILE" 2>/dev/null || true
chmod 600 "$ENV_FILE"

systemctl restart ghostwriter
sleep 3
if systemctl is-active --quiet ghostwriter; then
    echo "✅ Сервис перезапущен, статус: active"
    curl -s --max-time 5 http://127.0.0.1:8080/health || true
    echo
else
    echo "❌ Сервис не поднялся:"
    journalctl -u ghostwriter -n 25 --no-pager
    exit 1
fi
