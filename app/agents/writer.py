"""Автор — первый ИИ. Пишет текст поста под конкретную тему."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from ..db import Store
from ..llm import LLMClient, LLMError, extract_json
from ..runtime import Runtime
from ..threads_api import text_length

log = logging.getLogger(__name__)

SYSTEM = """Ты — автор постов для Threads. Пишешь от первого лица, как живой человек,
а не как пресс-релиз или маркетинговый бот.

Правила:
- Первая строка — крючок: мысль, вопрос или неожиданное утверждение, от которого
  хочется читать дальше. Без «В современном мире» и «Давайте разберёмся».
- Короткие абзацы, разговорный ритм, конкретика и примеры вместо общих слов.
- Одна мысль на пост. Не пытайся уместить всё.
- Финал — вопрос к читателю или приглашение к спору, если это уместно.
- Без хэштегов в тексте, без markdown, без кавычек вокруг всего поста.
- Не выдумывай факты, цифры и исследования. Если факт нужен, но его нет —
  говори обобщённо.
- Не повторяй формулировки из недавних постов, которые тебе покажут.

Отвечай строго валидным JSON без markdown-обёртки:
{"text": "текст поста", "topic_tag": "короткая тема без решётки или пустая строка"}"""

MAX_TOKENS = 2500  # запас на reasoning-токены «думающих» моделей

REVISE_SYSTEM = """Ты — редактор постов для Threads. Переписываешь текст по инструкции,
сохраняя смысл и голос автора. Отвечай строго JSON: {"text": "...", "topic_tag": "..."}"""


@dataclass
class WriterResult:
    text: str
    topic_tag: str = ""
    topic: str = ""
    attempts: int = 1
    raw: str = ""

    @property
    def bytes_len(self) -> int:
        return text_length(self.text)


@dataclass
class MultiWriterResult:
    """Результат одной модели в мультипосте: текст либо ошибка."""

    model: str
    result: WriterResult | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None


class Writer:
    def __init__(self, store: Store, rt: Runtime, llm: LLMClient | None = None) -> None:
        self.store = store
        self.rt = rt
        self.llm = llm or LLMClient()

    # ---------- бюджет длины ----------
    def _char_budget(self, language: str | None = None) -> int:
        """Сколько символов просить у модели, чтобы уложиться в байтовый лимит.

        Кириллица — 2 байта на символ, эмодзи — до 4. Берём запас.
        """
        max_bytes = self.rt.max_chars
        lang = (language or self.rt.get_str("brand_language")).lower()
        if lang.startswith(("ru", "uk", "be", "bg", "sr")):
            budget = max_bytes // 2 - 10
        else:
            budget = int(max_bytes * 0.8)
        return max(60, min(budget, 400))

    def _recent_texts_block(self, limit: int = 6) -> str:
        rows = self.store.recent_posts(limit=limit)
        texts = [r["text"].strip() for r in rows if r["text"] and r["status"] != "rejected"]
        if not texts:
            return "—"
        return "\n---\n".join(t[:300] for t in texts)

    # ---------- генерация ----------
    def _user_prompt(
        self,
        topic: str,
        angle: str = "",
        format_hint: str = "",
        instruction: str = "",
        language: str | None = None,
    ) -> str:
        lang = (language or self.rt.get_str("brand_language")).lower()
        budget = self._char_budget(lang)
        cta = self.rt.get_str("brand_cta")
        forbidden = self.rt.get_str("forbidden_topics")
        custom = self.rt.get_str("writer_instructions").strip()
        language_rule = (
            "ПИШИ ПОСТ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ."
            if lang.startswith("ru")
            else ""
        )
        return f"""НИША: {self.rt.get_str('brand_niche')}
ТОНАЛЬНОСТЬ: {self.rt.get_str('brand_voice')}
АУДИТОРИЯ: {self.rt.get_str('brand_audience')}
ЯЗЫК ПОСТА: {lang}
{language_rule}

ЗНАНИЯ О ПРОЕКТЕ (используй как источник фактов и контекста):
{self.store.project_knowledge_context() or '—'}

{('ПРИЗЫВ/ССЫЛКА (добавь органично, если уместно): ' + cta) if cta else ''}
{('НЕ КАСАЙСЯ ТЕМ: ' + forbidden) if forbidden else ''}
{('ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ АВТОРУ: ' + custom) if custom else ''}

ТЕМА: {topic}
{('УГОЛ ПОДАЧИ: ' + angle) if angle else ''}
{('ФОРМАТ: ' + format_hint) if format_hint else ''}
{('ДОПОЛНИТЕЛЬНО: ' + instruction) if instruction else ''}

НЕДАВНИЕ ПОСТЫ (не повторяй формулировки и заходы):
{self._recent_texts_block()}

Ограничение: {max(60, int(budget * 0.6))}–{budget} символов. Пост должен быть
законченным: крючок, мысль, финал — не одна строка и не обрывок. Жёсткий лимит
Threads — {self.rt.max_chars} байт UTF-8. Верни только JSON."""

    async def generate(
        self,
        topic: str,
        *,
        angle: str = "",
        format_hint: str = "",
        instruction: str = "",
        model: str | None = None,
        language: str | None = None,
    ) -> WriterResult:
        model_id = (model or self.rt.get_str("writer_model")).strip()
        if not model_id:
            raise LLMError("Не задана модель ИИ")
        budget = self._char_budget(language)
        raw = await self.llm.complete(
            system=SYSTEM,
            user=self._user_prompt(
                topic, angle, format_hint, instruction, language=language
            ),
            model=model_id,
            temperature=self.rt.get("llm_temperature"),
            max_tokens=MAX_TOKENS,
            json_mode=True,
        )
        text, tag = self._parse(raw)
        attempts = 1

        if text_length(text) > self.rt.max_chars:
            attempts = 2
            tighten = (
                f"Текст получился {text_length(text)} байт, а лимит — "
                f"{self.rt.max_chars} байт. Сократи примерно до {budget} символов, "
                "сохрани крючок и главную мысль, убери воду."
            )
            raw2 = await self.llm.complete(
                system=SYSTEM,
                user=self._user_prompt(
                    topic, angle, format_hint, tighten, language=language
                ),
                model=model_id,
                temperature=max(0.4, float(self.rt.get("llm_temperature")) - 0.3),
                max_tokens=MAX_TOKENS,
                json_mode=True,
            )
            text2, tag2 = self._parse(raw2)
            if text_length(text2) <= self.rt.max_chars:
                text, tag = text2, tag2
            else:
                text = self._truncate(text2)

        return WriterResult(
            text=text.strip(), topic_tag=self._clean_tag(tag), topic=topic,
            attempts=attempts, raw=raw,
        )

    async def generate_multi(
        self,
        topic: str,
        models: list[str],
        *,
        instruction: str = "",
        language: str = "ru",
        concurrency: int = 3,
    ) -> list[MultiWriterResult]:
        """Один и тот же промпт — каждой модели. Сбой одной не ломает остальные."""
        unique = list(dict.fromkeys(model.strip() for model in models if model.strip()))
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def run_one(model: str) -> MultiWriterResult:
            async with semaphore:
                try:
                    result = await self.generate(
                        topic,
                        instruction=instruction,
                        model=model,
                        language=language,
                    )
                except Exception as exc:
                    log.warning("Мультипост: модель %s не справилась: %s", model, exc)
                    return MultiWriterResult(model=model, error=str(exc)[:500])
                return MultiWriterResult(model=model, result=result)

        return list(await asyncio.gather(*(run_one(model) for model in unique)))

    async def revise(self, text: str, instruction: str) -> WriterResult:
        """Переписать существующий пост по замечанию из Telegram."""
        budget = self._char_budget()
        raw = await self.llm.complete(
            system=REVISE_SYSTEM,
            user=f"""НИША: {self.rt.get_str('brand_niche')}
ТОНАЛЬНОСТЬ: {self.rt.get_str('brand_voice')}
ЯЗЫК: {self.rt.get_str('brand_language')}

ЗНАНИЯ О ПРОЕКТЕ (сохрани фактическую точность):
{self.store.project_knowledge_context() or '—'}

{('ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ АВТОРУ: ' + self.rt.get_str('writer_instructions').strip()) if self.rt.get_str('writer_instructions').strip() else ''}

ТЕКУЩИЙ ПОСТ:
{text}

ЗАМЕЧАНИЕ: {instruction}

Не более {budget} символов (жёсткий лимит {self.rt.max_chars} байт UTF-8).
Верни только JSON.""",
            model=self.rt.get_str("writer_model"),
            temperature=self.rt.get("llm_temperature"),
            max_tokens=MAX_TOKENS,
            json_mode=True,
        )
        new_text, tag = self._parse(raw)
        if text_length(new_text) > self.rt.max_chars:
            new_text = self._truncate(new_text)
        return WriterResult(text=new_text.strip(), topic_tag=self._clean_tag(tag))

    async def paraphrase(self, text: str, instruction: str = "") -> WriterResult:
        """Создаёт самостоятельный вариант пользовательского поста."""
        request = (
            "Перефразируй пост: сохрани факты и основную мысль, но полностью обнови "
            "формулировки, ритм и крючок. Вариант должен читаться как отдельный пост."
        )
        if instruction.strip():
            request += f" Дополнительное пожелание: {instruction.strip()}"
        return await self.revise(text, request)

    # ---------- утилиты ----------
    def _parse(self, raw: str) -> tuple[str, str]:
        raw = (raw or "").strip()
        if not raw:
            raise LLMError("Модель вернула пустой ответ")
        try:
            data = extract_json(raw)
        except LLMError:
            # модель ответила просто текстом — используем как есть
            cleaned = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.S).strip()
            if not cleaned:
                raise LLMError("Модель вернула пустой ответ") from None
            return cleaned, ""
        text = str(data.get("text") or "").strip()
        if not text:
            raise LLMError(f"В ответе модели нет поля text: {raw[:200]}")
        return text, str(data.get("topic_tag") or "").strip()

    def _truncate(self, text: str) -> str:
        """Аварийное сокращение по границе предложения/слова."""
        limit = self.rt.max_chars
        if text_length(text) <= limit:
            return text
        truncated = text
        while truncated and text_length(truncated) > limit:
            cut = truncated.rfind(". ", 0, max(40, len(truncated) // 2))
            truncated = truncated[: cut + 1] if cut > 0 else truncated[: int(len(truncated) * 0.8)]
        return truncated.rstrip()

    @staticmethod
    def _clean_tag(tag: str) -> str:
        tag = tag.strip().lstrip("#")
        tag = re.sub(r"[^\w\s\-]", "", tag, flags=re.UNICODE)
        return tag.strip()[:50]


def preview_json(result: WriterResult) -> str:
    return json.dumps(
        {"text": result.text, "topic_tag": result.topic_tag, "bytes": result.bytes_len},
        ensure_ascii=False,
        indent=2,
    )
