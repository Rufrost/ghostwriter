"""Постоянный медиасток: валидация, ротация и публичная выдача."""
import struct

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import Store
from app.runtime import Runtime
from app.service import ContentService
from app.stock import StockImageError, inspect_image, stock_file_path, write_stock_file
from app.web import create_app


def fake_png(width: int = 640, height: int = 640) -> bytes:
    # Для inspect_image достаточно корректной PNG-сигнатуры и IHDR с размерами.
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"test-image-payload"
    )


def add_image(store: Store, number: int) -> int:
    data = fake_png(640 + number, 640)
    info = inspect_image(data)
    image_id, created = store.add_stock_image(
        token=f"token-{number}",
        storage_name=f"image-{number}.png",
        mime_type=info.mime_type,
        size_bytes=info.size_bytes,
        width=info.width,
        height=info.height,
        sha256=info.sha256,
        caption=f"Картинка {number}",
    )
    assert created
    return image_id


def test_inspect_image_accepts_png_and_rejects_bad_input():
    info = inspect_image(fake_png(1080, 1080))
    assert info.mime_type == "image/png"
    assert (info.width, info.height) == (1080, 1080)

    with pytest.raises(StockImageError, match="JPEG и PNG"):
        inspect_image(b"not an image")
    with pytest.raises(StockImageError, match="10:1"):
        inspect_image(fake_png(2000, 100))


def test_stock_rotation_and_disable(tmp_path):
    store = Store(tmp_path / "stock.db")
    store.init()
    ids = [add_image(store, n) for n in (1, 2, 3)]

    assigned = []
    for n in range(4):
        post_id = store.create_post(f"post {n}")
        assigned.append(int(store.assign_stock_image(post_id)["id"]))

    assert assigned == [ids[0], ids[1], ids[2], ids[0]]
    assert store.stock_counts() == {"total": 3, "active": 3, "uses": 4}

    assert store.set_stock_image_active(ids[1], False)
    assert store.stock_counts()["active"] == 2
    post_id = store.create_post("after disable")
    assert int(store.assign_stock_image(post_id)["id"]) != ids[1]


def test_stock_deduplicates_by_hash(tmp_path):
    store = Store(tmp_path / "stock.db")
    store.init()
    first = add_image(store, 1)
    info = inspect_image(fake_png(641, 640))
    duplicate, created = store.add_stock_image(
        token="other-token",
        storage_name="other.png",
        mime_type=info.mime_type,
        size_bytes=info.size_bytes,
        width=info.width,
        height=info.height,
        sha256=info.sha256,
    )
    assert duplicate == first
    assert created is False


def test_public_stock_url(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stock_media_dir", str(tmp_path / "files"))
    store = Store(tmp_path / "stock.db")
    store.init()
    image_id = add_image(store, 7)
    image = store.get_stock_image(image_id)
    data = fake_png(647, 640)
    write_stock_file(str(image["storage_name"]), data)
    assert stock_file_path(str(image["storage_name"])).is_file()

    rt = Runtime(store)
    client = TestClient(create_app(ContentService(store, rt), rt))
    response = client.get("/media/stock/token-7")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == data
    assert client.get("/media/stock/unknown").status_code == 404
