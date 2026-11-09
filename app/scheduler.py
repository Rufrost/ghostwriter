"""Планировщик: пользовательская очередь, метрики и продление токена."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .runtime import Runtime
from .service import ContentService
from .threads_api import ThreadsAPIError, ThreadsClient

log = logging.getLogger(__name__)

POSTING_JOB = "posting"
METRICS_JOB = "metrics"
TOKEN_JOB = "token"


def symmetric_jitter_base(hour: int, minute: int, jitter_minutes: int) -> tuple[int, int]:
    """Cron даёт только положительный jitter, поэтому стартуем на N минут раньше."""
    total = (hour * 60 + minute - jitter_minutes) % (24 * 60)
    return total // 60, total % 60


class Scheduler:
    def __init__(self, service: ContentService, rt: Runtime) -> None:
        self.service = service
        self.rt = rt
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)
        self._started = False

    # ----------生命周期 ----------
    def start(self) -> None:
        if self._started:
            return
        self.scheduler.add_job(
            self._metrics_job,
            IntervalTrigger(hours=6),
            id=METRICS_JOB,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._token_job,
            CronTrigger(hour=4, minute=10),
            id=TOKEN_JOB,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        self.reload()
        self.scheduler.start()
        self._started = True
        log.info("Планировщик запущен: %s", self.describe())

    def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    def reload(self) -> None:
        """Пересоздаёт задачи расписания после изменения настроек."""
        self._schedule_posting()

    def _schedule_posting(self) -> None:
        for job in self.scheduler.get_jobs():
            if job.id.startswith(f"{POSTING_JOB}-"):
                self.scheduler.remove_job(job.id)
        jitter_minutes = self.rt.posting_jitter_minutes
        for hour, minute in self.rt.posting_times:
            base_hour, base_minute = symmetric_jitter_base(
                hour, minute, jitter_minutes
            )
            trigger = CronTrigger(
                hour=base_hour,
                minute=base_minute,
                timezone=settings.timezone,
                # base=N минут раньше + случайные 0..2N минут = ±N
                jitter=jitter_minutes * 120 if jitter_minutes else None,
            )
            self.scheduler.add_job(
                self._posting_job,
                trigger,
                id=f"{POSTING_JOB}-{hour:02d}{minute:02d}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=900,
            )

    def describe(self) -> str:
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in self.rt.posting_times)
        return (
            f"очередь: {self.service.store.count_queued_posts()} пост.; "
            f"слоты: {len(self.rt.posting_times)}/день, база {times}, "
            f"случайно ±{self.rt.posting_jitter_minutes} мин ({settings.timezone}); "
            "метрики: каждые 6 ч; продление токена: 04:10"
        )

    # ---------- задачи ----------
    async def _posting_job(self) -> None:
        if self.rt.get_bool("paused"):
            log.info("Автопостинг на паузе — пропуск слота")
            return
        try:
            post_id = await self.service.prepare_next_queued_post()
            if post_id is None:
                log.info("Пользовательская очередь пуста — слот пропущен")
                return
            log.info("Пост #%s взят из пользовательской очереди", post_id)
        except Exception as exc:
            log.exception("Ошибка слота публикации")
            if self.service.notifier:
                await self.service.notifier.alert(f"⚠️ Слот публикации не сработал: {exc}")

    async def _metrics_job(self) -> None:
        try:
            await self.service.sync_metrics()
        except Exception:
            log.exception("Ошибка синхронизации метрик")

    async def _token_job(self) -> None:
        if not self.rt.token:
            return
        expires_at = self.rt.get_str("threads_token_expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp - datetime.now(timezone.utc) > timedelta(days=10):
                    return
            except ValueError:
                pass
        try:
            async with ThreadsClient(self.rt) as client:
                data = await client.refresh_token()
            token = str(data.get("access_token") or "")
            if not token:
                raise ThreadsAPIError("refresh не вернул access_token")
            expires_in = int(data.get("expires_in") or 5183944)
            self.rt.set("threads_access_token", token)
            self.rt.set(
                "threads_token_expires_at",
                (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(
                    timespec="seconds"
                ),
            )
            self.service.store.log_event("Токен Threads продлён на 60 дней", "info")
        except Exception as exc:
            log.exception("Не удалось продлить токен")
            if self.service.notifier:
                await self.service.notifier.alert(
                    f"⚠️ Не удалось продлить токен Threads: {exc}\n"
                    "Если срок истёк — переавторизуйся: /auth/threads"
                )


def next_run_times(scheduler: Scheduler) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for job in scheduler.scheduler.get_jobs():
        nrt = getattr(job, "next_run_time", None)
        if nrt:
            out.append((job.id, nrt.astimezone().strftime("%d.%m %H:%M")))
    out.sort(key=lambda x: x[1])
    return out
