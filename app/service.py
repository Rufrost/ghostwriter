"""Оркестрация: пользовательская очередь и Автор → согласование → публикация."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .agents.writer import Writer, WriterResult
from .config import settings
from .db import Store, metrics_dict, to_db_ts, utcnow
from .runtime import Runtime
from .stock import public_image_url, stock_file_path
from .threads_api import ThreadsAPIError, ThreadsClient, engagement_score, text_length

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def post_for_approval(self, post_id: int, text: str, topic: str, meta: dict[str, Any]) -> None: ...
    async def info(self, message: str) -> None: ...
    async def alert(self, message: str) -> None: ...


@dataclass
class PublishCheck:
    ok: bool
    reason: str = ""


class ContentService:
    def __init__(
        self,
        store: Store,
        rt: Runtime,
        *,
        writer: Writer | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.store = store
        self.rt = rt
        self.writer = writer or Writer(store, rt)
        self.notifier = notifier
        self._publish_lock = asyncio.Lock()
        self._generation_lock = asyncio.Lock()

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(settings.timezone)
        except Exception:
            return ZoneInfo("UTC")

    def now_local(self) -> datetime:
        return datetime.now(self.tz)

    # ---------- изображения и согласование ----------
    def _assign_image(self, post_id: int) -> Any | None:
        image = self.store.get_post_image(post_id)
        if image is None and self.rt.get_bool("stock_images_enabled"):
            image = self.store.assign_stock_image(post_id)
        if image is None:
            return None
        path = stock_file_path(str(image["storage_name"]))
        if path.is_file():
            return image
        self.store.set_stock_image_active(int(image["id"]), False)
        self.store.log_event(
            f"Изображение #{image['id']} отсутствует на диске и отключено", "error"
        )
        return None

    async def _send_for_approval(
        self,
        post_id: int,
        *,
        attempts: int = 1,
        topic_tag: str = "",
        notify: bool = True,
    ) -> None:
        post = self.store.get_post(post_id)
        if post is None:
            raise ValueError(f"Пост #{post_id} не найден")
        image = self._assign_image(post_id)
        self.store.update_post(post_id, status="pending_approval", error=None)
        if not notify or not self.notifier:
            return
        await self.notifier.post_for_approval(
            post_id,
            str(post["text"]),
            str(post["topic"] or "Без темы"),
            {
                "bytes": text_length(str(post["text"])),
                "limit": self.rt.max_chars,
                "topic_tag": topic_tag,
                "source": str(post["source"]),
                "attempts": attempts,
                "image_id": int(image["id"]) if image is not None else None,
                "image_path": (
                    str(stock_file_path(str(image["storage_name"])))
                    if image is not None
                    else ""
                ),
            },
        )

    async def _process_draft(
        self,
        post_id: int,
        *,
        attempts: int = 1,
        topic_tag: str = "",
        notify: bool = True,
    ) -> bool:
        self._assign_image(post_id)
        if self.rt.get_bool("auto_approve"):
            self.store.update_post(post_id, status="approved", error=None)
            published = await self.publish(post_id)
            if not published and notify and self.notifier:
                await self.notifier.alert(
                    f"Пост #{post_id} не опубликован — смотри /status."
                )
            return published
        await self._send_for_approval(
            post_id, attempts=attempts, topic_tag=topic_tag, notify=notify
        )
        return True

    # ---------- генерация Автором ----------
    async def create_post(
        self,
        *,
        topic: str = "Свободная тема",
        instruction: str = "",
        notify: bool = True,
    ) -> int:
        """Генерирует один пост по явно заданной пользователем теме."""
        topic = topic.strip() or "Свободная тема"
        async with self._generation_lock:
            result = await self.writer.generate(topic, instruction=instruction)
            post_id = self.store.create_post(
                result.text,
                topic=topic,
                status="draft",
                source="generated",
            )
            await self._process_draft(
                post_id,
                attempts=result.attempts,
                topic_tag=result.topic_tag,
                notify=notify,
            )
            return post_id

    async def regenerate(self, post_id: int, instruction: str = "") -> int:
        """Создаёт новый самостоятельный вариант текущего поста."""
        old = self.store.get_post(post_id)
        if old is None:
            raise ValueError(f"Пост #{post_id} не найден")
        async with self._generation_lock:
            result = await self.writer.paraphrase(str(old["text"]), instruction)
            self.store.update_post(post_id, status="rejected", error="перефразирован")
            new_id = self.store.create_post(
                result.text,
                topic=f"Вариант поста #{post_id}",
                status="draft",
                source="rephrased",
            )
            await self._process_draft(
                new_id, topic_tag=result.topic_tag, notify=True
            )
            return new_id

    async def revise(self, post_id: int, instruction: str) -> WriterResult:
        old = self.store.get_post(post_id)
        if old is None:
            raise ValueError(f"Пост #{post_id} не найден")
        result = await self.writer.revise(str(old["text"]), instruction)
        self.store.update_post(post_id, text=result.text)
        return result

    # ---------- пользовательская очередь ----------
    def queue_user_posts(self, texts: list[str]) -> list[int]:
        """Добавляет несколько готовых текстов в общую очередь расписания."""
        cleaned = [text.strip() for text in texts if text.strip()]
        if not cleaned:
            raise ValueError("Не найдено ни одного поста")
        for index, text in enumerate(cleaned, start=1):
            size = text_length(text)
            if size > self.rt.max_chars:
                raise ValueError(
                    f"Пост {index} занимает {size} байт; лимит — {self.rt.max_chars}"
                )
        ids = [
            self.store.create_post(
                text,
                topic="Заданный пост",
                status="queued",
                source="user_queue",
            )
            for text in cleaned
        ]
        self.store.log_event(f"В очередь добавлено пользовательских постов: {len(ids)}")
        return ids

    def update_queued_post(self, post_id: int, text: str) -> bool:
        text = text.strip()
        if not text:
            raise ValueError("Текст поста не может быть пустым")
        size = text_length(text)
        if size > self.rt.max_chars:
            raise ValueError(f"Текст занимает {size} байт; лимит — {self.rt.max_chars}")
        post = self.store.get_post(post_id)
        if post is None or post["status"] != "queued":
            return False
        self.store.update_post(post_id, text=text, error=None)
        return True

    def delete_queued_post(self, post_id: int) -> bool:
        return self.store.delete_queued_post(post_id)

    async def rephrase_to_queue(self, post_id: int, instruction: str = "") -> int:
        """Добавляет LLM-вариант как отдельный пост; исходник остаётся в очереди."""
        post = self.store.get_post(post_id)
        if post is None:
            raise ValueError(f"Пост #{post_id} не найден")
        async with self._generation_lock:
            result = await self.writer.paraphrase(str(post["text"]), instruction)
        new_id = self.store.create_post(
            result.text,
            topic=f"Перефразированный пост #{post_id}",
            status="queued",
            source="rephrased",
        )
        self.store.log_event(
            f"Автор создал вариант #{new_id} для пользовательского поста #{post_id}"
        )
        return new_id

    async def prepare_queued_post(self, post_id: int, *, notify: bool = True) -> bool:
        """Передаёт очередной пользовательский пост в публикацию или согласование."""
        post = self.store.get_post(post_id)
        if post is None or post["status"] != "queued":
            return False
        return await self._process_draft(post_id, notify=notify)

    async def prepare_next_queued_post(self) -> int | None:
        post = self.store.next_queued_post()
        if post is None:
            return None
        post_id = int(post["id"])
        if not await self.prepare_queued_post(post_id):
            return None
        return post_id

    async def publish_queued_now(self, post_id: int) -> bool:
        post = self.store.get_post(post_id)
        if post is None or post["status"] not in {"queued", "failed", "pending_approval"}:
            return False
        self._assign_image(post_id)
        self.store.update_post(post_id, status="approved", error=None)
        return await self.publish(post_id)

    # ---------- согласование ----------
    async def approve(self, post_id: int) -> bool:
        post = self.store.get_post(post_id)
        if post is None:
            return False
        if post["status"] not in {
            "queued", "pending_approval", "draft", "approved", "failed"
        }:
            return False
        self._assign_image(post_id)
        self.store.update_post(post_id, status="approved", error=None)
        return await self.publish(post_id)

    async def reject(self, post_id: int) -> None:
        self.store.update_post(post_id, status="rejected")

    # ---------- публикация ----------
    def can_publish_now(self) -> PublishCheck:
        if self.rt.get_bool("paused"):
            return PublishCheck(False, "автопостинг на паузе (/resume чтобы включить)")
        if self.rt.get_bool("dry_run"):
            return PublishCheck(True, "сухой режим")

        now = datetime.now(timezone.utc)
        local_midnight = self.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        since = to_db_ts(local_midnight)
        published_today = self.store.count_published_since(since)
        daily_cap = self.rt.get_int("max_posts_per_day")
        if published_today >= daily_cap:
            return PublishCheck(False, f"дневной лимит исчерпан ({published_today}/{daily_cap})")

        last = self.store.last_published_at()
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                gap = now - last_dt
                min_gap = timedelta(minutes=self.rt.get_int("min_minutes_between_posts"))
                if gap < min_gap:
                    left = int((min_gap - gap).total_seconds() // 60) + 1
                    return PublishCheck(False, f"слишком рано: следующий пост через ~{left} мин")
            except ValueError:
                pass
        return PublishCheck(True)

    async def publish(self, post_id: int, *, ignore_schedule: bool = True) -> bool:
        async with self._publish_lock:
            post = self.store.get_post(post_id)
            if post is None:
                return False
            if post["status"] in {"published", "published_dry"}:
                return True

            check = self.can_publish_now()
            if not check.ok and not ignore_schedule:
                self.store.log_event(f"Публикация #{post_id} отложена: {check.reason}", "warn")
                return False
            if not check.ok and check.reason.startswith("дневной лимит"):
                self.store.update_post(post_id, status="failed", error=check.reason)
                if self.notifier:
                    await self.notifier.alert(f"Пост #{post_id} не опубликован: {check.reason}")
                return False

            text = str(post["text"])
            topic = str(post["topic"] or "")

            if self.rt.get_bool("dry_run"):
                now = utcnow()
                self.store.update_post(
                    post_id, status="published_dry", published_at=now, threads_media_id=None
                )
                self.store.log_event(f"Сухой режим: пост #{post_id} «опубликован»", "info")
                if self.notifier:
                    await self.notifier.info(
                        f"🧪 Сухой режим: пост #{post_id} не отправлен в Threads.\n\n{text}"
                    )
                return True

            self.store.update_post(post_id, status="publishing", error=None)
            try:
                async with ThreadsClient(self.rt) as client:
                    if not client.is_configured:
                        raise ThreadsAPIError(
                            "Threads не подключён: нет токена или user_id. "
                            "Открой ссылку /auth/threads и авторизуйся."
                        )
                    image = self.store.get_post_image(post_id)
                    if image is not None:
                        image_path = stock_file_path(str(image["storage_name"]))
                    else:
                        image_path = None
                    if image is not None and image_path is not None and image_path.is_file():
                        image_url = public_image_url(self.rt, str(image["token"]))
                        media_id, permalink = await client.publish_image(text, image_url)
                    else:
                        media_id, permalink = await client.publish_text(text)
            except ThreadsAPIError as exc:
                self.store.update_post(post_id, status="failed", error=str(exc)[:900])
                self.store.log_event(f"Ошибка публикации #{post_id}: {exc}", "error")
                if self.notifier:
                    hint = (
                        "\n\nНужна повторная авторизация: открой ссылку /auth/threads."
                        if exc.is_token_error
                        else ""
                    )
                    await self.notifier.alert(f"❌ Не удалось опубликовать пост #{post_id}: {exc}{hint}")
                return False
            except Exception as exc:  # сеть и прочее
                log.exception("Публикация #%s упала", post_id)
                self.store.update_post(post_id, status="failed", error=str(exc)[:900])
                if self.notifier:
                    await self.notifier.alert(f"❌ Публикация #{post_id} упала: {exc}")
                return False

            now = utcnow()
            if not permalink:
                username = self.rt.username
                permalink = (
                    f"https://www.threads.net/@{username}/post/{media_id}" if username else None
                )
            self.store.update_post(
                post_id,
                status="published",
                published_at=now,
                threads_media_id=media_id,
                threads_permalink=permalink,
            )
            self.store.log_event(f"Опубликован пост #{post_id} → {media_id}", "info")
            if self.notifier:
                await self.notifier.info(
                    f"✅ Опубликовано: «{topic or 'без темы'}»\n{permalink or media_id}\n\n{text}"
                )
            return True

    # ---------- метрики ----------
    async def sync_metrics(self) -> int:
        if not self.rt.token or not self.rt.user_id:
            return 0
        updated = 0
        async with ThreadsClient(self.rt) as client:
            for post in self.store.posts_pending_metrics(limit=50):
                media_id = post["threads_media_id"]
                if not media_id:
                    continue
                try:
                    metrics = await client.media_insights(str(media_id))
                except ThreadsAPIError as exc:
                    if exc.is_token_error:
                        self.store.log_event(f"Метрики: проблема с токеном — {exc}", "error")
                        if self.notifier:
                            await self.notifier.alert(
                                "⚠️ Токен Threads недействителен. Нужна переавторизация: /auth/threads"
                            )
                        return updated
                    log.debug("Метрики поста #%s: %s", post["id"], exc)
                    continue
                self.store.update_post(
                    int(post["id"]),
                    metrics_json=json.dumps(metrics, ensure_ascii=False),
                    metrics_updated_at=utcnow(),
                )
                updated += 1
        if updated:
            self.store.log_event(f"Обновлены метрики у {updated} постов", "info")
        return updated

    async def account_insights(self, days: int = 7) -> dict[str, int]:
        if not self.rt.token or not self.rt.user_id:
            return {}
        until = datetime.now(timezone.utc).date()
        since = until - timedelta(days=days)
        async with ThreadsClient(self.rt) as client:
            return await client.user_insights(since.isoformat(), until.isoformat())

    # ---------- снимок для /status ----------
    def summary(self) -> dict[str, Any]:
        recent = self.store.recent_posts(limit=50)
        published = [p for p in recent if p["status"] in {"published", "published_dry"}]
        total = {"views": 0, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "shares": 0}
        for p in published:
            m = metrics_dict(p)
            for k in total:
                try:
                    total[k] += int(m.get(k) or 0)
                except (TypeError, ValueError):
                    pass
        best = None
        if published:
            best = max(published, key=lambda p: engagement_score(metrics_dict(p)))
        pending = sum(1 for p in recent if p["status"] == "pending_approval")
        failed = sum(1 for p in recent if p["status"] == "failed")
        return {
            "connected": bool(self.rt.token and self.rt.user_id),
            "username": self.rt.username,
            "paused": self.rt.get_bool("paused"),
            "auto_approve": self.rt.get_bool("auto_approve"),
            "dry_run": self.rt.get_bool("dry_run"),
            "queued_posts": self.store.count_queued_posts(),
            "stock": self.store.stock_counts(),
            "stock_enabled": self.rt.get_bool("stock_images_enabled"),
            "knowledge": self.store.project_knowledge_stats(),
            "pending": pending,
            "failed": failed,
            "published_recent": len(published),
            "totals": total,
            "best_post": best,
            "posting_times": self.rt.posting_times,
            "check": self.can_publish_now(),
            "tz": str(self.tz),
        }
