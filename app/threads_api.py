"""Клиент Threads API (graph.threads.net) — публикация, метрики, токены.

Документация: https://developers.facebook.com/docs/threads
Двухшаговая публикация: создать media-контейнер → дождаться обработки →
опубликовать. Для текстовых постов media_type=TEXT, лимит 500 символов,
эмодзи считаются как UTF-8 байты.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .config import settings
from .runtime import Runtime

log = logging.getLogger(__name__)

GRAPH = "https://graph.threads.net"
OAUTH_AUTHORIZE = "https://threads.com/oauth/authorize"
OAUTH_TOKEN = f"{GRAPH}/oauth/access_token"
LONG_LIVED = f"{GRAPH}/access_token"
REFRESH = f"{GRAPH}/refresh_access_token"

# Коды, означающие проблему с токеном (нужна повторная авторизация)
TOKEN_ERROR_CODES = {102, 190, 463, 467}

SCOPES = ",".join(
    [
        "threads_basic",
        "threads_content_publish",
        "threads_manage_insights",
        "threads_read_replies",
        "threads_manage_replies",
    ]
)

MEDIA_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
USER_METRICS = ("views", "likes", "replies", "reposts", "quotes", "clicks", "followers_count")


class ThreadsAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: int = 0, subcode: int = 0):
        super().__init__(message)
        self.status = status
        self.code = code
        self.subcode = subcode

    @property
    def is_token_error(self) -> bool:
        return self.code in TOKEN_ERROR_CODES


def text_length(text: str) -> int:
    """Threads считает длину текста в UTF-8 байтах (эмодзи — 4 байта)."""
    return len(text.encode("utf-8"))


class ThreadsClient:
    def __init__(self, rt: Runtime, http: httpx.AsyncClient | None = None) -> None:
        self.rt = rt
        self._http = http
        self._owns_http = http is None

    # ---------- инфраструктура ----------
    async def __aenter__(self) -> "ThreadsClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
            self._owns_http = True
        return self._http

    @property
    def token(self) -> str:
        return self.rt.token

    @property
    def user_id(self) -> str:
        return self.rt.user_id

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.user_id)

    def _raise_for_error(self, resp: httpx.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            raise ThreadsAPIError(
                f"Threads API {resp.status_code}: {resp.text[:400]}", status=resp.status_code
            ) from None
        err = data.get("error") or {}
        message = err.get("message") or data.get("error_message") or resp.text[:400]
        code = int(err.get("code") or data.get("code") or 0)
        subcode = int(err.get("error_subcode") or 0)
        raise ThreadsAPIError(
            f"Threads API: {message}", status=resp.status_code, code=code, subcode=subcode
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        params["access_token"] = self.token
        resp = await self.http.get(f"{GRAPH}/v1.0/{path.lstrip('/')}", params=params)
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        return resp.json()

    async def _post(self, path: str, data: dict[str, Any]) -> Any:
        payload = dict(data)
        payload["access_token"] = self.token
        resp = await self.http.post(f"{GRAPH}/v1.0/{path.lstrip('/')}", data=payload)
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        return resp.json()

    # ---------- OAuth ----------
    @staticmethod
    def authorize_url(redirect_uri: str, state: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": settings.threads_app_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "response_type": "code",
            "state": state,
        }
        return f"{OAUTH_AUTHORIZE}?{urlencode(params)}"

    @staticmethod
    async def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
        """Код авторизации → короткоживущий токен (1 час)."""
        payload = {
            "client_id": settings.threads_app_id,
            "client_secret": settings.threads_app_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(OAUTH_TOKEN, data=payload)
        if resp.status_code >= 400:
            body = resp.text[:400]
            raise ThreadsAPIError(f"Обмен кода не удался: {body}", status=resp.status_code)
        return resp.json()

    @staticmethod
    async def exchange_long_lived(short_token: str) -> dict[str, Any]:
        """Короткоживущий токен → долгоживущий (60 дней)."""
        params = {
            "grant_type": "th_exchange_token",
            "client_secret": settings.threads_app_secret,
            "access_token": short_token,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(LONG_LIVED, params=params)
        if resp.status_code >= 400:
            raise ThreadsAPIError(f"Получение долгоживущего токена: {resp.text[:400]}",
                                  status=resp.status_code)
        return resp.json()

    async def refresh_token(self) -> dict[str, Any]:
        """Продлевает долгоживущий токен ещё на 60 дней."""
        params = {"grant_type": "th_refresh_token", "access_token": self.token}
        resp = await self.http.get(REFRESH, params=params)
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        return resp.json()

    # ---------- профиль ----------
    async def get_profile(self) -> dict[str, Any]:
        return await self._get(
            self.user_id, {"fields": "id,username,name,threads_profile_picture_url"}
        )

    async def publishing_limit(self) -> dict[str, Any]:
        return await self._get(
            f"{self.user_id}/threads_publishing_limit",
            {"fields": "quota_usage,config"},
        )

    # ---------- публикация ----------
    async def create_text_container(
        self,
        text: str,
        *,
        topic_tag: str | None = None,
        link_attachment: str | None = None,
        made_with_ai: bool = False,
    ) -> str:
        if text_length(text) > 500:
            raise ThreadsAPIError(
                f"Текст длиннее 500 байт ({text_length(text)}), Threads его не примет"
            )
        data: dict[str, Any] = {"media_type": "TEXT", "text": text}
        if topic_tag:
            # topic_tag: 1..50 символов, без точек и амперсандов
            clean = topic_tag.strip().lstrip("#").replace(".", "").replace("&", "")[:50]
            if clean:
                data["topic_tag"] = clean
        if link_attachment:
            data["link_attachment"] = link_attachment
        if made_with_ai:
            data["is_made_with_ai"] = "true"
        result = await self._post(f"{self.user_id}/threads", data)
        container_id = str(result.get("id") or "")
        if not container_id:
            raise ThreadsAPIError(f"API не вернул id контейнера: {result}")
        return container_id

    async def create_image_container(
        self,
        text: str,
        image_url: str,
        *,
        topic_tag: str | None = None,
        made_with_ai: bool = False,
    ) -> str:
        """Создаёт контейнер одиночного изображения с подписью."""
        if text_length(text) > 500:
            raise ThreadsAPIError(
                f"Текст длиннее 500 байт ({text_length(text)}), Threads его не примет"
            )
        if not image_url.startswith(("https://", "http://")):
            raise ThreadsAPIError("image_url должен быть публичным HTTP(S)-адресом")
        data: dict[str, Any] = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": text,
        }
        if topic_tag:
            clean = topic_tag.strip().lstrip("#").replace(".", "").replace("&", "")[:50]
            if clean:
                data["topic_tag"] = clean
        if made_with_ai:
            data["is_made_with_ai"] = "true"
        result = await self._post(f"{self.user_id}/threads", data)
        container_id = str(result.get("id") or "")
        if not container_id:
            raise ThreadsAPIError(f"API не вернул id контейнера: {result}")
        return container_id

    async def wait_container(self, container_id: str, timeout: float = 90.0) -> str:
        """Ждёт готовности контейнера. Возвращает статус FINISHED.

        Для текстовых постов обработка обычно мгновенная, но API рекомендует
        подождать перед публикацией, поэтому опрашиваем статус с паузой.
        """
        deadline = time.monotonic() + timeout
        delay = 2.0
        last_status = ""
        while time.monotonic() < deadline:
            try:
                data = await self._get(container_id, {"fields": "status,error_message"})
            except ThreadsAPIError as exc:
                if exc.is_token_error:
                    raise
                # Временная ошибка чтения статуса: продолжаем опрос. Статусы
                # ERROR/EXPIRED обрабатываются ниже и не должны проглатываться.
                log.debug("Ожидание контейнера %s: %s", container_id, exc)
            else:
                last_status = str(data.get("status") or "")
                if last_status in {"FINISHED", "PUBLISHED"}:
                    return last_status
                if last_status in {"ERROR", "EXPIRED"}:
                    raise ThreadsAPIError(
                        f"Контейнер в статусе {last_status}: {data.get('error_message', '')}"
                    )
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 10.0)
        # не блокируем публикацию навсегда: пробуем опубликовать всё равно
        log.warning("Контейнер %s не подтвердил готовность (статус %r)", container_id, last_status)
        return last_status

    async def publish_container(self, container_id: str) -> str:
        result = await self._post(
            f"{self.user_id}/threads_publish", {"creation_id": container_id}
        )
        media_id = str(result.get("id") or "")
        if not media_id:
            raise ThreadsAPIError(f"API не вернул id публикации: {result}")
        return media_id

    async def publish_text(
        self,
        text: str,
        *,
        topic_tag: str | None = None,
        wait_seconds: float = 0.0,
    ) -> tuple[str, str | None]:
        """Полный цикл публикации. Возвращает (media_id, permalink).

        wait_seconds=0 — публикуем сразу (для текста это нормально и быстрее);
        больше нуля — ждём готовности контейнера.
        """
        container_id = await self.create_text_container(text, topic_tag=topic_tag)
        if wait_seconds > 0:
            await self.wait_container(container_id, timeout=wait_seconds)
        media_id = await self.publish_container(container_id)
        permalink: str | None = None
        try:
            info = await self._get(media_id, {"fields": "permalink,id"})
            permalink = info.get("permalink")
        except ThreadsAPIError as exc:
            log.debug("permalink недоступен для %s: %s", media_id, exc)
        return media_id, permalink

    async def publish_image(
        self,
        text: str,
        image_url: str,
        *,
        topic_tag: str | None = None,
    ) -> tuple[str, str | None]:
        """Полный цикл публикации поста с одним изображением."""
        container_id = await self.create_image_container(
            text, image_url, topic_tag=topic_tag
        )
        await self.wait_container(container_id, timeout=90.0)
        media_id = await self.publish_container(container_id)
        permalink: str | None = None
        try:
            info = await self._get(media_id, {"fields": "permalink,id"})
            permalink = info.get("permalink")
        except ThreadsAPIError as exc:
            log.debug("permalink недоступен для %s: %s", media_id, exc)
        return media_id, permalink

    async def delete_post(self, media_id: str) -> bool:
        resp = await self.http.delete(
            f"{GRAPH}/v1.0/{media_id}", params={"access_token": self.token}
        )
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        return bool((resp.json() or {}).get("success", True))

    # ---------- метрики ----------
    async def media_insights(self, media_id: str) -> dict[str, int]:
        data = await self._get(
            f"{media_id}/insights", {"metric": ",".join(MEDIA_METRICS)}
        )
        return _metrics_to_dict(data)

    async def user_insights(self, since: str, until: str) -> dict[str, int]:
        data = await self._get(
            f"{self.user_id}/threads_insights",
            {"metric": ",".join(USER_METRICS), "since": since, "until": until},
        )
        return _metrics_to_dict(data)


def _metrics_to_dict(data: dict[str, Any]) -> dict[str, int]:
    """Ответ insights: {"data":[{"name":..., "values":[{"value": N}]}]}."""
    out: dict[str, int] = {}
    for item in data.get("data") or []:
        name = item.get("name")
        values = item.get("values") or []
        if name and values:
            value = values[0].get("value")
            if isinstance(value, (int, float)):
                out[str(name)] = int(value)
            elif isinstance(value, dict):
                # follower_demographics и подобные — сохраняем как есть
                out[str(name)] = value  # type: ignore[assignment]
    return out


def _num(metrics: dict[str, Any], key: str) -> float:
    value = metrics.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def engagement_score(metrics: dict[str, Any]) -> float:
    """Единая метрика для сравнения постов: лайки/ответы/репосты/цитаты + просмотры."""
    if not metrics:
        return 0.0
    return (
        _num(metrics, "likes")
        + 3 * _num(metrics, "replies")
        + 4 * _num(metrics, "reposts")
        + 3 * _num(metrics, "quotes")
        + 2 * _num(metrics, "shares")
        + 0.05 * _num(metrics, "views")
    )
