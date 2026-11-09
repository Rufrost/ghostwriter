"""Клиент LLM: OpenAI-совместимый /chat/completions или Anthropic Messages API.

Поддерживаются любой провайдер с OpenAI-совместимым API (OpenAI, DeepSeek,
OpenRouter, Ollama, vLLM, Together и т.п.) и нативный Anthropic.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


# Сколько токенов запрашивать при повторе, если ответ пришёл пустым:
# рассуждения модели съели весь max_tokens.
EMPTY_RETRY_TOKENS = 4000


def extract_json(raw: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели, терпимо к ```json обёрткам."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        raise LLMError("Ответ модели не является JSON-объектом")
    except json.JSONDecodeError:
        pass
    # последняя попытка: первый {...} в тексте
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise LLMError(f"Не удалось разобрать JSON из ответа модели: {raw[:300]}")


class LLMClient:
    def __init__(
        self,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = (provider or settings.llm_provider).lower().strip()
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.timeout = timeout or settings.llm_timeout
        self.transport = transport

    # ---------- публичный API ----------
    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float = 0.8,
        max_tokens: int = 1500,
        json_mode: bool = False,
    ) -> str:
        if self.provider == "anthropic":
            return await self._anthropic(
                system=system,
                user=user,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return await self._openai(
            system=system,
            user=user,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )

    # ---------- реализации ----------
    async def _openai(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY не задан")
        url = f"{self.base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # «Думающие» модели (reasoning) тратят max_tokens на размышления и при
        # нехватке лимита возвращают пустой content. Повторяем с бо́льшим лимитом.
        limits = [max_tokens]
        if max_tokens < EMPTY_RETRY_TOKENS:
            limits.append(EMPTY_RETRY_TOKENS)

        detail = ""
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            for limit in limits:
                payload = dict(body, max_tokens=limit)
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 400 and "response_format" in payload:
                    # не все провайдеры поддерживают response_format
                    payload.pop("response_format")
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code >= 400:
                    raise LLMError(f"LLM {resp.status_code}: {resp.text[:500]}")
                try:
                    data = resp.json()
                    choice = data["choices"][0]
                    content = str((choice.get("message") or {}).get("content") or "").strip()
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise LLMError(f"Неожиданный ответ LLM: {resp.text[:500]}") from exc
                if content:
                    return content
                usage = data.get("usage") or {}
                reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                detail = (
                    f"finish_reason={choice.get('finish_reason')}, "
                    f"reasoning_tokens={reasoning}, max_tokens={limit}"
                )
                log.warning("LLM вернул пустой content (%s)", detail)

        raise LLMError(f"LLM вернул пустой ответ ({detail})")

    async def _anthropic(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY не задан")
        base = self.base_url or "https://api.anthropic.com"
        if base.endswith("/v1"):
            url = f"{base}/messages"
        else:
            url = f"{base}/v1/messages"
        payload = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise LLMError(f"LLM {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
            parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return "".join(parts).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError(f"Неожиданный ответ LLM: {resp.text[:500]}") from exc


def get_llm() -> LLMClient:
    return LLMClient()
