"""Отладка: сырой ответ Polza для запроса автора."""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/ghostwriter")

import httpx

from app.agents.writer import SYSTEM
from app.config import settings


async def call(payload_extra: dict, label: str) -> None:
    payload = {
        "model": settings.writer_model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "НИША: технологии и ИИ\nЯЗЫК ПОСТА: ru\n"
                    "ТЕМА: Почему люди бросают проекты на середине\n"
                    "Ограничение: не более 200 символов. Верни только JSON."
                ),
            },
        ],
        "temperature": 0.8,
        "max_tokens": 900,
    }
    payload.update(payload_extra)
    print("=" * 72)
    print("ТЕСТ:", label, "| json_mode:", payload_extra)
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
        )
    print("HTTP:", resp.status_code)
    try:
        data = resp.json()
    except Exception:
        print("НЕ JSON:", resp.text[:800])
        return
    if "choices" in data:
        msg = data["choices"][0].get("message", {})
        print("ключи message:", list(msg.keys()))
        print("content:", repr(msg.get("content"))[:400])
        for k in msg:
            if k != "content":
                print(f"  {k}:", repr(msg[k])[:400])
        print("finish_reason:", data["choices"][0].get("finish_reason"))
        print("usage:", data.get("usage"))
    else:
        print(json.dumps(data, ensure_ascii=False)[:800])


async def main() -> None:
    print("модель:", settings.writer_model)
    await call({}, "без response_format")
    await call({"response_format": {"type": "json_object"}}, "с response_format")


asyncio.run(main())
