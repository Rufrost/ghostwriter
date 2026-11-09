"""Редактируемая память о проекте и её использование ИИ-агентами."""
from app.bot import BOT_COMMANDS
from app.db import Store


def test_project_knowledge_crud_and_context(tmp_path):
    store = Store(tmp_path / "memory.db")
    store.init()

    first = store.add_project_knowledge(
        "О продукте", "Проект называется Север. Он помогает планировать поездки."
    )
    second = store.add_project_knowledge(
        "О клиентах", "Основные клиенты — небольшие туристические агентства."
    )

    context = store.project_knowledge_context()
    assert "[О продукте]" in context
    assert "Проект называется Север" in context
    assert "[О клиентах]" in context
    assert store.project_knowledge_stats() == {"total": 2, "active": 2, "chars": 110}

    assert store.update_project_knowledge(
        first, title="Продукт", content="Проект Север строит маршруты."
    )
    assert "[Продукт]" in store.project_knowledge_context()
    assert "планировать поездки" not in store.project_knowledge_context()

    assert store.set_project_knowledge_active(second, False)
    assert "туристические агентства" not in store.project_knowledge_context()
    assert store.project_knowledge_stats()["active"] == 1

    assert store.delete_project_knowledge(first)
    assert store.get_project_knowledge(first) is None
    assert store.project_knowledge_context() == ""


def test_project_knowledge_context_has_size_limit(tmp_path):
    store = Store(tmp_path / "memory.db")
    store.init()
    store.add_project_knowledge("Большая запись", "x" * 3500)
    assert len(store.project_knowledge_context(max_chars=200)) <= 200


def test_native_telegram_menu_contains_memory_and_core_commands():
    names = {command.command for command in BOT_COMMANDS}
    assert {"menu", "help", "post", "queue", "memory", "stock", "status", "settings", "frequency", "jitter", "author"} <= names
    assert "strategist" not in names
    assert "strategy" not in names
    assert "topics" not in names
