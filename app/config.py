"""Конфигурация сервиса. Значения читаются из .env, но ключевые настройки
(токен Threads, режим автопубликации, расписание) можно менять из Telegram-бота —
они хранятся в БД и имеют приоритет над .env.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- Telegram ----------
    telegram_bot_token: str = ""
    # ID администраторов через запятую: 123456789,987654321
    telegram_admin_ids: str = ""
    telegram_polling_timeout: int = 30

    # ---------- Threads ----------
    threads_app_id: str = ""
    threads_app_secret: str = ""
    # Должен совпадать с Redirect Callback URL в настройках приложения Meta
    threads_redirect_uri: str = "http://localhost:8080/auth/threads/callback"
    threads_api_version: str = "v1.0"
    # Можно задать токен вручную; если пусто — берётся из БД (после OAuth)
    threads_access_token: str = ""
    threads_user_id: str = ""
    threads_username: str = ""
    # Сухой режим: не обращается к Threads API, посты помечаются published_dry
    threads_dry_run: bool = False

    # ---------- LLM (OpenAI-совместимый провайдер) ----------
    # openai  — любой /chat/completions (OpenAI, DeepSeek, OpenRouter, Ollama, vLLM)
    # anthropic — нативный Messages API
    llm_provider: str = "openai"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    writer_model: str = "deepseek-chat"
    # Модели для /multipost через запятую. Меняются из бота командой /models.
    multipost_models: str = ""
    llm_temperature: float = 0.8
    llm_timeout: float = 120.0

    # ---------- Контент / бренд ----------
    brand_niche: str = "технологии, продуктивность и искусственный интеллект"
    brand_voice: str = (
        "живой и дружелюбный тон, без канцелярита; короткие абзацы; "
        "конкретика вместо общих слов; допустима лёгкая ирония"
    )
    brand_language: str = "ru"
    brand_audience: str = "предприниматели, разработчики и авторы проектов 25–40 лет"
    brand_cta: str = ""
    forbidden_topics: str = ""
    max_post_chars: int = 500

    # ---------- Постоянный сток изображений ----------
    # Файлы лежат внутри data/, чтобы systemd разрешал запись и бэкап их подхватывал.
    stock_media_dir: str = "data/stock"
    stock_images_enabled: bool = True

    # ---------- Режим публикации ----------
    # false — каждый пост сначала приходит в Telegram на подтверждение
    auto_approve: bool = False
    posting_times: str = "09:00,13:00,19:00"
    # Каждый базовый слот сдвигается случайно в диапазоне ±N минут.
    posting_jitter_minutes: int = 10
    # Окно, внутри которого /frequency равномерно распределяет слоты.
    posting_window_start: str = "09:00"
    posting_window_end: str = "19:00"
    timezone: str = "Europe/Moscow"
    max_posts_per_day: int = 10
    min_minutes_between_posts: int = 60

    # ---------- Web / OAuth ----------
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    # Секрет для входа в /auth/threads?key=... (обязателен в проде)
    web_auth_key: str = ""
    # Публичный адрес сервера, например https://bot.example.com
    public_base_url: str = "http://localhost:8080"

    # ---------- Прочее ----------
    db_path: str = "data/ghostwriter.db"
    log_level: str = "INFO"

    @field_validator("max_post_chars")
    @classmethod
    def _check_chars(cls, v: int) -> int:
        if not 1 <= v <= 500:
            raise ValueError("max_post_chars должен быть в диапазоне 1..500")
        return v

    @field_validator("posting_jitter_minutes")
    @classmethod
    def _check_jitter(cls, value: int) -> int:
        if not 0 <= value <= 60:
            raise ValueError("posting_jitter_minutes должен быть в диапазоне 0..60")
        return value

    @property
    def admin_ids(self) -> set[int]:
        result: set[int] = set()
        for chunk in self.telegram_admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    result.add(int(chunk))
                except ValueError:
                    pass
        return result

    @property
    def posting_times_list(self) -> list[tuple[int, int]]:
        return parse_posting_times(self.posting_times)

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def stock_dir(self) -> Path:
        p = Path(self.stock_media_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def threads_graph_url(self) -> str:
        return f"https://graph.threads.net/{self.threads_api_version}"

    def web_url(self, path: str) -> str:
        return self.public_base_url.rstrip("/") + "/" + path.lstrip("/")


def parse_posting_times(raw: str) -> list[tuple[int, int]]:
    """'09:00, 13:00,19:30' -> [(9,0),(13,0),(19,30)] с валидацией."""
    times: list[tuple[int, int]] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.replace(".", ":").split(":")
        if len(parts) != 2:
            raise ValueError(f"Неверное время: {chunk!r}. Ожидается HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Неверное время: {chunk!r}")
        pair = (hour, minute)
        if pair not in times:
            times.append(pair)
    if not times:
        raise ValueError("Список времён публикации пуст")
    return sorted(times)


def parse_clock(raw: str) -> tuple[int, int]:
    """Один HH:MM → (hour, minute)."""
    parsed = parse_posting_times(raw)
    if len(parsed) != 1:
        raise ValueError(f"Ожидается одно время HH:MM, получено: {raw!r}")
    return parsed[0]


def distribute_posting_times(
    count: int, start: str = "09:00", end: str = "19:00"
) -> list[tuple[int, int]]:
    """Равномерно распределяет N базовых слотов внутри дневного окна."""
    if not 1 <= count <= 10:
        raise ValueError("Частота должна быть от 1 до 10 постов в день")
    start_h, start_m = parse_clock(start)
    end_h, end_m = parse_clock(end)
    first = start_h * 60 + start_m
    last = end_h * 60 + end_m
    if last <= first:
        raise ValueError("Конец окна должен быть позже начала в пределах одного дня")
    if count == 1:
        minute_values = [round((first + last) / 2)]
    else:
        span = last - first
        if span < count - 1:
            raise ValueError("Временное окно слишком короткое для такой частоты")
        minute_values = [round(first + i * span / (count - 1)) for i in range(count)]
    result = [(value // 60, value % 60) for value in minute_values]
    if len(set(result)) != count:
        raise ValueError("Не удалось распределить уникальные слоты в этом окне")
    return result


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
