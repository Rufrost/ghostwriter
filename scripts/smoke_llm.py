"""Быстрая проверка ИИ-Автора на сервере. Не трогает продовую базу."""
import asyncio
import sys

sys.path.insert(0, "/opt/ghostwriter")

from app.agents.writer import Writer
from app.config import settings
from app.db import Store
from app.runtime import Runtime


async def main() -> None:
    store = Store("/tmp/gwtest.db")
    store.init()
    rt = Runtime(store)

    print("провайдер    :", settings.llm_provider, "→", settings.llm_base_url)
    print("модель автора:", rt.get_str("writer_model"))
    print("ниша         :", rt.get_str("brand_niche"))
    print("-" * 60)

    writer = Writer(store, rt)
    result = await writer.generate(
        "Почему люди бросают проекты на середине",
        angle="личный опыт и один неочевидный приём",
    )
    print(f"ГЕНЕРАЦИЯ · {result.bytes_len}/500 байт · тег: {result.topic_tag or '—'}")
    print(result.text)
    print("-" * 60)

    variant = await writer.paraphrase(result.text, "сделай динамичнее")
    print(f"ПЕРЕФРАЗИРОВАНИЕ · {variant.bytes_len}/500 байт")
    print(variant.text)


asyncio.run(main())
