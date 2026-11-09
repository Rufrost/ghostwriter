#!/usr/bin/env bash
# Ежедневный бэкап SQLite и зеркальная копия постоянного медиастока.
# Ставить в cron:  0 3 * * *  /opt/ghostwriter/scripts/backup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ghostwriter}"
DB="$APP_DIR/data/ghostwriter.db"
DEST="$APP_DIR/backups"
KEEP="${KEEP:-14}"

[[ -f "$DB" ]] || exit 0
mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$DEST/ghostwriter-$STAMP.db"

"$APP_DIR/.venv/bin/python" - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PY

gzip -f "$OUT"

# Изображения постоянны: держим одну актуальную копию без ежедневных дублей.
if [[ -d "$APP_DIR/data/stock" ]]; then
    mkdir -p "$DEST/stock"
    rsync -a "$APP_DIR/data/stock/" "$DEST/stock/"
fi

find "$DEST" -name 'ghostwriter-*.db.gz' -mtime "+$KEEP" -delete
echo "$(date -Is) backup: $(basename "$OUT").gz"
