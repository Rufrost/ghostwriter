"""Эффективные настройки: .env как значения по умолчанию, БД — как переопределение.

Изменения из Telegram-бота пишутся в БД, поэтому применяются без перезапуска.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .config import settings
from .db import Store
from .prompts import DEFAULT_WRITER_SYSTEM_PROMPT

_TRUE = {"1", "true", "yes", "on", "да", "вкл"}
MAX_MULTIPOST_MODELS = 8


def parse_model_list(raw: str) -> list[str]:
    """'a/b, c-d\\n e' → ['a/b', 'c-d', 'e'] без дублей, порядок сохраняется."""
    models: list[str] = []
    for chunk in re.split(r"[,\n;\s]+", raw or ""):
        model = chunk.strip()
        if model and model not in models:
            models.append(model)
    return models


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _TRUE


def _as_int(v: Any) -> int:
    return int(str(v).strip())


def _as_float(v: Any) -> float:
    return float(str(v).replace(",", ".").strip())


def _as_str(v: Any) -> str:
    return str(v)


@dataclass(frozen=True)
class Spec:
    env_attr: str | None
    caster: Callable[[Any], Any]
    default: Any


SPECS: dict[str, Spec] = {
    # публикация
    "auto_approve": Spec("auto_approve", _as_bool, False),
    "paused": Spec(None, _as_bool, False),
    "dry_run": Spec("threads_dry_run", _as_bool, False),
    "posting_times": Spec("posting_times", _as_str, "09:00,13:00,19:00"),
    "posting_jitter_minutes": Spec("posting_jitter_minutes", _as_int, 10),
    "posting_window_start": Spec("posting_window_start", _as_str, "09:00"),
    "posting_window_end": Spec("posting_window_end", _as_str, "19:00"),
    "max_posts_per_day": Spec("max_posts_per_day", _as_int, 10),
    "min_minutes_between_posts": Spec("min_minutes_between_posts", _as_int, 60),
    "stock_images_enabled": Spec("stock_images_enabled", _as_bool, True),
    # Threads
    "threads_access_token": Spec("threads_access_token", _as_str, ""),
    "threads_user_id": Spec("threads_user_id", _as_str, ""),
    "threads_username": Spec("threads_username", _as_str, ""),
    "threads_token_expires_at": Spec(None, _as_str, ""),
    "public_base_url": Spec("public_base_url", _as_str, ""),
    # бренд
    "brand_niche": Spec("brand_niche", _as_str, ""),
    "brand_voice": Spec("brand_voice", _as_str, ""),
    "brand_audience": Spec("brand_audience", _as_str, ""),
    "brand_language": Spec("brand_language", _as_str, "ru"),
    "brand_cta": Spec("brand_cta", _as_str, ""),
    "forbidden_topics": Spec("forbidden_topics", _as_str, ""),
    "max_post_chars": Spec("max_post_chars", _as_int, 500),
    # модели
    "writer_model": Spec("writer_model", _as_str, "deepseek-chat"),
    "multipost_models": Spec("multipost_models", _as_str, ""),
    "llm_temperature": Spec("llm_temperature", _as_float, 0.8),
    "writer_system_prompt": Spec(None, _as_str, DEFAULT_WRITER_SYSTEM_PROMPT),
}


class Runtime:
    """Доступ к действующим настройкам."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._cache: dict[str, str] | None = None

    # ---------- чтение ----------
    def raw(self, key: str) -> str | None:
        return self.store.get_setting(key)

    def get(self, key: str) -> Any:
        if key not in SPECS:
            raise KeyError(f"Неизвестная настройка: {key}")
        spec = SPECS[key]
        stored = self.store.get_setting(key)
        if stored is not None:
            try:
                return spec.caster(stored)
            except (TypeError, ValueError):
                pass
        if spec.env_attr:
            env_value = getattr(settings, spec.env_attr, None)
            if env_value not in (None, ""):
                try:
                    return spec.caster(env_value)
                except (TypeError, ValueError):
                    pass
        return spec.default

    def get_str(self, key: str) -> str:
        value = self.get(key)
        return "" if value is None else str(value)

    def get_bool(self, key: str) -> bool:
        return bool(self.get(key))

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    # ---------- запись ----------
    def set(self, key: str, value: Any) -> None:
        if key not in SPECS:
            raise KeyError(f"Неизвестная настройка: {key}")
        if isinstance(value, bool):
            stored = "1" if value else "0"
        else:
            stored = str(value)
        self.store.set_setting(key, stored)

    def reset(self, key: str) -> None:
        self.store.delete_setting(key)

    # ---------- производные ----------
    @property
    def max_chars(self) -> int:
        return max(1, min(1500, self.get_int("max_post_chars")))

    @property
    def posting_times(self) -> list[tuple[int, int]]:
        from .config import parse_posting_times

        try:
            return parse_posting_times(self.get_str("posting_times"))
        except ValueError:
            return parse_posting_times(settings.posting_times)

    @property
    def posting_jitter_minutes(self) -> int:
        return max(0, min(60, self.get_int("posting_jitter_minutes")))

    @property
    def multipost_models(self) -> list[str]:
        """Список моделей для /multipost, заданный командой /models."""
        return parse_model_list(self.get_str("multipost_models"))[:MAX_MULTIPOST_MODELS]

    @property
    def token(self) -> str:
        return self.get_str("threads_access_token").strip()

    @property
    def user_id(self) -> str:
        return self.get_str("threads_user_id").strip()

    @property
    def username(self) -> str:
        return self.get_str("threads_username").strip()

    @property
    def public_base_url(self) -> str:
        return (self.get_str("public_base_url") or settings.public_base_url).rstrip("/")

    def snapshot(self) -> dict[str, Any]:
        return {key: self.get(key) for key in SPECS}
