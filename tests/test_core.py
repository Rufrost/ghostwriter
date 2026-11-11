import json
import sqlite3

import pytest

from app.agents.writer import Writer
from app.config import Settings, distribute_posting_times, parse_posting_times
from app.db import Store
from app.llm import LLMClient, LLMError, extract_json
from app.runtime import MAX_MULTIPOST_MODELS, Runtime


def test_distribute_posting_times():
    assert distribute_posting_times(1, "09:00", "19:00") == [(14, 0)]
    assert distribute_posting_times(3, "09:00", "19:00") == [
        (9, 0), (14, 0), (19, 0)
    ]
    assert distribute_posting_times(5, "09:00", "19:00") == [
        (9, 0), (11, 30), (14, 0), (16, 30), (19, 0)
    ]
    with pytest.raises(ValueError):
        distribute_posting_times(0)
    with pytest.raises(ValueError):
        distribute_posting_times(11)
    with pytest.raises(ValueError):
        distribute_posting_times(3, "19:00", "09:00")


def test_parse_posting_times_ok():
    assert parse_posting_times("09:00, 13:00,19:30") == [(9, 0), (13, 0), (19, 30)]
    assert parse_posting_times("23.15") == [(23, 15)]
    assert parse_posting_times("09:00,09:00") == [(9, 0)]  # дедупликация


@pytest.mark.parametrize("raw", ["", "25:00", "09:70", "abc"])
def test_parse_posting_times_bad(raw):
    with pytest.raises(ValueError):
        parse_posting_times(raw)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Вот результат: {"a": 3} — готово') == {"a": 3}
    with pytest.raises(Exception):
        extract_json("совсем не json")


def test_store_and_runtime(tmp_path):
    store = Store(tmp_path / "test.db")
    store.init()
    rt = Runtime(store)

    # значения из .env / дефолтов
    assert rt.get_int("max_posts_per_day") == 10
    assert rt.get_bool("auto_approve") is False

    # переопределение из БД, включая типы
    rt.set("auto_approve", True)
    rt.set("posting_times", "10:15, 20:45")
    rt.set("max_posts_per_day", 3)
    assert rt.get_bool("auto_approve") is True
    assert rt.posting_times == [(10, 15), (20, 45)]
    assert rt.get_int("max_posts_per_day") == 3

    # сброс — возврат к значению по умолчанию
    rt.reset("max_posts_per_day")
    assert rt.get_int("max_posts_per_day") == 10


def test_store_post_queue_and_publication_history(tmp_path):
    store = Store(tmp_path / "t.db")
    store.init()

    first = store.create_post("первый", status="queued", source="user_queue")
    second = store.create_post("второй", status="queued", source="user_queue")
    assert store.count_queued_posts() == 2
    assert [row["id"] for row in store.queued_posts()] == [first, second]
    assert store.next_queued_post()["id"] == first

    assert store.delete_queued_post(first)
    assert store.count_queued_posts() == 1
    store.update_post(second, status="published", published_at="2026-11-08T10:00:00+00:00")
    assert store.count_published_since("2026-11-08T00:00:00+00:00") == 1
    assert store.last_published_at() == "2026-11-08T10:00:00+00:00"

def test_stale_publishing_failed(tmp_path):
    store = Store(tmp_path / "t.db")
    store.init()
    pid = store.create_post("x", status="publishing")
    assert store.fail_stale_publishing() == 1
    assert store.get_post(pid)["status"] == "failed"


def test_writer_budget_and_truncate(tmp_path):
    store = Store(tmp_path / "t.db")
    store.init()
    rt = Runtime(store)
    w = Writer(store, rt)

    # ru: 500 байт → около 240 символов
    assert 200 <= w._char_budget() <= 250

    long_text = ("Очень длинное предложение про продуктивность. " * 30).strip()
    cut = w._truncate(long_text)
    assert len(cut.encode()) <= 500
    assert cut.endswith(".")

    rt.set("brand_language", "en")
    assert w._char_budget() > 300


def test_clean_tag():
    assert Writer._clean_tag("#искусственный.интеллект&ко") == "искусственныйинтеллектко"
    assert len(Writer._clean_tag("x" * 80)) == 50


def test_writer_parse_fallbacks(tmp_path):
    store = Store(tmp_path / "t.db")
    store.init()
    w = Writer(store, Runtime(store))

    text, tag = w._parse(json.dumps({"text": "привет", "topic_tag": "#ии"}))
    assert (text, tag) == ("привет", "#ии")

    # модель ответила простым текстом
    text, tag = w._parse("просто текст без json")
    assert text == "просто текст без json"
    assert tag == ""

    # регрессия: пустой ответ и ответ без поля text не должны давать пустой пост
    with pytest.raises(LLMError):
        w._parse("")
    with pytest.raises(LLMError):
        w._parse("   ")
    with pytest.raises(LLMError):
        w._parse(json.dumps({"topic_tag": "ии"}))


async def test_llm_retries_when_reasoning_eats_tokens():
    """Модель потратила max_tokens на рассуждения → content пустой → повтор с большим лимитом."""
    import httpx

    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        limit = json.loads(request.content)["max_tokens"]
        seen.append(limit)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
                    ],
                    "usage": {"completion_tokens_details": {"reasoning_tokens": 900}},
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"text": "готово"}'}}]}
        )

    llm = LLMClient(
        provider="openai",
        base_url="https://example.test/v1",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    text = await llm._openai(
        system="s", user="u", model="m", temperature=0.8, max_tokens=900, json_mode=True
    )
    assert text == '{"text": "готово"}'
    assert seen == [900, 4000]


async def test_llm_raises_on_persistently_empty_content():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {},
            },
        )

    llm = LLMClient(
        provider="openai",
        base_url="https://example.test/v1",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError, match="пустой"):
        await llm._openai(
            system="s", user="u", model="m", temperature=0.8, max_tokens=900, json_mode=True
        )


def test_author_settings_are_runtime_editable(tmp_path):
    store = Store(tmp_path / "author.db")
    store.init()
    rt = Runtime(store)

    rt.set("writer_instructions", "Всегда добавляй конкретный пример")
    rt.set("writer_model", "author-model")

    assert rt.get_str("writer_instructions") == "Всегда добавляй конкретный пример"
    assert rt.get_str("writer_model") == "author-model"
    with pytest.raises(KeyError):
        rt.get("strategist_model")


def test_multipost_models_are_parsed_deduplicated_and_capped(tmp_path):
    store = Store(tmp_path / "multi.db")
    store.init()
    rt = Runtime(store)

    assert rt.multipost_models == []

    rt.set("multipost_models", "a/b, c-d\n e; a/b")
    assert rt.multipost_models == ["a/b", "c-d", "e"]

    rt.set(
        "multipost_models",
        ",".join(f"provider/model-{index}" for index in range(MAX_MULTIPOST_MODELS + 4)),
    )
    assert len(rt.multipost_models) == MAX_MULTIPOST_MODELS

    rt.reset("multipost_models")
    assert rt.multipost_models == []


def test_init_removes_legacy_topics_and_preserves_posts(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO settings VALUES ('strategist_model', 'old-model', '2026-01-01');
            INSERT INTO settings VALUES ('metrics_lookback_days', '14', '2026-01-01');

            CREATE TABLE topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                angle TEXT,
                rationale TEXT,
                source TEXT NOT NULL DEFAULT 'legacy',
                status TEXT NOT NULL DEFAULT 'new',
                score REAL,
                created_at TEXT NOT NULL,
                used_at TEXT
            );
            INSERT INTO topics(title, created_at) VALUES ('Старая тема', '2026-01-01');

            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER REFERENCES topics(id),
                topic TEXT,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                threads_media_id TEXT,
                threads_permalink TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                scheduled_for TEXT,
                published_at TEXT,
                metrics_json TEXT,
                metrics_updated_at TEXT,
                source TEXT NOT NULL DEFAULT 'auto'
            );
            INSERT INTO posts(topic_id, topic, text, status, created_at, source)
                VALUES (1, 'Старая тема', 'Сохранённый пост', 'published',
                        '2026-01-01', 'generated');

            CREATE TABLE stock_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                storage_name TEXT NOT NULL UNIQUE,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                telegram_file_id TEXT,
                caption TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                usage_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            );
            INSERT INTO stock_images(
                token, storage_name, mime_type, size_bytes, width, height, sha256, created_at
            ) VALUES ('token', 'image.png', 'image/png', 10, 1, 1, 'hash', '2026-01-01');

            CREATE TABLE post_images (
                post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                image_id INTEGER NOT NULL REFERENCES stock_images(id),
                assigned_at TEXT NOT NULL
            );
            INSERT INTO post_images VALUES (1, 1, '2026-01-01');
            """
        )

    store = Store(db_path)
    store.init()

    with store.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(posts)")
        }
        assert "topics" not in tables
        assert "topic_id" not in columns
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    post = store.get_post(1)
    assert post is not None
    assert post["topic"] == "Старая тема"
    assert post["text"] == "Сохранённый пост"
    assert store.get_post_image(1)["id"] == 1
    assert store.get_setting("strategist_model") is None
    assert store.get_setting("metrics_lookback_days") is None


@pytest.mark.parametrize("value", [1, 500, 501, 1500])
def test_max_post_chars_settings_accept_valid_values(value):
    assert Settings(_env_file=None, max_post_chars=value).max_post_chars == value


@pytest.mark.parametrize("value", [0, -1, 1501])
def test_max_post_chars_settings_reject_invalid_values(value):
    with pytest.raises(ValueError, match="1..1500"):
        Settings(_env_file=None, max_post_chars=value)


@pytest.mark.parametrize("value, expected", [(0, 1), (501, 501), (1500, 1500), (1501, 1500)])
def test_runtime_post_limit(tmp_path, value, expected):
    store = Store(tmp_path / "limit.db")
    store.init()
    rt = Runtime(store)
    rt.set("max_post_chars", value)
    assert rt.max_chars == expected


def test_writer_supports_extended_post_limit(tmp_path):
    store = Store(tmp_path / "writer-limit.db")
    store.init()
    rt = Runtime(store)
    rt.set("max_post_chars", 1500)
    writer = Writer(store, rt)

    assert writer._char_budget("ru") == 740
    assert writer._char_budget("en") == 1200
    text = "Я" * 750
    assert writer._truncate(text) == text
    assert len(writer._truncate(text + "Я").encode("utf-8")) <= 1500
