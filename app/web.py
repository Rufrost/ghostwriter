"""FastAPI: OAuth-подключение Threads, health-check, служебные страницы."""
from __future__ import annotations

import html
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import settings
from .runtime import Runtime
from .service import ContentService
from .stock import StockImageError, stock_file_path
from .threads_api import ThreadsAPIError, ThreadsClient

log = logging.getLogger(__name__)

HELP_PAGE_PATH = Path(__file__).with_name("static") / "help.html"

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ghostwriter · Threads</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
      font-family:-apple-system,Segoe UI,Roboto,sans-serif;
      background:#0f1115;color:#e8eaed}}
 .card{{max-width:520px;padding:40px;border-radius:18px;background:#171a21;
       border:1px solid #242a35;box-shadow:0 20px 60px rgba(0,0,0,.4)}}
 h1{{font-size:22px;margin:0 0 12px}}
 p{{line-height:1.55;color:#b8bfcc;margin:0 0 10px}}
 code{{background:#0b0d11;padding:2px 6px;border-radius:6px;color:#8fd6ff}}
 .ok{{color:#63e6a3}} .err{{color:#ff7d7d}}
 a{{color:#8fd6ff}}
</style></head><body><div class="card">{body}</div></body></html>"""


def page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(body=body), status_code=status_code)


def create_app(service: ContentService, rt: Runtime) -> FastAPI:
    store = service.store
    app = FastAPI(title="Ghostwriter", docs_url=None, redoc_url=None)

    def auth_key_ok(key: str | None) -> bool:
        if not settings.web_auth_key:
            return True
        return bool(key) and secrets.compare_digest(key, settings.web_auth_key)

    # ---------------------------------------------------------- служебное
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        connected = bool(rt.token and rt.user_id)
        status = (
            f'<p class="ok">Аккаунт подключён: @{html.escape(rt.username or "threads")}</p>'
            if connected
            else '<p class="err">Threads ещё не подключён</p>'
        )
        return page(
            "<h1>Ghostwriter</h1>"
            f"{status}"
            "<p>Сервис автопостинга в Threads. Управление — через Telegram-бота.</p>"
            "<p>Подключение: <code>/auth/threads</code> · Проверка: <code>/health</code></p>"
        )

    @app.get("/help", response_class=HTMLResponse, response_model=None)
    async def command_help() -> FileResponse:
        """Мобильная справка по всем командам Telegram-бота."""
        return FileResponse(
            HELP_PAGE_PATH,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "threads_connected": bool(rt.token and rt.user_id),
                "username": rt.username or None,
                "paused": rt.get_bool("paused"),
                "auto_approve": rt.get_bool("auto_approve"),
                "dry_run": rt.get_bool("dry_run"),
                "queued_posts": store.count_queued_posts(),
                "stock_images": store.stock_counts()["active"],
                "knowledge_entries": store.project_knowledge_stats()["active"],
                "posting_frequency": len(rt.posting_times),
                "posting_jitter_minutes": rt.posting_jitter_minutes,
            }
        )

    @app.get("/media/stock/{token}", response_model=None)
    async def stock_media(token: str):
        """Публичный неизменяемый URL: Threads скачивает отсюда изображение."""
        if not token or len(token) > 80:
            return Response(status_code=404)
        image = store.get_stock_image_by_token(token)
        if image is None:
            return Response(status_code=404)
        try:
            path = stock_file_path(str(image["storage_name"]))
        except StockImageError:
            return Response(status_code=404)
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(
            path,
            media_type=str(image["mime_type"]),
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # ---------------------------------------------------------- OAuth
    @app.get("/auth/threads", response_class=HTMLResponse, response_model=None)
    async def auth_start(key: str | None = Query(default=None)) -> HTMLResponse | RedirectResponse:
        if not auth_key_ok(key):
            return page("<h1>403</h1><p class='err'>Неверный ключ доступа.</p>", 403)
        if not settings.threads_app_id or not settings.threads_app_secret:
            return page(
                "<h1>Не настроено</h1>"
                "<p class='err'>В .env не заданы THREADS_APP_ID / THREADS_APP_SECRET.</p>"
                "<p>Смотри README, раздел «Как получить ключ API».</p>",
                400,
            )
        state = secrets.token_urlsafe(24)
        store.add_oauth_state(state)
        url = ThreadsClient.authorize_url(settings.threads_redirect_uri, state)
        log.info("Старт OAuth Threads, redirect_uri=%s", settings.threads_redirect_uri)
        return RedirectResponse(url)

    @app.get("/auth/threads/callback", response_class=HTMLResponse)
    async def auth_callback(
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
        error_description: str | None = Query(default=None),
    ) -> HTMLResponse:
        if error:
            msg = html.escape(error_description or error)
            return page(f"<h1>Доступ не выдан</h1><p class='err'>{msg}</p>", 400)
        if not code:
            return page("<h1>Ошибка</h1><p class='err'>Meta не вернул code.</p>", 400)
        if not state or not store.pop_oauth_state(state):
            return page(
                "<h1>Ошибка состояния</h1>"
                "<p class='err'>state не найден или уже использован. "
                "Начни заново: <code>/auth/threads</code>.</p>",
                400,
            )

        code = code.strip()
        if code.endswith("#_"):
            code = code[:-2]
        try:
            short = await ThreadsClient.exchange_code(code, settings.threads_redirect_uri)
            short_token = str(short.get("access_token") or "")
            user_id = str(short.get("user_id") or "")
            if not short_token:
                raise ThreadsAPIError(f"Нет access_token в ответе: {short}")

            long = await ThreadsClient.exchange_long_lived(short_token)
            token = str(long.get("access_token") or short_token)
            expires_in = int(long.get("expires_in") or 5183944)

            rt.set("threads_access_token", token)
            if user_id:
                rt.set("threads_user_id", user_id)
            rt.set(
                "threads_token_expires_at",
                (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(
                    timespec="seconds"
                ),
            )

            username = ""
            try:
                async with ThreadsClient(rt) as client:
                    profile = await client.get_profile()
                username = str(profile.get("username") or "")
                if profile.get("id"):
                    rt.set("threads_user_id", str(profile["id"]))
                if username:
                    rt.set("threads_username", username)
            except Exception as exc:
                log.warning("Не удалось получить профиль: %s", exc)

            store.log_event(
                f"Threads подключён: @{username or user_id}, токен на "
                f"{expires_in // 86400} дн.", "info"
            )
            if service.notifier:
                await service.notifier.info(
                    f"✅ Threads подключён: @{html.escape(username or user_id)}\n"
                    f"Токен действует ~{expires_in // 86400} дней, продлевается автоматически."
                )

            return page(
                "<h1 class='ok'>Готово</h1>"
                f"<p>Аккаунт <b>@{html.escape(username or user_id)}</b> подключён.</p>"
                "<p>Токен сохранён, продление автоматическое. Можно закрыть страницу "
                "и вернуться в Telegram.</p>"
            )
        except ThreadsAPIError as exc:
            log.exception("OAuth обмен не удался")
            store.log_event(f"OAuth Threads не удался: {exc}", "error")
            return page(f"<h1>Ошибка</h1><p class='err'>{html.escape(str(exc))}</p>", 400)

    # ---------------------------------------------------------- Meta callbacks
    @app.post("/deauthorize")
    async def deauthorize(request: Request) -> JSONResponse:
        """Meta пингует при отзыве доступа пользователем."""
        rt.reset("threads_access_token")
        rt.reset("threads_token_expires_at")
        store.log_event("Пользователь отозвал доступ приложения в Threads", "warn")
        if service.notifier:
            await service.notifier.alert(
                "Доступ Threads отозван. Автопостинг остановлен — нужна переавторизация: /auth/threads"
            )
        return JSONResponse({"status": "ok"})

    @app.post("/data-deletion")
    async def data_deletion(request: Request) -> JSONResponse:
        code = secrets.token_hex(6)
        store.log_event(f"Запрос на удаление данных, код {code}", "warn")
        return JSONResponse(
            {
                "url": f"{rt.public_base_url}/data-deletion/status/{code}",
                "confirmation_code": code,
            }
        )

    @app.get("/data-deletion/status/{code}", response_class=HTMLResponse)
    async def data_deletion_status(code: str) -> HTMLResponse:
        return page(
            "<h1>Запрос на удаление данных</h1>"
            f"<p>Код <code>{html.escape(code)}</code> принят.</p>"
            "<p>Данные аккаунта удаляются из сервиса. Токены и посты стираются "
            "автоматически, история метрик — в течение 30 дней.</p>"
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        # 204 не должен иметь тела, иначе uvicorn ругается на Content-Length
        return Response(status_code=204)

    return app
