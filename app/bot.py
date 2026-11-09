"""Telegram-бот: управление сервисом, согласование постов, настройки."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
from io import BytesIO
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    TelegramObject,
)

from .config import distribute_posting_times, settings
from .db import metrics_dict
from .runtime import SPECS, Runtime
from .service import ContentService
from .stock import (
    MAX_IMAGE_BYTES,
    StockImageError,
    inspect_image,
    stock_file_path,
    write_stock_file,
)
from .threads_api import engagement_score, text_length

log = logging.getLogger(__name__)

HELP = """<b>Ghostwriter — автопостинг в Threads</b>

<b>Контент</b>
/post [тема] — сгенерировать пост Автором прямо сейчас
/queue — очередь готовых постов для общего расписания
/pending — посты, ожидающие подтверждения
/author — настройки ИИ-автора
/memory — память о проекте: добавить и редактировать знания
/stock — постоянный сток изображений и управление им

<b>Управление</b>
/menu — открыть список команд
/status — состояние сервиса
/analytics — метрики постов и аккаунта
/frequency [N] — количество слотов в день (1–10)
/jitter [минуты] — случайное отклонение времени, по умолчанию ±10
/schedule [09:00,13:00,19:00] — показать или изменить базовые слоты
/auto on|off — публиковать сразу или с подтверждением
/pause, /resume — остановить / возобновить автопостинг
/dryrun on|off — сухой режим (без реальной публикации)

<b>Настройки</b>
/settings — текущие параметры
/set ключ значение — изменить параметр
/reset ключ — вернуть значение из .env
/model — посмотреть или переключить модель Автора
/token — подключение аккаунта Threads
/logs — последние события

В /queue можно добавить сразу несколько постов, разделив их строкой <code>---</code>.
Каждый пост можно изменить, опубликовать сейчас или перефразировать Автором."""


BOT_COMMANDS = [
    BotCommand(command="menu", description="Все команды бота"),
    BotCommand(command="help", description="Полная справка по командам"),
    BotCommand(command="post", description="Сгенерировать пост Автором"),
    BotCommand(command="queue", description="Очередь готовых постов"),
    BotCommand(command="pending", description="Посты на согласовании"),
    BotCommand(command="author", description="Настройки ИИ-автора"),
    BotCommand(command="memory", description="Память и знания о проекте"),
    BotCommand(command="stock", description="Сток изображений"),
    BotCommand(command="status", description="Состояние сервиса"),
    BotCommand(command="analytics", description="Метрики и аналитика"),
    BotCommand(command="frequency", description="Количество слотов в день"),
    BotCommand(command="jitter", description="Разброс времени ±N минут"),
    BotCommand(command="schedule", description="Базовые времена публикаций"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="model", description="Модель ИИ-автора"),
    BotCommand(command="dryrun", description="Тестовый режим без публикации"),
    BotCommand(command="auto", description="Автопубликация on/off"),
    BotCommand(command="pause", description="Поставить автопостинг на паузу"),
    BotCommand(command="resume", description="Возобновить автопостинг"),
    BotCommand(command="token", description="Подключение Threads"),
    BotCommand(command="logs", description="Последние события"),
]


async def configure_bot_menu(bot: Bot) -> None:
    """Постоянная нативная кнопка «Меню» и список команд Telegram."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as exc:
        log.warning("Не удалось настроить меню Telegram: %s", exc)


class Editing(StatesGroup):
    instruction = State()


class ProjectKnowledgeEditing(StatesGroup):
    adding = State()
    editing = State()


class QueuedPostEditing(StatesGroup):
    adding = State()
    editing = State()
    rephrasing = State()


class AgentSettingEditing(StatesGroup):
    value = State()


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


# ---------------------------------------------------------------- фильтр админов
class AdminMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: set[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if not self.admin_ids:
            text = (
                f"Ваш Telegram ID: <code>{user.id}</code>\n"
                "Добавьте его в <code>TELEGRAM_ADMIN_IDS</code> в файле .env "
                "и перезапустите сервис."
            )
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return None
        if user.id not in self.admin_ids:
            if isinstance(event, Message):
                await event.answer(f"⛔ Доступ закрыт. Ваш ID: <code>{user.id}</code>")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Нет доступа", show_alert=True)
            return None
        return await handler(event, data)


# ---------------------------------------------------------------- уведомления
class BotNotifier:
    def __init__(self, bot: Bot, admin_ids: set[int]) -> None:
        self.bot = bot
        self.admin_ids = admin_ids

    async def _send(self, text: str, **kwargs: Any) -> None:
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, **kwargs)
            except Exception as exc:  # админ заблокировал бота и т.п.
                log.warning("Не удалось отправить сообщение %s: %s", admin_id, exc)

    async def _send_photo(self, path: str, caption: str, **kwargs: Any) -> None:
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_photo(
                    admin_id, FSInputFile(path), caption=caption, **kwargs
                )
            except Exception as exc:
                log.warning("Не удалось отправить изображение %s: %s", admin_id, exc)
                try:
                    await self.bot.send_message(admin_id, caption, **kwargs)
                except Exception as fallback_exc:
                    log.warning("Не удалось отправить текстовый fallback %s: %s", admin_id, fallback_exc)

    async def info(self, message: str) -> None:
        await self._send(message)

    async def alert(self, message: str) -> None:
        await self._send(f"⚠️ {message}")

    async def post_for_approval(
        self, post_id: int, text: str, topic: str, meta: dict[str, Any]
    ) -> None:
        body = (
            f"📝 <b>Пост #{post_id}</b> — на согласование\n"
            f"<b>Тема:</b> {esc(topic)}\n"
            f"<b>Длина:</b> {meta.get('bytes', text_length(text))}/{meta.get('limit', 500)} байт"
            + (f" · попыток: {meta['attempts']}" if meta.get("attempts", 1) > 1 else "")
            + (
                f"\n<b>Изображение:</b> #{meta['image_id']} из медиастока"
                if meta.get("image_id") else ""
            )
            + "\n\n"
            f"<blockquote>{esc(text)}</blockquote>"
        )
        image_path = str(meta.get("image_path") or "")
        if image_path:
            await self._send_photo(
                image_path, body, reply_markup=approval_keyboard(post_id)
            )
        else:
            await self._send(
                body,
                reply_markup=approval_keyboard(post_id),
                disable_web_page_preview=True,
            )


def approval_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub:{post_id}"),
                InlineKeyboardButton(text="🔄 Переписать", callback_data=f"regen:{post_id}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Доработать", callback_data=f"edit:{post_id}"),
                InlineKeyboardButton(text="🚫 Пропустить", callback_data=f"skip:{post_id}"),
            ],
        ]
    )


# ---------------------------------------------------------------- сборка
def build_dispatcher(
    service: ContentService, rt: Runtime, scheduler: Any, admin_ids: set[int]
) -> tuple[Dispatcher, BotNotifier, Bot]:
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=MemoryStorage())
    notifier = BotNotifier(bot, admin_ids)
    service.notifier = notifier

    dp.message.outer_middleware(AdminMiddleware(admin_ids))
    dp.callback_query.outer_middleware(AdminMiddleware(admin_ids))

    router = Router()
    store = service.store


    def stock_text() -> str:
        counts = store.stock_counts()
        mode = "включена" if rt.get_bool("stock_images_enabled") else "выключена"
        lines = [
            "<b>🖼 Постоянный медиасток</b>",
            f"Автоподстановка: <b>{mode}</b>",
            f"Изображений: <b>{counts['active']}</b> активных из {counts['total']}",
            f"Всего назначений постам: {counts['uses']}",
            "",
            "Отправьте боту JPEG или PNG — файл сохранится на сервере и будет "
            "автоматически использоваться в новых постах.",
            "",
            "Управление:",
            "<code>/stock on</code> или <code>/stock off</code>",
            "<code>/stock disable ID</code> — убрать изображение из ротации",
            "<code>/stock enable ID</code> — вернуть в ротацию",
        ]
        recent = store.list_stock_images(limit=10)
        if recent:
            lines.append("\n<b>Последние изображения</b>")
            for image in recent:
                state = "✅" if image["active"] else "⏸"
                note = str(image["caption"] or "").replace("\n", " ").strip()
                note = f" · {esc(note[:60])}" if note else ""
                lines.append(
                    f"{state} <code>#{image['id']}</code> · "
                    f"{image['width']}×{image['height']} · "
                    f"исп. {image['usage_count']}{note}"
                )
        return "\n".join(lines)

    async def save_stock_upload(
        message: Message, file_id: str, file_size: int | None
    ) -> None:
        if file_size and file_size > MAX_IMAGE_BYTES:
            await message.answer("❌ Файл больше 8 МБ — Threads его не примет.")
            return
        buffer = BytesIO()
        try:
            await bot.download(file_id, destination=buffer, timeout=60)
            data = buffer.getvalue()
            info = inspect_image(data)
        except StockImageError as exc:
            await message.answer(f"❌ {esc(exc)}")
            return
        except Exception as exc:
            log.exception("Не удалось скачать изображение из Telegram")
            await message.answer(f"❌ Не удалось скачать изображение: {esc(exc)}")
            return

        existing = store.get_stock_image_by_hash(info.sha256)
        if existing is not None:
            await message.answer(
                f"ℹ️ Это изображение уже есть в медиастоке: "
                f"<code>#{existing['id']}</code>."
            )
            return

        caption = (message.caption or "").strip()
        if caption.startswith("/stock"):
            caption = caption[len("/stock") :].strip()
        token = secrets.token_urlsafe(18)
        storage_name = f"{token}.{info.extension}"
        path = await asyncio.to_thread(write_stock_file, storage_name, data)
        try:
            image_id, created = store.add_stock_image(
                token=token,
                storage_name=storage_name,
                mime_type=info.mime_type,
                size_bytes=info.size_bytes,
                width=info.width,
                height=info.height,
                sha256=info.sha256,
                telegram_file_id=file_id,
                caption=caption,
            )
        except Exception as exc:
            path.unlink(missing_ok=True)
            log.exception("Не удалось зарегистрировать изображение в медиастоке")
            await message.answer(f"❌ Не удалось сохранить изображение: {esc(exc)}")
            return
        if not created:
            path.unlink(missing_ok=True)
            await message.answer(f"ℹ️ Это изображение уже есть: <code>#{image_id}</code>.")
            return
        store.log_event(
            f"В медиасток добавлено изображение #{image_id} ({info.width}×{info.height})",
            "info",
        )
        await message.answer(
            f"✅ Изображение <code>#{image_id}</code> добавлено в постоянный медиасток.\n"
            f"Размер: {info.width}×{info.height}, {info.size_bytes / 1024:.0f} КБ.\n"
            f"Активных изображений: {store.stock_counts()['active']}.\n\n"
            "Оно будет автоматически участвовать в ротации новых постов."
        )

    def parse_knowledge_input(raw: str) -> tuple[str, str]:
        raw = raw.strip()
        if not raw:
            raise ValueError("Сообщение пустое")
        first, separator, rest = raw.partition("\n")
        if separator and rest.strip():
            title, content = first.strip(), rest.strip()
        else:
            title, content = "О проекте", raw
        if not title or not content:
            raise ValueError("Нужны название и текст")
        return title[:120], content[:3500]

    def escaped_preview(value: Any, limit: int = 3000) -> str:
        raw = str(value or "")
        shortened = False
        while len(esc(raw)) > limit and raw:
            raw = raw[: max(1, int(len(raw) * 0.8))]
            shortened = True
        return esc(raw.rstrip()) + ("…" if shortened else "")

    def memory_panel_text() -> str:
        stats = store.project_knowledge_stats()
        if not stats["total"]:
            return (
                "<b>🧠 Память о проекте</b>\n\n"
                "Пока пусто. Добавьте сведения о продукте, клиентах, преимуществах, "
                "ограничениях, фактах и вашем личном опыте. Автор будет "
                "учитывать активные записи при генерации и перефразировании постов."
            )
        return (
            "<b>🧠 Память о проекте</b>\n"
            f"Активно: <b>{stats['active']}</b> из {stats['total']} записей · "
            f"{stats['chars']} символов\n\n"
            "Нажмите запись, чтобы открыть, изменить, отключить или удалить её."
        )

    def memory_panel_keyboard() -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(text="➕ Добавить знания", callback_data="memory:add")]
        ]
        for item in store.list_project_knowledge(limit=30):
            state = "✅" if item["active"] else "⏸"
            title = str(item["title"])
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{state} #{item['id']} · {title[:42]}",
                        callback_data=f"memory:view:{item['id']}",
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def memory_entry_text(entry: Any) -> str:
        state = "активна — учитывается ИИ" if entry["active"] else "отключена"
        return (
            f"<b>🧠 Запись #{entry['id']}: {esc(entry['title'])}</b>\n"
            f"Статус: <b>{state}</b>\n\n"
            f"<blockquote>{escaped_preview(entry['content'])}</blockquote>"
        )

    def memory_entry_keyboard(entry: Any) -> InlineKeyboardMarkup:
        knowledge_id = int(entry["id"])
        toggle_label = "⏸ Отключить" if entry["active"] else "✅ Включить"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Редактировать",
                        callback_data=f"memory:edit:{knowledge_id}",
                    ),
                    InlineKeyboardButton(
                        text=toggle_label,
                        callback_data=f"memory:toggle:{knowledge_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить", callback_data=f"memory:delete:{knowledge_id}"
                    )
                ],
                [InlineKeyboardButton(text="← Все записи", callback_data="memory:list")],
            ]
        )

    def parse_queue_input(raw: str) -> list[str]:
        parts = re.split(r"(?m)^\s*---\s*$", raw.strip())
        return [part.strip() for part in parts if part.strip()]

    def queue_panel_text() -> str:
        count = store.count_queued_posts()
        times = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in rt.posting_times)
        mode = "автопубликация" if rt.get_bool("auto_approve") else "с подтверждением"
        return (
            "<b>📚 Очередь готовых постов</b>\n"
            f"Ожидают расписания: <b>{count}</b>\n"
            f"Слоты: <b>{times}</b> · {mode}\n\n"
            "Посты идут сверху вниз. В каждый слот берётся один пост. "
            "Если очередь пуста, слот пропускается без генерации LLM."
        )

    def queue_panel_keyboard() -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text="➕ Добавить посты", callback_data="queue:add")]]
        for position, post in enumerate(store.queued_posts(limit=30), start=1):
            preview = str(post["text"]).replace("\n", " ").strip()
            source = "✨" if post["source"] == "rephrased" else "📝"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{source} {position}. #{post['id']} · {preview[:38]}",
                        callback_data=f"queue:view:{post['id']}",
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def queued_post_text(post: Any) -> str:
        source = "вариант Автора" if post["source"] == "rephrased" else "задан пользователем"
        size = text_length(str(post["text"]))
        return (
            f"<b>📝 Пост #{post['id']}</b> · {source}\n"
            f"Длина: <b>{size}/{rt.max_chars} байт</b>\n\n"
            f"<blockquote>{escaped_preview(post['text'], 3200)}</blockquote>"
        )

    def queued_post_keyboard(post_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✨ Перефразировать", callback_data=f"queue:rephrase:{post_id}"
                    ),
                    InlineKeyboardButton(
                        text="✏️ Изменить", callback_data=f"queue:edit:{post_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🚀 Опубликовать сейчас", callback_data=f"queue:publish:{post_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить", callback_data=f"queue:delete:{post_id}"
                    ),
                    InlineKeyboardButton(text="← Очередь", callback_data="queue:list"),
                ],
            ]
        )

    AUTHOR_FIELDS: list[tuple[str, str]] = [
        ("writer_model", "Модель"),
        ("llm_temperature", "Креативность"),
        ("brand_niche", "Ниша"),
        ("brand_voice", "Тональность"),
        ("brand_audience", "Аудитория"),
        ("brand_language", "Язык"),
        ("brand_cta", "Призыв / ссылка"),
        ("forbidden_topics", "Запрещённые темы"),
        ("max_post_chars", "Лимит байт"),
        ("writer_instructions", "Инструкции автору"),
    ]

    def author_panel_text() -> str:
        lines = ["<b>✍️ Настройки Автора</b>", ""]
        for key, label in AUTHOR_FIELDS:
            value = rt.get(key)
            rendered = str(value).replace("\n", " ").strip() or "—"
            if len(rendered) > 90:
                rendered = rendered[:89] + "…"
            lines.append(f"<b>{esc(label)}:</b> {esc(rendered)}")
        lines.append("\nНажмите параметр, затем отправьте новое значение. «-» очищает текстовое поле.")
        return "\n".join(lines)

    def author_panel_keyboard() -> InlineKeyboardMarkup:
        rows = []
        for index in range(0, len(AUTHOR_FIELDS), 2):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✏️ {label}", callback_data=f"author:set:{key}"
                    )
                    for key, label in AUTHOR_FIELDS[index : index + 2]
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def validate_author_setting(key: str, raw: str) -> Any:
        value = raw.strip()
        if value == "-":
            value = ""
        if key in {"writer_model", "brand_language"} and not value:
            raise ValueError("Значение не может быть пустым")
        if key == "llm_temperature":
            number = float(value.replace(",", "."))
            if not 0 <= number <= 2:
                raise ValueError("Креативность должна быть от 0 до 2")
            return number
        if key == "max_post_chars":
            number = int(value)
            if not 1 <= number <= 500:
                raise ValueError("Лимит должен быть от 1 до 500 байт")
            return number
        return value[:4000]

    async def replace_result_message(
        message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        """Редактирует и текстовое сообщение, и подпись у фото-превью."""
        if message.photo or message.document or message.video:
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(text, reply_markup=reply_markup)

    # ---------- /start и /help ----------
    def help_landing_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📖 Открыть полную справку",
                        url=f"{rt.public_base_url}/help",
                    )
                ]
            ]
        )

    @router.message(Command("start", "help", "menu"))
    async def cmd_start(message: Message) -> None:
        await message.answer(HELP, reply_markup=help_landing_keyboard())

    # ---------- редактируемая память о проекте ----------
    @router.message(Command("memory", "project"))
    async def cmd_memory(message: Message) -> None:
        await message.answer(
            memory_panel_text(), reply_markup=memory_panel_keyboard()
        )

    @router.callback_query(F.data == "memory:list")
    async def cb_memory_list(cb: CallbackQuery) -> None:
        await cb.answer()
        if cb.message:
            await cb.message.edit_text(
                memory_panel_text(), reply_markup=memory_panel_keyboard()
            )

    @router.callback_query(F.data == "memory:add")
    async def cb_memory_add(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ProjectKnowledgeEditing.adding)
        await cb.answer()
        if cb.message:
            await cb.message.answer(
                "🧠 Отправьте знания одним текстовым сообщением.\n\n"
                "Первая строка — название раздела, со второй строки — факты.\n\n"
                "Например:\n<code>О продукте\nGhostwriter помогает автоматически "
                "готовить и публиковать посты...</code>\n\n"
                "Можно добавить клиентов, преимущества, ограничения, историю, "
                "тон общения и факты, которые нельзя выдумывать.\n"
                "/cancel — отмена."
            )

    @router.message(ProjectKnowledgeEditing.adding)
    async def on_memory_add(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.lower() in {"/cancel", "отмена"}:
            await state.clear()
            await message.answer("Добавление отменено.")
            return
        try:
            title, content = parse_knowledge_input(raw)
            knowledge_id = store.add_project_knowledge(title, content)
        except ValueError as exc:
            await message.answer(f"❌ {esc(exc)}. Отправьте текст ещё раз или /cancel.")
            return
        await state.clear()
        store.log_event(f"В память проекта добавлена запись #{knowledge_id}: {title}")
        await message.answer(
            f"✅ Запись <code>#{knowledge_id}</code> сохранена. "
            "Автор уже будет учитывать её при генерации и перефразировании."
        )
        await message.answer(
            memory_panel_text(), reply_markup=memory_panel_keyboard()
        )

    @router.callback_query(F.data.startswith("memory:view:"))
    async def cb_memory_view(cb: CallbackQuery) -> None:
        knowledge_id = int(cb.data.rsplit(":", 1)[1])
        entry = store.get_project_knowledge(knowledge_id)
        await cb.answer()
        if not cb.message:
            return
        if entry is None:
            await cb.message.edit_text(
                "Запись уже удалена.", reply_markup=memory_panel_keyboard()
            )
            return
        await cb.message.edit_text(
            memory_entry_text(entry), reply_markup=memory_entry_keyboard(entry)
        )

    @router.callback_query(F.data.startswith("memory:edit:"))
    async def cb_memory_edit(cb: CallbackQuery, state: FSMContext) -> None:
        knowledge_id = int(cb.data.rsplit(":", 1)[1])
        entry = store.get_project_knowledge(knowledge_id)
        if entry is None:
            await cb.answer("Запись не найдена", show_alert=True)
            return
        await state.set_state(ProjectKnowledgeEditing.editing)
        await state.update_data(knowledge_id=knowledge_id)
        await cb.answer()
        if cb.message:
            await cb.message.answer(
                f"✏️ Отправьте новую версию записи #{knowledge_id}.\n"
                "Первая строка — название, далее — полный новый текст.\n"
                "/cancel — оставить без изменений."
            )

    @router.message(ProjectKnowledgeEditing.editing)
    async def on_memory_edit(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.lower() in {"/cancel", "отмена"}:
            await state.clear()
            await message.answer("Изменение отменено.")
            return
        data = await state.get_data()
        knowledge_id = int(data.get("knowledge_id") or 0)
        try:
            title, content = parse_knowledge_input(raw)
            updated = store.update_project_knowledge(
                knowledge_id, title=title, content=content
            )
        except ValueError as exc:
            await message.answer(f"❌ {esc(exc)}. Отправьте текст ещё раз или /cancel.")
            return
        await state.clear()
        if not updated:
            await message.answer("❌ Запись уже удалена.")
            return
        store.log_event(f"Изменена запись памяти проекта #{knowledge_id}: {title}")
        entry = store.get_project_knowledge(knowledge_id)
        await message.answer(
            "✅ Память обновлена. Новая версия применяется к следующим запросам ИИ."
        )
        if entry is not None:
            await message.answer(
                memory_entry_text(entry), reply_markup=memory_entry_keyboard(entry)
            )

    @router.callback_query(F.data.startswith("memory:toggle:"))
    async def cb_memory_toggle(cb: CallbackQuery) -> None:
        knowledge_id = int(cb.data.rsplit(":", 1)[1])
        entry = store.get_project_knowledge(knowledge_id)
        if entry is None:
            await cb.answer("Запись не найдена", show_alert=True)
            return
        active = not bool(entry["active"])
        store.set_project_knowledge_active(knowledge_id, active)
        entry = store.get_project_knowledge(knowledge_id)
        await cb.answer("Включено" if active else "Отключено")
        if cb.message and entry is not None:
            await cb.message.edit_text(
                memory_entry_text(entry), reply_markup=memory_entry_keyboard(entry)
            )

    @router.callback_query(F.data.regexp(r"^memory:delete:\d+$"))
    async def cb_memory_delete(cb: CallbackQuery) -> None:
        knowledge_id = int(cb.data.rsplit(":", 1)[1])
        entry = store.get_project_knowledge(knowledge_id)
        if entry is None:
            await cb.answer("Запись не найдена", show_alert=True)
            return
        await cb.answer()
        if cb.message:
            await cb.message.edit_text(
                f"Удалить запись <b>#{knowledge_id} · {esc(entry['title'])}</b>?\n"
                "Это действие нельзя отменить.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🗑 Да, удалить",
                                callback_data=f"memory:delete-confirm:{knowledge_id}",
                            ),
                            InlineKeyboardButton(
                                text="Отмена", callback_data=f"memory:view:{knowledge_id}"
                            ),
                        ]
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("memory:delete-confirm:"))
    async def cb_memory_delete_confirm(cb: CallbackQuery) -> None:
        knowledge_id = int(cb.data.rsplit(":", 1)[1])
        deleted = store.delete_project_knowledge(knowledge_id)
        if deleted:
            store.log_event(f"Удалена запись памяти проекта #{knowledge_id}", "warn")
        await cb.answer("Удалено" if deleted else "Запись уже удалена")
        if cb.message:
            await cb.message.edit_text(
                memory_panel_text(), reply_markup=memory_panel_keyboard()
            )

    # ---------- /status ----------
    def status_text() -> str:
        s = service.summary()
        account = f"@{esc(s['username'])}" if s["username"] else "не подключён"
        check = s["check"]
        mode = []
        mode.append("авто" if s["auto_approve"] else "с подтверждением")
        if s["paused"]:
            mode.append("⏸ пауза")
        if s["dry_run"]:
            mode.append("🧪 сухой режим")
        totals = s["totals"]
        token_exp = rt.get_str("threads_token_expires_at")
        token_line = ""
        if token_exp:
            try:
                dt = datetime.fromisoformat(token_exp)
                token_line = f"\n<b>Токен:</b> до {dt.astimezone().strftime('%d.%m.%Y')}"
            except ValueError:
                token_line = ""
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in s["posting_times"])
        return (
            f"<b>🤖 Ghostwriter</b>\n"
            f"<b>Аккаунт:</b> {account}\n"
            f"<b>Режим:</b> {', '.join(mode)}\n"
            f"<b>Очередь постов:</b> {s['queued_posts']} · "
            f"<b>На согласовании:</b> {s['pending']} · <b>Ошибок:</b> {s['failed']}\n"
            f"<b>Медиасток:</b> {s['stock']['active']}/{s['stock']['total']} активных"
            f" · {'включён' if s['stock_enabled'] else 'выключен'}\n"
            f"<b>Память проекта:</b> {s['knowledge']['active']}/{s['knowledge']['total']} активных записей\n"
            f"<b>Опубликовано (в выборке):</b> {s['published_recent']}\n"
            f"<b>Вовлечённость:</b> 👁 {totals['views']} · ❤️ {totals['likes']} · "
            f"💬 {totals['replies']} · 🔁 {totals['reposts']}\n"
            f"<b>Расписание:</b> {len(s['posting_times'])} пост./день · "
            f"база {times} · случайно ±{rt.posting_jitter_minutes} мин "
            f"({esc(s['tz'])}){token_line}\n"
            f"<b>Готовность:</b> {'✅ можно публиковать' if check.ok else '⏳ ' + esc(check.reason)}"
        )

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await message.answer(
            status_text(),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Обновить", callback_data="status:refresh")]
                ]
            ),
        )

    @router.callback_query(F.data == "status:refresh")
    async def cb_status(cb: CallbackQuery) -> None:
        await cb.answer()
        if cb.message:
            await cb.message.edit_text(status_text())

    # ---------- постоянный медиасток ----------
    @router.message(Command("stock"))
    async def cmd_stock(message: Message, command: CommandObject) -> None:
        # Можно прислать фото с подписью /stock — оно сразу попадёт в фонд.
        if message.photo:
            photo = message.photo[-1]
            await save_stock_upload(message, photo.file_id, photo.file_size)
            return
        if message.document:
            document = message.document
            await save_stock_upload(message, document.file_id, document.file_size)
            return

        raw = (command.args or "").strip().lower()
        if not raw:
            await message.answer(stock_text())
            return
        if raw in {"on", "вкл", "1"}:
            rt.set("stock_images_enabled", True)
            await message.answer("✅ Автоподстановка изображений из медиастока включена.")
            return
        if raw in {"off", "выкл", "0"}:
            rt.set("stock_images_enabled", False)
            await message.answer(
                "⏸ Автоподстановка выключена. Сами изображения сохранены на сервере."
            )
            return

        parts = raw.split()
        if len(parts) == 2 and parts[0] in {"enable", "disable", "включить", "отключить"}:
            try:
                image_id = int(parts[1].lstrip("#"))
            except ValueError:
                await message.answer("❌ ID изображения должен быть числом.")
                return
            active = parts[0] in {"enable", "включить"}
            if not store.set_stock_image_active(image_id, active):
                await message.answer(f"❌ Изображение #{image_id} не найдено.")
                return
            await message.answer(
                f"{'✅ Возвращено в ротацию' if active else '⏸ Убрано из ротации'}: "
                f"изображение <code>#{image_id}</code>."
            )
            return
        await message.answer(stock_text())

    @router.message(F.photo)
    async def add_stock_photo(message: Message) -> None:
        photo = message.photo[-1]
        await save_stock_upload(message, photo.file_id, photo.file_size)

    @router.message(F.document)
    async def add_stock_document(message: Message) -> None:
        document = message.document
        if not document:
            return
        if document.mime_type not in {"image/jpeg", "image/png"}:
            await message.answer("❌ Для медиастока отправьте документ в формате JPEG или PNG.")
            return
        await save_stock_upload(message, document.file_id, document.file_size)

    # ---------- /post и /next ----------
    @router.message(Command("post", "next"))
    async def cmd_post(message: Message, command: CommandObject) -> None:
        topic_text = (command.args or "").strip()
        if not topic_text:
            await message.answer(
                "Укажите тему для Автора. Например:\n"
                "<code>/post Почему автоматизация не заменяет эксперта</code>\n\n"
                "Готовые тексты для расписания добавляются через /queue."
            )
            return
        note = await message.answer("⏳ Автор генерирует пост…")
        try:
            await service.create_post(topic=topic_text[:500])
            await note.edit_text("Готово — пост отправлен выше.")
        except Exception as exc:
            log.exception("Ошибка генерации")
            await note.edit_text(f"❌ Не получилось: {esc(exc)}")

    # ---------- /pending ----------
    @router.message(Command("pending"))
    async def cmd_pending(message: Message) -> None:
        rows = [p for p in store.recent_posts(50) if p["status"] == "pending_approval"]
        if not rows:
            await message.answer("Нет постов на согласовании.")
            return
        for post in rows:
            post_id = int(post["id"])
            image = store.get_post_image(post_id)
            body = (
                f"📝 <b>Пост #{post_id}</b> · {esc(post['topic'])}"
                + (f" · 🖼 #{image['id']}" if image is not None else "")
                + f"\n\n<blockquote>{esc(post['text'])}</blockquote>"
            )
            if image is not None:
                path = stock_file_path(str(image["storage_name"]))
            else:
                path = None
            if path is not None and path.is_file():
                await message.answer_photo(
                    FSInputFile(path),
                    caption=body,
                    reply_markup=approval_keyboard(post_id),
                )
            else:
                await message.answer(
                    body,
                    reply_markup=approval_keyboard(post_id),
                    disable_web_page_preview=True,
                )

    # ---------- очередь готовых постов ----------
    @router.message(Command("queue", "posts"))
    async def cmd_queue(message: Message) -> None:
        await message.answer(queue_panel_text(), reply_markup=queue_panel_keyboard())

    @router.callback_query(F.data == "queue:list")
    async def cb_queue_list(cb: CallbackQuery) -> None:
        await cb.answer()
        if cb.message:
            await cb.message.edit_text(
                queue_panel_text(), reply_markup=queue_panel_keyboard()
            )

    @router.callback_query(F.data == "queue:add")
    async def cb_queue_add(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(QueuedPostEditing.adding)
        await cb.answer()
        if cb.message:
            await cb.message.answer(
                "➕ Отправьте один или несколько готовых постов.\n\n"
                "Чтобы добавить несколько постов одним сообщением, разделяйте их "
                "отдельной строкой <code>---</code>:\n\n"
                "<code>Первый пост\n---\nВторой пост\n---\nТретий пост</code>\n\n"
                f"Каждый пост — не более {rt.max_chars} байт. /cancel — отмена."
            )

    @router.message(QueuedPostEditing.adding)
    async def on_queue_add(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.lower() in {"/cancel", "отмена"}:
            await state.clear()
            await message.answer("Добавление отменено.")
            return
        try:
            ids = service.queue_user_posts(parse_queue_input(raw))
        except ValueError as exc:
            await message.answer(f"❌ {esc(exc)}. Исправьте текст или /cancel.")
            return
        await state.clear()
        await message.answer(
            f"✅ Добавлено постов: <b>{len(ids)}</b>. "
            f"В очереди теперь: <b>{store.count_queued_posts()}</b>."
        )
        await message.answer(queue_panel_text(), reply_markup=queue_panel_keyboard())

    @router.callback_query(F.data.startswith("queue:view:"))
    async def cb_queue_view(cb: CallbackQuery) -> None:
        post_id = int(cb.data.rsplit(":", 1)[1])
        post = store.get_post(post_id)
        await cb.answer()
        if not cb.message:
            return
        if post is None or post["status"] != "queued":
            await cb.message.edit_text(
                "Пост уже обработан или удалён.", reply_markup=queue_panel_keyboard()
            )
            return
        await cb.message.edit_text(
            queued_post_text(post), reply_markup=queued_post_keyboard(post_id)
        )

    @router.callback_query(F.data.startswith("queue:edit:"))
    async def cb_queue_edit(cb: CallbackQuery, state: FSMContext) -> None:
        post_id = int(cb.data.rsplit(":", 1)[1])
        post = store.get_post(post_id)
        if post is None or post["status"] != "queued":
            await cb.answer("Пост уже обработан", show_alert=True)
            return
        await state.set_state(QueuedPostEditing.editing)
        await state.update_data(post_id=post_id)
        await cb.answer()
        if cb.message:
            await cb.message.answer(
                f"✏️ Отправьте полный новый текст поста #{post_id}.\n"
                f"Лимит: {rt.max_chars} байт. /cancel — оставить без изменений."
            )

    @router.message(QueuedPostEditing.editing)
    async def on_queue_edit(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.lower() in {"/cancel", "отмена"}:
            await state.clear()
            await message.answer("Изменение отменено.")
            return
        data = await state.get_data()
        post_id = int(data.get("post_id") or 0)
        try:
            updated = service.update_queued_post(post_id, raw)
        except ValueError as exc:
            await message.answer(f"❌ {esc(exc)}. Отправьте текст ещё раз или /cancel.")
            return
        await state.clear()
        if not updated:
            await message.answer("❌ Пост уже обработан или удалён.")
            return
        post = store.get_post(post_id)
        await message.answer("✅ Текст обновлён.")
        if post is not None:
            await message.answer(
                queued_post_text(post), reply_markup=queued_post_keyboard(post_id)
            )

    @router.callback_query(F.data.startswith("queue:rephrase:"))
    async def cb_queue_rephrase(cb: CallbackQuery, state: FSMContext) -> None:
        post_id = int(cb.data.rsplit(":", 1)[1])
        post = store.get_post(post_id)
        if post is None or post["status"] != "queued":
            await cb.answer("Пост уже обработан", show_alert=True)
            return
        await state.set_state(QueuedPostEditing.rephrasing)
        await state.update_data(post_id=post_id)
        await cb.answer()
        if cb.message:
            await cb.message.answer(
                f"✨ Автор создаст отдельный вариант поста #{post_id}; "
                "исходник останется в очереди.\n\n"
                "Отправьте пожелание, например «короче и провокационнее», "
                "или отправьте <code>-</code> для обычного перефразирования.\n"
                "/cancel — отмена."
            )

    @router.message(QueuedPostEditing.rephrasing)
    async def on_queue_rephrase(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.lower() in {"/cancel", "отмена"}:
            await state.clear()
            await message.answer("Перефразирование отменено.")
            return
        data = await state.get_data()
        post_id = int(data.get("post_id") or 0)
        instruction = "" if raw == "-" else raw
        note = await message.answer("✨ Автор перефразирует пост…")
        try:
            new_id = await service.rephrase_to_queue(post_id, instruction)
        except Exception as exc:
            await state.clear()
            await note.edit_text(f"❌ Не получилось: {esc(exc)}")
            return
        await state.clear()
        new_post = store.get_post(new_id)
        if new_post is None:
            await note.edit_text("❌ Новый вариант не найден.")
            return
        await note.edit_text(
            f"✅ Создан отдельный вариант #{new_id}. Оба поста остаются в очереди."
        )
        await message.answer(
            queued_post_text(new_post), reply_markup=queued_post_keyboard(new_id)
        )

    @router.callback_query(F.data.startswith("queue:publish:"))
    async def cb_queue_publish(cb: CallbackQuery) -> None:
        post_id = int(cb.data.rsplit(":", 1)[1])
        await cb.answer("Публикую…")
        ok = await service.publish_queued_now(post_id)
        if cb.message:
            text = (
                f"✅ Пост #{post_id} опубликован."
                if ok
                else f"❌ Пост #{post_id} не опубликован. Смотрите /logs."
            )
            await cb.message.edit_text(text, reply_markup=queue_panel_keyboard())

    @router.callback_query(F.data.regexp(r"^queue:delete:\d+$"))
    async def cb_queue_delete(cb: CallbackQuery) -> None:
        post_id = int(cb.data.rsplit(":", 1)[1])
        post = store.get_post(post_id)
        if post is None or post["status"] != "queued":
            await cb.answer("Пост уже обработан", show_alert=True)
            return
        await cb.answer()
        if cb.message:
            await cb.message.edit_text(
                f"Удалить пост <b>#{post_id}</b> из очереди?",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🗑 Да, удалить",
                                callback_data=f"queue:delete-confirm:{post_id}",
                            ),
                            InlineKeyboardButton(
                                text="Отмена", callback_data=f"queue:view:{post_id}"
                            ),
                        ]
                    ]
                ),
            )

    @router.callback_query(F.data.startswith("queue:delete-confirm:"))
    async def cb_queue_delete_confirm(cb: CallbackQuery) -> None:
        post_id = int(cb.data.rsplit(":", 1)[1])
        deleted = service.delete_queued_post(post_id)
        await cb.answer("Удалено" if deleted else "Пост уже обработан")
        if cb.message:
            await cb.message.edit_text(
                queue_panel_text(), reply_markup=queue_panel_keyboard()
            )

    # ---------- настройки Автора ----------
    @router.message(Command("author", "writer"))
    async def cmd_author(message: Message) -> None:
        await message.answer(author_panel_text(), reply_markup=author_panel_keyboard())

    @router.callback_query(F.data.startswith("author:set:"))
    async def cb_author_setting(cb: CallbackQuery, state: FSMContext) -> None:
        key = cb.data.rsplit(":", 1)[1]
        fields = dict(AUTHOR_FIELDS)
        if key not in fields:
            await cb.answer("Настройка не найдена", show_alert=True)
            return
        await state.set_state(AgentSettingEditing.value)
        await state.update_data(setting_key=key)
        await cb.answer()
        if cb.message:
            current = str(rt.get(key)).strip() or "—"
            await cb.message.answer(
                f"✏️ <b>{esc(fields[key])}</b>\n"
                f"Сейчас: <code>{escaped_preview(current, 2500)}</code>\n\n"
                "Отправьте новое значение. «-» очистит текстовое поле, "
                "<code>/reset</code> вернёт значение по умолчанию, /cancel — отмена."
            )

    @router.message(AgentSettingEditing.value)
    async def on_author_setting(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.lower() in {"/cancel", "отмена"}:
            await state.clear()
            await message.answer("Изменение отменено.")
            return
        data = await state.get_data()
        key = str(data.get("setting_key") or "")
        fields = dict(AUTHOR_FIELDS)
        if key not in fields:
            await state.clear()
            await message.answer("❌ Настройка больше недоступна.")
            return
        try:
            if raw.lower() == "/reset":
                rt.reset(key)
            else:
                rt.set(key, validate_author_setting(key, raw))
        except (TypeError, ValueError) as exc:
            await message.answer(f"❌ {esc(exc)}. Отправьте значение ещё раз или /cancel.")
            return
        await state.clear()
        store.log_event(f"Изменена настройка {key} через Telegram")
        await message.answer(f"✅ <b>{esc(fields[key])}</b>: {esc(rt.get(key))}")
        await message.answer(author_panel_text(), reply_markup=author_panel_keyboard())

    # ---------- /analytics ----------
    @router.message(Command("analytics"))
    async def cmd_analytics(message: Message) -> None:
        note = await message.answer("📊 Собираю метрики…")
        try:
            await service.sync_metrics()
        except Exception as exc:
            log.warning("sync_metrics: %s", exc)
        posts = [
            p for p in store.recent_posts(40)
            if p["status"] in {"published", "published_dry"} and p["metrics_json"]
        ]
        posts.sort(key=lambda p: engagement_score(metrics_dict(p)), reverse=True)
        lines = ["<b>📊 Аналитика</b>"]
        if not posts:
            lines.append("Пока нет постов с метриками. Метрики подтягиваются каждые 6 часов.")
        for p in posts[:10]:
            m = metrics_dict(p)
            link = p["threads_permalink"]
            title = esc(p["topic"] or (p["text"] or "")[:50])
            head = f'<a href="{esc(link)}">{title}</a>' if link else title
            lines.append(
                f"• {head}\n  👁 {m.get('views', 0)} · ❤️ {m.get('likes', 0)} · "
                f"💬 {m.get('replies', 0)} · 🔁 {m.get('reposts', 0)} · "
                f"<i>score {engagement_score(m):.0f}</i>"
            )
        try:
            acc = await service.account_insights(days=7)
            if acc:
                lines.append(
                    f"\n<b>Аккаунт за 7 дней:</b> 👁 {acc.get('views', 0)} · "
                    f"❤️ {acc.get('likes', 0)} · 💬 {acc.get('replies', 0)} · "
                    f"👥 {acc.get('followers_count', 0)}"
                )
        except Exception as exc:
            log.debug("account insights: %s", exc)
        await note.edit_text("\n".join(lines), disable_web_page_preview=True)

    # ---------- /frequency /jitter /schedule ----------
    def save_posting_plan(
        times: list[tuple[int, int]], *, window_start: str | None = None,
        window_end: str | None = None,
    ) -> int | None:
        """Сохраняет слоты и синхронизирует дневной лимит/защитный интервал."""
        rt.set("posting_times", ",".join(f"{h:02d}:{m:02d}" for h, m in times))
        rt.set("max_posts_per_day", len(times))
        if window_start is not None:
            rt.set("posting_window_start", window_start)
        if window_end is not None:
            rt.set("posting_window_end", window_end)

        adjusted_interval: int | None = None
        if len(times) > 1:
            values = sorted(h * 60 + m for h, m in times)
            gaps = [b - a for a, b in zip(values, values[1:])]
            gaps.append(24 * 60 - values[-1] + values[0])
            # Два соседних слота могут сблизиться на 2*jitter.
            safe_interval = max(1, min(gaps) - 2 * rt.posting_jitter_minutes - 1)
            if rt.get_int("min_minutes_between_posts") > safe_interval:
                rt.set("min_minutes_between_posts", safe_interval)
                adjusted_interval = safe_interval
        scheduler.reload()
        return adjusted_interval

    @router.message(Command("frequency"))
    async def cmd_frequency(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        current_times = rt.posting_times
        if not raw:
            times = ", ".join(f"{h:02d}:{m:02d}" for h, m in current_times)
            await message.answer(
                f"<b>Частота:</b> {len(current_times)} пост./день\n"
                f"<b>Окно:</b> {esc(rt.get_str('posting_window_start'))}–"
                f"{esc(rt.get_str('posting_window_end'))}\n"
                f"<b>Базовые слоты:</b> {times}\n"
                f"<b>Случайный разброс:</b> ±{rt.posting_jitter_minutes} мин\n\n"
                "Изменить: <code>/frequency 4</code>\n"
                "Другое окно: <code>/frequency 4 10:00 20:00</code>"
            )
            return

        parts = raw.replace("–", "-").replace("—", "-").replace("-", " ").split()
        if len(parts) not in {1, 3}:
            await message.answer(
                "Использование: <code>/frequency N</code> или "
                "<code>/frequency N 09:00 19:00</code>"
            )
            return
        try:
            count = int(parts[0])
            start = parts[1] if len(parts) == 3 else rt.get_str("posting_window_start")
            end = parts[2] if len(parts) == 3 else rt.get_str("posting_window_end")
            times = distribute_posting_times(count, start, end)
        except (TypeError, ValueError) as exc:
            await message.answer(f"❌ {esc(exc)}")
            return

        adjusted = save_posting_plan(times, window_start=start, window_end=end)
        rendered = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
        warning = (
            "\n⚠️ Сейчас включено подтверждение: в эти моменты бот пришлёт "
            "черновик. Для автоматической публикации используйте <code>/auto on</code>."
            if not rt.get_bool("auto_approve") else ""
        )
        interval_note = (
            f"\nЗащитный интервал автоматически изменён на {adjusted} мин."
            if adjusted is not None else ""
        )
        await message.answer(
            f"✅ Частота: <b>{count} пост./день</b>\n"
            f"Окно: {esc(start)}–{esc(end)}\n"
            f"Базовые слоты: {rendered}\n"
            f"Каждый слот сработает случайно в пределах "
            f"±{rt.posting_jitter_minutes} минут.{interval_note}{warning}"
        )

    @router.message(Command("jitter"))
    async def cmd_jitter(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                f"Случайный разброс: <b>±{rt.posting_jitter_minutes} минут</b>.\n"
                "Изменить: <code>/jitter 10</code> (допустимо 0–60)."
            )
            return
        try:
            minutes = int(raw)
            if not 0 <= minutes <= 60:
                raise ValueError("Разброс должен быть от 0 до 60 минут")
        except ValueError as exc:
            await message.answer(f"❌ {esc(exc)}")
            return
        rt.set("posting_jitter_minutes", minutes)
        scheduler.reload()
        if minutes:
            await message.answer(
                f"✅ Каждый базовый слот теперь срабатывает случайно в пределах "
                f"±{minutes} минут. Новое время выбирается для каждого запуска."
            )
        else:
            await message.answer("✅ Случайный разброс выключен: слоты снова точные.")

    # ---------- /schedule ----------
    @router.message(Command("schedule"))
    async def cmd_schedule(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if not arg:
            times = ", ".join(f"{h:02d}:{m:02d}" for h, m in rt.posting_times)
            await message.answer(
                f"<b>Базовое расписание:</b> {times} ({esc(settings.timezone)})\n"
                f"Фактический запуск: случайно ±{rt.posting_jitter_minutes} мин.\n"
                f"Частота: {len(rt.posting_times)} пост./день.\n"
                "Изменить вручную: <code>/schedule 09:00,13:00,19:30</code>\n"
                "Изменить только частоту: <code>/frequency 3</code>"
            )
            return
        from .config import parse_posting_times

        try:
            parsed = parse_posting_times(arg)
            if len(parsed) > 10:
                raise ValueError("Допустимо не более 10 слотов в день")
        except ValueError as exc:
            await message.answer(f"❌ {esc(exc)}")
            return
        start = f"{parsed[0][0]:02d}:{parsed[0][1]:02d}" if len(parsed) > 1 else None
        end = f"{parsed[-1][0]:02d}:{parsed[-1][1]:02d}" if len(parsed) > 1 else None
        adjusted = save_posting_plan(parsed, window_start=start, window_end=end)
        interval_note = (
            f" Защитный интервал: {adjusted} мин." if adjusted is not None else ""
        )
        await message.answer(
            "✅ Новое базовое расписание: "
            + ", ".join(f"{h:02d}:{m:02d}" for h, m in parsed)
            + f". Каждый слот случайно ±{rt.posting_jitter_minutes} мин."
            + interval_note
        )

    # ---------- /auto /pause /resume /dryrun ----------
    @router.message(Command("auto"))
    async def cmd_auto(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        if arg not in {"on", "off", "вкл", "выкл", "1", "0"}:
            await message.answer(
                "Сейчас: " + ("авто" if rt.get_bool("auto_approve") else "с подтверждением")
                + "\nИспользование: <code>/auto on</code> или <code>/auto off</code>"
            )
            return
        value = arg in {"on", "вкл", "1"}
        rt.set("auto_approve", value)
        await message.answer(
            "✅ Публикую сразу без подтверждения" if value
            else "✅ Каждый пост будет приходить на подтверждение"
        )

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        rt.set("paused", True)
        await message.answer("⏸ Автопостинг на паузе. /resume чтобы возобновить.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        rt.set("paused", False)
        await message.answer("▶️ Автопостинг возобновлён. " + scheduler.describe())

    @router.message(Command("dryrun"))
    async def cmd_dryrun(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        if arg not in {"on", "off", "вкл", "выкл", "1", "0"}:
            await message.answer(
                "Сухой режим: " + ("вкл" if rt.get_bool("dry_run") else "выкл")
                + "\nИспользование: <code>/dryrun on</code>"
            )
            return
        value = arg in {"on", "вкл", "1"}
        rt.set("dry_run", value)
        await message.answer(
            "🧪 Сухой режим включён: посты генерируются, но в Threads не уходят."
            if value else "✅ Сухой режим выключен: посты публикуются по-настоящему."
        )

    # ---------- /settings /set ----------
    @router.message(Command("settings"))
    async def cmd_settings(message: Message) -> None:
        keys = [
            "auto_approve", "dry_run", "posting_times", "posting_jitter_minutes",
            "posting_window_start", "posting_window_end", "max_posts_per_day",
            "min_minutes_between_posts", "stock_images_enabled", "brand_niche", "brand_voice",
            "brand_audience", "brand_language", "brand_cta", "forbidden_topics",
            "max_post_chars", "writer_model", "llm_temperature",
            "writer_instructions", "threads_username",
        ]
        lines = ["<b>⚙️ Настройки</b>"]
        overridden = set(store.all_settings())
        for key in keys:
            mark = " <i>(из бота)</i>" if key in overridden else ""
            lines.append(f"<code>{key}</code> = {esc(rt.get(key))}{mark}")
        lines.append(
            "\nИзменение: <code>/set ключ значение</code>\n"
            "Сброс к .env: <code>/reset ключ</code>\n"
            "Модели: <code>/model</code>"
        )
        await message.answer("\n".join(lines))

    @router.message(Command("set"))
    async def cmd_set(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw or " " not in raw:
            await message.answer("Использование: <code>/set brand_niche технологии и ИИ</code>")
            return
        key, value = raw.split(" ", 1)
        key = key.strip()
        value = value.strip()
        if key not in SPECS:
            available = ", ".join(sorted(SPECS))
            await message.answer(f"❌ Неизвестный ключ.\nДоступны: {esc(available)}")
            return
        try:
            old = rt.get(key)
            author_keys = {field for field, _ in AUTHOR_FIELDS}
            parsed_value = validate_author_setting(key, value) if key in author_keys else value
            rt.set(key, parsed_value)
            new = rt.get(key)
        except Exception as exc:
            await message.answer(f"❌ {esc(exc)}")
            return
        if key == "max_post_chars" and not 1 <= int(new) <= 500:
            rt.set(key, old)
            await message.answer("❌ max_post_chars должен быть 1..500")
            return
        if key == "posting_jitter_minutes" and not 0 <= int(new) <= 60:
            rt.set(key, old)
            await message.answer("❌ posting_jitter_minutes должен быть 0..60")
            return
        if key == "posting_times":
            from .config import parse_posting_times
            try:
                parsed = parse_posting_times(value)
                if len(parsed) > 10:
                    raise ValueError("Допустимо не более 10 слотов")
            except ValueError as exc:
                rt.set(key, old)
                await message.answer(f"❌ {esc(exc)}")
                return
        if key in {"posting_times", "posting_jitter_minutes"}:
            scheduler.reload()
        await message.answer(f"✅ <code>{esc(key)}</code>: {esc(old)} → {esc(new)}")

    @router.message(Command("reset"))
    async def cmd_reset(message: Message, command: CommandObject) -> None:
        key = (command.args or "").strip()
        if not key:
            await message.answer("Использование: <code>/reset writer_model</code>")
            return
        if key not in SPECS:
            await message.answer(f"❌ Неизвестный ключ: {esc(key)}")
            return
        rt.reset(key)
        if key in {"posting_times", "posting_jitter_minutes"}:
            scheduler.reload()
        await message.answer(
            f"↩️ <code>{esc(key)}</code> сброшен, действует значение: {esc(rt.get(key))}"
        )

    # ---------- /model ----------
    MODEL_KEYS = {"writer": "writer_model", "author": "writer_model"}

    def models_text() -> str:
        return (
            "<b>🧠 Модель Автора</b>\n"
            f"Сейчас: <code>{esc(rt.get_str('writer_model'))}</code>\n\n"
            "Переключить:\n"
            "<code>/model writer anthropic/claude-sonnet-5.5</code>\n\n"
            "Полный каталог с ценами: polza.ai/models\n"
            "Сброс к .env: <code>/reset writer_model</code>"
        )

    @router.message(Command("model"))
    async def cmd_model(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(models_text())
            return
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() not in MODEL_KEYS:
            await message.answer(
                "Использование: <code>/model writer ID_модели</code>\n\n" + models_text()
            )
            return
        role, model_id = parts[0].lower(), parts[1].strip()
        key = MODEL_KEYS[role]

        # Мягкая проверка по каталогу Polza: предупреждаем, но не блокируем
        warning = ""
        if not model_id.startswith(("openai/", "anthropic/", "google/", "deepseek/")) and "/" not in model_id:
            warning = "\n\n⚠️ ID модели обычно содержит провайдера: <code>anthropic/claude-haiku-5.5</code>"

        old = rt.get(key)
        rt.set(key, model_id)
        await message.answer(
            f"✅ {esc(role)}: <code>{esc(old)}</code> → <code>{esc(model_id)}</code>\n"
            "Применится к следующему посту." + warning
        )

    # ---------- /token ----------
    @router.message(Command("token"))
    async def cmd_token(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if arg:
            parts = arg.split()
            try:
                rt.set("threads_access_token", parts[0])
                if len(parts) > 1:
                    rt.set("threads_user_id", parts[1])
                if len(parts) > 2:
                    rt.set("threads_username", parts[2].lstrip("@"))
            except Exception as exc:
                await message.answer(f"❌ {esc(exc)}")
                return
            await message.answer("✅ Токен сохранён. Проверь /status.")
            return

        if not settings.threads_app_id or not settings.threads_app_secret:
            await message.answer(
                "Сначала заполни <code>THREADS_APP_ID</code> и "
                "<code>THREADS_APP_SECRET</code> в .env (смотри README, раздел "
                "«Как получить ключ API»)."
            )
            return
        if rt.token and rt.user_id:
            await message.answer(
                f"Аккаунт подключён: @{esc(rt.username)}\n"
                f"Токен: <code>{esc(rt.token[:12])}…</code>\n\n"
                "Переподключить: ссылка ниже."
            )
        link = f"{rt.public_base_url}/auth/threads"
        if settings.web_auth_key:
            link += f"?key={settings.web_auth_key}"
        await message.answer(
            f"🔗 <b>Подключение Threads</b>\n{esc(link)}\n\n"
            "Открой ссылку, разреши доступ — токен сохранится автоматически."
        )

    # ---------- /logs ----------
    @router.message(Command("logs"))
    async def cmd_logs(message: Message) -> None:
        rows = store.recent_events(15)
        if not rows:
            await message.answer("Событий пока нет.")
            return
        lines = ["<b>🗒 Последние события</b>"]
        for r in rows:
            ts = (r["created_at"] or "")[:16].replace("T", " ")
            icon = {"error": "❌", "warn": "⚠️"}.get(r["level"], "•")
            lines.append(f"{icon} <code>{ts}</code> {esc(r['message'])[:300]}")
        await message.answer("\n".join(lines))

    # ---------- согласование: кнопки ----------
    @router.callback_query(F.data.startswith("pub:"))
    async def cb_publish(cb: CallbackQuery) -> None:
        post_id = int(cb.data.split(":", 1)[1])
        await cb.answer("Публикую…")
        ok = await service.approve(post_id)
        if cb.message:
            prefix = "✅ Опубликовано" if ok else "❌ Не удалось опубликовать"
            await replace_result_message(
                cb.message,
                f"{prefix} (пост #{post_id}). Подробности — /status или /logs.",
            )

    @router.callback_query(F.data.startswith("regen:"))
    async def cb_regenerate(cb: CallbackQuery) -> None:
        post_id = int(cb.data.split(":", 1)[1])
        await cb.answer("Переписываю…")
        try:
            await service.regenerate(post_id)
            if cb.message:
                await replace_result_message(
                    cb.message, f"🔄 Пост #{post_id} перегенерирован — новый вариант выше."
                )
        except Exception as exc:
            if cb.message:
                await cb.message.answer(f"❌ {esc(exc)}")

    @router.callback_query(F.data.startswith("skip:"))
    async def cb_skip(cb: CallbackQuery) -> None:
        post_id = int(cb.data.split(":", 1)[1])
        await service.reject(post_id)
        await cb.answer("Пропущено")
        if cb.message:
            await replace_result_message(cb.message, f"🚫 Пост #{post_id} пропущен.")

    @router.callback_query(F.data.startswith("edit:"))
    async def cb_edit(cb: CallbackQuery, state: FSMContext) -> None:
        post_id = int(cb.data.split(":", 1)[1])
        await state.set_state(Editing.instruction)
        await state.update_data(post_id=post_id)
        await cb.answer()
        if cb.message:
            await cb.message.answer(
                f"✏️ Что изменить в посте #{post_id}? Напиши одним сообщением, "
                "например: «короче», «добавь вопрос в конце», «убери эмодзи».\n"
                "/cancel — отмена."
            )

    @router.message(Editing.instruction)
    async def on_edit_instruction(message: Message, state: FSMContext) -> None:
        if (message.text or "").strip().lower() in {"/cancel", "отмена"}:
            await state.clear()
            await message.answer("Отменено.")
            return
        data = await state.get_data()
        post_id = int(data.get("post_id") or 0)
        await state.clear()
        note = await message.answer("✏️ Переписываю…")
        try:
            result = await service.revise(post_id, (message.text or "").strip())
            await note.edit_text(
                f"📝 <b>Пост #{post_id} обновлён</b>\n\n"
                f"<blockquote>{esc(result.text)}</blockquote>\n"
                f"<i>{result.bytes_len}/500 байт</i>",
                reply_markup=approval_keyboard(post_id),
            )
        except Exception as exc:
            await note.edit_text(f"❌ {esc(exc)}")

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.")

    # неизвестные сообщения
    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer("Не понял. Список команд — /help")

    dp.include_router(router)
    return dp, notifier, bot


async def run_bot(dp: Dispatcher, bot: Bot) -> None:
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        polling_timeout=settings.telegram_polling_timeout,
    )
