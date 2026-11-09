
import httpx
import pytest

from app.db import Store, utcnow
from app.runtime import Runtime
from app.service import ContentService
from app.threads_api import (
    ThreadsAPIError,
    ThreadsClient,
    _metrics_to_dict,
    engagement_score,
    text_length,
)


def make_client(tmp_path, requests: list[httpx.Request]) -> ThreadsClient:
    store = Store(tmp_path / "t.db")
    store.init()
    rt = Runtime(store)
    rt.set("threads_access_token", "token123")
    rt.set("threads_user_id", "999")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/threads") and request.method == "POST":
            return httpx.Response(200, json={"id": "container-1"})
        if path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "media-1"})
        if path.endswith("/container-1") and request.method == "GET":
            return httpx.Response(200, json={"id": "container-1", "status": "FINISHED"})
        if path.endswith("/media-1"):
            return httpx.Response(
                200, json={"id": "media-1", "permalink": "https://threads.net/@u/post/media-1"}
            )
        if path.endswith("/insights"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": "views", "values": [{"value": 1000}]},
                        {"name": "likes", "values": [{"value": 50}]},
                        {"name": "replies", "values": [{"value": 4}]},
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found", "code": 100}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ThreadsClient(rt, http=http)


async def test_publish_text_two_step(tmp_path):
    requests: list[httpx.Request] = []
    client = make_client(tmp_path, requests)

    media_id, permalink = await client.publish_text("Привет, Threads!", topic_tag="#ии")
    await client.http.aclose()

    assert media_id == "media-1"
    assert permalink == "https://threads.net/@u/post/media-1"

    create, publish, _permalink = requests[0], requests[1], requests[2]
    assert create.url.path == "/v1.0/999/threads"
    body = create.content.decode()
    assert "media_type=TEXT" in body
    assert "access_token=token123" in body
    assert "topic_tag=%D0%B8%D0%B8" in body  # «ии» url-encoded

    assert publish.url.path == "/v1.0/999/threads_publish"
    assert "creation_id=container-1" in publish.content.decode()


async def test_publish_image_two_step(tmp_path):
    requests: list[httpx.Request] = []
    client = make_client(tmp_path, requests)

    media_id, permalink = await client.publish_image(
        "Пост со скриншотом", "https://example.test/image.png"
    )
    await client.http.aclose()

    assert media_id == "media-1"
    assert permalink == "https://threads.net/@u/post/media-1"
    create = requests[0]
    body = create.content.decode()
    assert "media_type=IMAGE" in body
    assert "image_url=https%3A%2F%2Fexample.test%2Fimage.png" in body
    assert any(r.url.path.endswith("/container-1") for r in requests)



async def test_create_text_container_rejects_long_text(tmp_path):
    client = make_client(tmp_path, [])
    with pytest.raises(ThreadsAPIError, match="500"):
        await client.create_text_container("я" * 400)  # 800 байт
    await client.http.aclose()


async def test_media_insights(tmp_path):
    requests: list[httpx.Request] = []
    client = make_client(tmp_path, requests)
    metrics = await client.media_insights("media-1")
    await client.http.aclose()
    assert metrics == {"views": 1000, "likes": 50, "replies": 4}
    assert "metric=" in str(requests[0].url)


async def test_api_error_is_raised(tmp_path):
    client = make_client(tmp_path, [])
    rt = client.rt
    rt.set("threads_user_id", "0")
    with pytest.raises(ThreadsAPIError):
        await client.get_profile()
    await client.http.aclose()


def test_metrics_helpers():
    data = {
        "data": [
            {"name": "likes", "values": [{"value": 10}]},
            {"name": "followers_count", "values": [{"value": 123}]},
        ]
    }
    assert _metrics_to_dict(data) == {"likes": 10, "followers_count": 123}
    assert engagement_score({"likes": 10, "replies": 2, "reposts": 1, "views": 1000}) == 70.0
    assert engagement_score({}) == 0.0
    assert text_length("привет") == 12
    assert text_length("👋") == 4


def test_service_rate_limits(tmp_path):
    store = Store(tmp_path / "t.db")
    store.init()
    rt = Runtime(store)
    service = ContentService(store, rt)

    assert service.can_publish_now().ok

    rt.set("paused", True)
    check = service.can_publish_now()
    assert not check.ok and "пауз" in check.reason
    rt.set("paused", False)

    # публикация «только что» — упираемся в дневной лимит
    pid = store.create_post("x", status="published")
    store.update_post(pid, published_at=utcnow())
    rt.set("max_posts_per_day", 1)
    check = service.can_publish_now()
    assert not check.ok and "дневной лимит" in check.reason

    # лимит поднят — теперь срабатывает минимальный интервал
    rt.set("max_posts_per_day", 10)
    rt.set("min_minutes_between_posts", 60)
    check = service.can_publish_now()
    assert not check.ok and "рано" in check.reason

    # старый пост не мешает
    store.update_post(pid, published_at="2020-01-01 00:00:00+00:00")
    assert service.can_publish_now().ok


def test_utcnow_is_sqlite_comparable(tmp_path):
    """Регрессия: 'T' в ISO-строке ломает сравнение с datetime('now') внутри дня."""
    import sqlite3

    store = Store(tmp_path / "t.db")
    store.init()
    ts = utcnow()
    assert "T" not in ts and "+00:00" in ts

    conn = sqlite3.connect(":memory:")
    older = conn.execute(
        "SELECT ? < datetime('2026-01-01 18:00:00')",
        ("2026-01-01 10:00:00+00:00",),
    ).fetchone()[0]
    assert older == 1


async def test_service_publish_dry_run(tmp_path):
    store = Store(tmp_path / "t.db")
    store.init()
    rt = Runtime(store)
    rt.set("dry_run", True)
    service = ContentService(store, rt)
    post_id = store.create_post("текст для сухого режима", status="approved")

    assert await service.publish(post_id) is True
    post = store.get_post(post_id)
    assert post["status"] == "published_dry"
    assert post["published_at"]
    assert post["threads_media_id"] is None


async def test_web_health_and_auth_redirect(tmp_path):
    from fastapi.testclient import TestClient

    from app.web import create_app

    store = Store(tmp_path / "t.db")
    store.init()
    rt = Runtime(store)
    service = ContentService(store, rt)
    client = TestClient(create_app(service, rt))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["threads_connected"] is False
    assert health.json()["posting_frequency"] == 3
    assert health.json()["posting_jitter_minutes"] == 10

    help_page = client.get("/help")
    assert help_page.status_code == 200
    assert "text/html" in help_page.headers["content-type"]
    assert "Командный стол" in help_page.text
    for command in ("/post", "/memory", "/frequency", "/jitter", "/token", "/cancel"):
        assert command in help_page.text

    resp = client.get("/auth/threads", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://threads.com/oauth/authorize")
    assert "scope=threads_basic" in location
    assert "response_type=code" in location

    # регрессия: 204 не должен отдавать тело (иначе uvicorn: content longer than Content-Length)
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 204
    assert favicon.content == b""
    assert "content-length" not in {k.lower() for k in favicon.headers} or favicon.headers.get(
        "content-length"
    ) in (None, "0")


async def test_publish_image_stops_on_failed_container(tmp_path):
    store = Store(tmp_path / "error.db")
    store.init()
    rt = Runtime(store)
    rt.set("threads_access_token", "token123")
    rt.set("threads_user_id", "999")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "bad-container"})
        if request.method == "GET" and request.url.path.endswith("/bad-container"):
            return httpx.Response(
                200,
                json={"id": "bad-container", "status": "ERROR", "error_message": "BAD_IMAGE"},
            )
        return httpx.Response(500, json={"error": {"message": "should not publish"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ThreadsClient(rt, http=http)
    with pytest.raises(ThreadsAPIError, match="BAD_IMAGE"):
        await client.publish_image("text", "https://example.test/bad.png")
    await http.aclose()
    assert not any(r.url.path.endswith("/threads_publish") for r in requests)
