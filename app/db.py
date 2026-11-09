"""Хранилище на SQLite: настройки, посты, события, OAuth-state.

Соединение открывается на каждую операцию (WAL + busy timeout) — этого
достаточно для нагрузки бота и планировщика и не требует пула.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    topic              TEXT,
    text               TEXT NOT NULL,
    status             TEXT NOT NULL,  -- queued | draft | pending_approval | approved
                                       -- | publishing | published | published_dry | failed | rejected
    threads_media_id   TEXT,
    threads_permalink  TEXT,
    error              TEXT,
    created_at         TEXT NOT NULL,
    scheduled_for      TEXT,
    published_at       TEXT,
    metrics_json       TEXT,
    metrics_updated_at TEXT,
    source             TEXT NOT NULL DEFAULT 'auto'  -- generated | user_queue | rephrased | schedule
);

CREATE INDEX IF NOT EXISTS idx_posts_status     ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_published  ON posts(published_at);

CREATE TABLE IF NOT EXISTS project_knowledge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_knowledge_active
    ON project_knowledge(active);

CREATE TABLE IF NOT EXISTS stock_images (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token            TEXT NOT NULL UNIQUE,
    storage_name     TEXT NOT NULL UNIQUE,
    mime_type        TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    width            INTEGER NOT NULL,
    height           INTEGER NOT NULL,
    sha256           TEXT NOT NULL UNIQUE,
    telegram_file_id TEXT,
    caption          TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    usage_count      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    last_used_at     TEXT
);

CREATE TABLE IF NOT EXISTS post_images (
    post_id     INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
    image_id    INTEGER NOT NULL REFERENCES stock_images(id),
    assigned_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stock_images_active ON stock_images(active);
CREATE INDEX IF NOT EXISTS idx_post_images_image    ON post_images(image_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state      TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    """UTC-время в формате, сравнимом со SQLite datetime() — с пробелом, а не 'T'.

    Это важно: SQLite сравнивает такие строки лексикографически, и 'T' больше
    пробела, из-за чего ISO-строки с 'T' некорректно сравниваются с
    datetime('now') внутри одного дня.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def to_db_ts(dt: datetime) -> str:
    """datetime → строка того же формата, что и utcnow()."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else settings.db_file

    # ---------- низкий уровень ----------
    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._remove_legacy_topics(conn)

    @staticmethod
    def _remove_legacy_topics(conn: sqlite3.Connection) -> None:
        """Удаляет старую таблицу тем и связь posts.topic_id, сохраняя посты."""
        post_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(posts)")
        }
        has_topic_id = "topic_id" in post_columns
        has_topics = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='topics'"
        ).fetchone() is not None

        if has_topic_id:
            # SQLite не умеет безопасно убрать колонку с внешним ключом на всех
            # поддерживаемых версиях, поэтому перестраиваем posts и post_images.
            conn.commit()
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;

                    CREATE TABLE posts_new (
                        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                        topic              TEXT,
                        text               TEXT NOT NULL,
                        status             TEXT NOT NULL,
                        threads_media_id   TEXT,
                        threads_permalink  TEXT,
                        error              TEXT,
                        created_at         TEXT NOT NULL,
                        scheduled_for      TEXT,
                        published_at       TEXT,
                        metrics_json       TEXT,
                        metrics_updated_at TEXT,
                        source             TEXT NOT NULL DEFAULT 'auto'
                    );

                    INSERT INTO posts_new(
                        id, topic, text, status, threads_media_id,
                        threads_permalink, error, created_at, scheduled_for,
                        published_at, metrics_json, metrics_updated_at, source
                    )
                    SELECT
                        id, topic, text, status, threads_media_id,
                        threads_permalink, error, created_at, scheduled_for,
                        published_at, metrics_json, metrics_updated_at, source
                    FROM posts;

                    CREATE TEMP TABLE post_images_backup AS
                        SELECT post_id, image_id, assigned_at FROM post_images;
                    DROP TABLE post_images;
                    DROP TABLE posts;
                    ALTER TABLE posts_new RENAME TO posts;

                    CREATE INDEX idx_posts_status ON posts(status);
                    CREATE INDEX idx_posts_published ON posts(published_at);

                    CREATE TABLE post_images (
                        post_id     INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                        image_id    INTEGER NOT NULL REFERENCES stock_images(id),
                        assigned_at TEXT NOT NULL
                    );
                    INSERT INTO post_images(post_id, image_id, assigned_at)
                        SELECT post_id, image_id, assigned_at FROM post_images_backup;
                    DROP TABLE post_images_backup;
                    CREATE INDEX idx_post_images_image ON post_images(image_id);

                    DROP TABLE IF EXISTS topics;
                    COMMIT;
                    """
                )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
        elif has_topics:
            conn.execute("DROP TABLE topics")

        # Эти значения принадлежали удалённой роли и больше не читаются.
        conn.execute(
            """DELETE FROM settings
               WHERE key LIKE 'strategist_%' OR key='metrics_lookback_days'"""
        )

    # ---------- настройки ----------
    def get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                               updated_at=excluded.updated_at
                """,
                (key, str(value), utcnow()),
            )

    def delete_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))

    def all_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---------- посты ----------
    def create_post(
        self,
        text: str,
        *,
        topic: str = "",
        status: str = "draft",
        scheduled_for: str | None = None,
        source: str = "auto",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO posts(topic, text, status, scheduled_for, source, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (topic, text, status, scheduled_for, source, utcnow()),
            )
            return int(cur.lastrowid)

    def get_post(self, post_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def update_post(self, post_id: int, **fields: Any) -> None:
        allowed = {
            "text", "status", "threads_media_id", "threads_permalink", "error",
            "scheduled_for", "published_at", "metrics_json", "metrics_updated_at",
            "topic", "source",
        }
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        cols = ", ".join(f"{k}=?" for k in data)
        with self.connect() as conn:
            conn.execute(f"UPDATE posts SET {cols} WHERE id=?", (*data.values(), post_id))

    def recent_posts(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def queued_posts(self, limit: int = 50) -> list[sqlite3.Row]:
        """Пользовательские посты, ожидающие ближайших слотов расписания."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM posts WHERE status='queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()

    def next_queued_post(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM posts WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()

    def count_queued_posts(self) -> int:
        with self.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM posts WHERE status='queued'"
                ).fetchone()["c"]
            )

    def delete_queued_post(self, post_id: int) -> bool:
        """Удаляет только пост, который ещё находится в пользовательской очереди."""
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM posts WHERE id=? AND status='queued'", (post_id,)
            )
            return cur.rowcount > 0

    def delete_post(
        self, post_id: int, *, statuses: tuple[str, ...] | None = None
    ) -> bool:
        """Удаляет пост; со statuses — только если он в одном из этих статусов."""
        with self.connect() as conn:
            if statuses:
                marks = ",".join("?" for _ in statuses)
                cur = conn.execute(
                    f"DELETE FROM posts WHERE id=? AND status IN ({marks})",
                    (post_id, *statuses),
                )
            else:
                cur = conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
            return cur.rowcount > 0

    def posts_for_analysis(self, days: int = 14, limit: int = 40) -> list[sqlite3.Row]:
        """Опубликованные посты за период — вход для аналитика."""
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM posts
                   WHERE status IN ('published','published_dry')
                     AND published_at IS NOT NULL
                     AND published_at >= datetime('now', ?)
                   ORDER BY published_at DESC LIMIT ?""",
                (f"-{days} days", limit),
            ).fetchall()

    def posts_pending_metrics(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM posts
                   WHERE status='published' AND threads_media_id IS NOT NULL
                     AND (metrics_updated_at IS NULL
                          OR metrics_updated_at < datetime('now','-3 hours'))
                   ORDER BY published_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def count_published_since(self, iso_ts: str) -> int:
        with self.connect() as conn:
            return int(
                conn.execute(
                    """SELECT COUNT(*) AS c FROM posts
                       WHERE status IN ('published','published_dry')
                         AND published_at >= ?""",
                    (iso_ts,),
                ).fetchone()["c"]
            )

    def last_published_at(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT published_at FROM posts
                   WHERE status IN ('published','published_dry')
                   ORDER BY published_at DESC LIMIT 1"""
            ).fetchone()
        return row["published_at"] if row else None

    def latest_pending_approval(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM posts WHERE status='pending_approval' ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def fail_stale_publishing(self) -> int:
        """Посты, зависшие в publishing после падения процесса, помечаются failed."""
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE posts SET status='failed', error=COALESCE(error, 'прервано перезапуском')
                   WHERE status='publishing'"""
            )
            return cur.rowcount

    # ---------- редактируемая память о проекте ----------
    def add_project_knowledge(self, title: str, content: str) -> int:
        title = title.strip()[:120]
        content = content.strip()[:3500]
        if not title or not content:
            raise ValueError("Название и текст записи не могут быть пустыми")
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO project_knowledge(title,content,active,created_at,updated_at)
                   VALUES(?,?,1,?,?)""",
                (title, content, now, now),
            )
            return int(cur.lastrowid)

    def get_project_knowledge(self, knowledge_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM project_knowledge WHERE id=?", (knowledge_id,)
            ).fetchone()

    def list_project_knowledge(
        self, *, active: bool | None = None, limit: int = 50
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if active is None:
                return conn.execute(
                    "SELECT * FROM project_knowledge ORDER BY id LIMIT ?", (limit,)
                ).fetchall()
            return conn.execute(
                """SELECT * FROM project_knowledge WHERE active=?
                   ORDER BY id LIMIT ?""",
                (1 if active else 0, limit),
            ).fetchall()

    def update_project_knowledge(
        self, knowledge_id: int, *, title: str, content: str
    ) -> bool:
        title = title.strip()[:120]
        content = content.strip()[:3500]
        if not title or not content:
            raise ValueError("Название и текст записи не могут быть пустыми")
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE project_knowledge
                   SET title=?, content=?, updated_at=? WHERE id=?""",
                (title, content, utcnow(), knowledge_id),
            )
            return cur.rowcount > 0

    def set_project_knowledge_active(self, knowledge_id: int, active: bool) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE project_knowledge SET active=?, updated_at=? WHERE id=?""",
                (1 if active else 0, utcnow(), knowledge_id),
            )
            return cur.rowcount > 0

    def delete_project_knowledge(self, knowledge_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM project_knowledge WHERE id=?", (knowledge_id,)
            )
            return cur.rowcount > 0

    def project_knowledge_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(CASE WHEN active=1 THEN 1 ELSE 0 END),0) AS active,
                          COALESCE(SUM(CASE WHEN active=1 THEN length(content) ELSE 0 END),0) AS chars
                   FROM project_knowledge"""
            ).fetchone()
        return {
            "total": int(row["total"]),
            "active": int(row["active"]),
            "chars": int(row["chars"]),
        }

    def project_knowledge_context(self, max_chars: int = 12000) -> str:
        """Активные знания для промптов, с жёстким ограничением размера."""
        rows = self.list_project_knowledge(active=True, limit=100)
        blocks: list[str] = []
        used = 0
        for row in rows:
            block = f"[{row['title']}]\n{str(row['content']).strip()}"
            separator = 2 if blocks else 0
            remaining = max_chars - used - separator
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = block[:remaining].rstrip()
            if block:
                blocks.append(block)
                used += len(block) + separator
            if used >= max_chars:
                break
        return "\n\n".join(blocks)

    # ---------- постоянный сток изображений ----------
    def add_stock_image(
        self,
        *,
        token: str,
        storage_name: str,
        mime_type: str,
        size_bytes: int,
        width: int,
        height: int,
        sha256: str,
        telegram_file_id: str = "",
        caption: str = "",
    ) -> tuple[int, bool]:
        """Добавляет изображение. Возвращает (id, создано); дубликаты не размножает."""
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM stock_images WHERE sha256=?", (sha256,)
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            try:
                cur = conn.execute(
                    """INSERT INTO stock_images(
                           token, storage_name, mime_type, size_bytes, width, height, sha256,
                           telegram_file_id, caption, created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        token, storage_name, mime_type, int(size_bytes), int(width), int(height),
                        sha256, telegram_file_id, caption.strip()[:1000], utcnow(),
                    ),
                )
            except sqlite3.IntegrityError:
                # Два изображения из Telegram-альбома могли обрабатываться одновременно.
                existing = conn.execute(
                    "SELECT id FROM stock_images WHERE sha256=?", (sha256,)
                ).fetchone()
                if existing:
                    return int(existing["id"]), False
                raise
            return int(cur.lastrowid), True

    def get_stock_image(self, image_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM stock_images WHERE id=?", (image_id,)
            ).fetchone()

    def get_stock_image_by_token(self, token: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM stock_images WHERE token=?", (token,)
            ).fetchone()

    def get_stock_image_by_hash(self, sha256: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM stock_images WHERE sha256=?", (sha256,)
            ).fetchone()

    def list_stock_images(
        self, *, active: bool | None = None, limit: int = 30
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if active is None:
                return conn.execute(
                    "SELECT * FROM stock_images ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return conn.execute(
                "SELECT * FROM stock_images WHERE active=? ORDER BY id DESC LIMIT ?",
                (1 if active else 0, limit),
            ).fetchall()

    def stock_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(CASE WHEN active=1 THEN 1 ELSE 0 END),0) AS active,
                          COALESCE(SUM(usage_count),0) AS uses
                   FROM stock_images"""
            ).fetchone()
        return {"total": int(row["total"]), "active": int(row["active"]), "uses": int(row["uses"])}

    def set_stock_image_active(self, image_id: int, active: bool) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE stock_images SET active=? WHERE id=?",
                (1 if active else 0, image_id),
            )
            return cur.rowcount > 0

    def assign_stock_image(self, post_id: int) -> sqlite3.Row | None:
        """Закрепляет за постом наименее использованное активное изображение."""
        with self.connect() as conn:
            existing = conn.execute(
                """SELECT s.* FROM stock_images s
                   JOIN post_images p ON p.image_id=s.id WHERE p.post_id=?""",
                (post_id,),
            ).fetchone()
            if existing:
                return existing
            image = conn.execute(
                """SELECT * FROM stock_images WHERE active=1
                   ORDER BY usage_count ASC,
                            CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END ASC,
                            last_used_at ASC, id ASC
                   LIMIT 1"""
            ).fetchone()
            if image is None:
                return None
            now = utcnow()
            conn.execute(
                "INSERT INTO post_images(post_id,image_id,assigned_at) VALUES(?,?,?)",
                (post_id, int(image["id"]), now),
            )
            conn.execute(
                """UPDATE stock_images
                   SET usage_count=usage_count+1, last_used_at=? WHERE id=?""",
                (now, int(image["id"])),
            )
            return conn.execute(
                "SELECT * FROM stock_images WHERE id=?", (int(image["id"]),)
            ).fetchone()

    def get_post_image(self, post_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT s.* FROM stock_images s
                   JOIN post_images p ON p.image_id=s.id WHERE p.post_id=?""",
                (post_id,),
            ).fetchone()

    # ---------- события ----------
    def log_event(self, message: str, level: str = "info") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(level, message, created_at) VALUES(?,?,?)",
                (level, message[:4000], utcnow()),
            )

    def recent_events(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    # ---------- OAuth ----------
    def add_oauth_state(self, state: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_states(state, created_at) VALUES(?,?)",
                (state, utcnow()),
            )

    def pop_oauth_state(self, state: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
            return cur.rowcount > 0


def metrics_dict(post_row: sqlite3.Row) -> dict[str, Any]:
    raw = post_row["metrics_json"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


BOT_STATE = None  # заполняется в main.py, чтобы сервис мог слать уведомления
