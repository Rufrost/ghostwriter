"""Постоянный сток изображений: проверка, хранение и публичные URL."""
from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .runtime import Runtime

MAX_IMAGE_BYTES = 8 * 1024 * 1024


class StockImageError(ValueError):
    pass


@dataclass(frozen=True)
class ImageInfo:
    mime_type: str
    extension: str
    width: int
    height: int
    size_bytes: int
    sha256: str


def inspect_image(data: bytes) -> ImageInfo:
    """Проверяет поддерживаемый Threads JPEG/PNG без сторонних библиотек."""
    if not data:
        raise StockImageError("Файл пустой")
    if len(data) > MAX_IMAGE_BYTES:
        raise StockImageError("Изображение больше 8 МБ — Threads его не примет")

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise StockImageError("Повреждённый PNG")
        width, height = struct.unpack(">II", data[16:24])
        mime_type, extension = "image/png", "png"
    elif data.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_dimensions(data)
        mime_type, extension = "image/jpeg", "jpg"
    else:
        raise StockImageError("Поддерживаются только JPEG и PNG")

    if width <= 0 or height <= 0:
        raise StockImageError("Не удалось определить размер изображения")
    if max(width / height, height / width) > 10:
        raise StockImageError(
            f"Слишком вытянутое изображение ({width}×{height}); предел Threads — 10:1"
        )

    return ImageInfo(
        mime_type=mime_type,
        extension=extension,
        width=width,
        height=height,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    # SOF-маркеры, содержащие высоту и ширину (baseline/progressive и варианты).
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    i = 2
    while i + 3 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > len(data):
            break
        length = int.from_bytes(data[i : i + 2], "big")
        if length < 2 or i + length > len(data):
            break
        if marker in sof and length >= 7:
            height = int.from_bytes(data[i + 3 : i + 5], "big")
            width = int.from_bytes(data[i + 5 : i + 7], "big")
            return width, height
        i += length
    raise StockImageError("Повреждённый JPEG или не удалось определить его размер")


def stock_file_path(storage_name: str) -> Path:
    """Безопасный путь: в БД хранится только basename, без каталогов."""
    if not storage_name or Path(storage_name).name != storage_name:
        raise StockImageError("Некорректное имя файла в медиастоке")
    return settings.stock_dir / storage_name


def write_stock_file(storage_name: str, data: bytes) -> Path:
    """Атомарно записывает уже проверенное изображение."""
    path = stock_file_path(storage_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return path


def public_image_url(rt: Runtime, token: str) -> str:
    return f"{rt.public_base_url}/media/stock/{token}"
