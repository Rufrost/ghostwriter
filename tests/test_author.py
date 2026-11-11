"""Редактирование полного system-промпта через панель Автора."""
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.bot import AgentSettingEditing, build_dispatcher
from app.config import settings
from app.db import Store
from app.prompts import DEFAULT_WRITER_SYSTEM_PROMPT, WRITER_JSON_REQUIREMENT, WRITER_LENGTH_REQUIREMENT
from app.runtime import Runtime
from app.service import ContentService


class MessageStub:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))


def get_handler(dp, observer, name):
    handlers = dp.sub_routers[0].observers[observer].handlers
    return next(handler.callback for handler in handlers if handler.callback.__name__ == name)


@pytest.fixture
async def author_panel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456789:test-token")
    store = Store(tmp_path / "author.db")
    store.init()
    rt = Runtime(store)
    dp, _notifier, bot = build_dispatcher(ContentService(store, rt), rt, None, {1})
    state = FSMContext(dp.storage, StorageKey(bot_id=bot.id, chat_id=1, user_id=1))
    try:
        yield dp, rt, state
    finally:
        await dp.storage.close()
        await bot.session.close()


async def open_prompt(dp, state):
    message = MessageStub()

    async def answer(*args, **kwargs):
        pass

    cb = SimpleNamespace(data="author:set:writer_system_prompt", message=message, answer=answer)
    await get_handler(dp, "callback_query", "cb_author_setting")(cb, state)
    return message


async def test_author_panel_exposes_system_prompt_instead_of_extra_instructions(author_panel):
    dp, rt, state = author_panel
    message = MessageStub()
    await get_handler(dp, "message", "cmd_author")(message)

    text, options = message.sent[0]
    assert "Системный промпт" in text
    buttons = [button for row in options["reply_markup"].inline_keyboard for button in row]
    assert any(button.callback_data == "author:set:writer_system_prompt" for button in buttons)
    assert all("writer_instructions" not in (button.callback_data or "") for button in buttons)

    opened = await open_prompt(dp, state)
    full_prompt = "".join(text for text, kwargs in opened.sent if kwargs.get("parse_mode", "HTML") is None)
    assert full_prompt == DEFAULT_WRITER_SYSTEM_PROMPT
    assert WRITER_JSON_REQUIREMENT not in full_prompt
    assert WRITER_LENGTH_REQUIREMENT not in full_prompt
    assert "Пост должен быть" not in full_prompt
    assert any("Блоки длины и JSON добавляются автоматически" in text for text, _ in opened.sent)
    assert await state.get_state() == AgentSettingEditing.value.state

    prompt = "Пиши без рекламы.\nВерни JSON с text и topic_tag."
    await get_handler(dp, "message", "on_author_setting")(MessageStub(prompt), state)
    assert rt.get_str("writer_system_prompt") == prompt.strip()
    assert await state.get_state() is None

    await open_prompt(dp, state)
    await get_handler(dp, "message", "on_author_setting")(MessageStub("/reset"), state)
    assert rt.get_str("writer_system_prompt") == DEFAULT_WRITER_SYSTEM_PROMPT


async def test_system_prompt_is_shown_in_full_and_safely(author_panel):
    dp, rt, state = author_panel
    prompt = '<tag>&"😀\n' * 440
    assert len(prompt) <= 4000
    rt.set("writer_system_prompt", prompt)
    opened = await open_prompt(dp, state)
    parts = [text for text, kwargs in opened.sent if kwargs.get("parse_mode", "HTML") is None]
    assert "".join(parts) == prompt.strip()
    assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in parts)

    changed = MessageStub(prompt)
    await get_handler(dp, "message", "on_author_setting")(changed, state)
    assert rt.get_str("writer_system_prompt") == prompt.strip()
    assert all(len(text) < 4096 for text, _ in changed.sent)
    assert "&lt;tag&gt;" in changed.sent[0][0]


@pytest.mark.parametrize("invalid", ["", "-", "x" * 4001])
async def test_system_prompt_rejects_empty_or_oversized_replacement(author_panel, invalid):
    dp, rt, state = author_panel
    await open_prompt(dp, state)
    message = MessageStub(invalid)
    await get_handler(dp, "message", "on_author_setting")(message, state)
    assert rt.get_str("writer_system_prompt") == DEFAULT_WRITER_SYSTEM_PROMPT
    assert await state.get_state() == AgentSettingEditing.value.state
    assert message.sent[0][0].startswith("❌")


async def test_cancel_keeps_system_prompt(author_panel):
    dp, rt, state = author_panel
    rt.set("writer_system_prompt", "Текущий промпт")
    await open_prompt(dp, state)
    await get_handler(dp, "message", "on_author_setting")(MessageStub("/cancel"), state)
    assert rt.get_str("writer_system_prompt") == "Текущий промпт"
    assert await state.get_state() is None
