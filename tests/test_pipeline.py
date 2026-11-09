"""Сквозные тесты: Автор, пользовательская очередь, согласование и публикация."""
import json

import pytest

from app.agents.writer import Writer
from app.db import Store
from app.runtime import Runtime
from app.service import ContentService


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.post_counter = 0

    async def complete(self, *, system: str, user: str, model: str, **kwargs) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        self.post_counter += 1
        return json.dumps(
            {
                "text": f"Вариант {self.post_counter}: конкретная мысль про продуктивность.",
                "topic_tag": "продуктивность",
            },
            ensure_ascii=False,
        )


class FakeNotifier:
    def __init__(self) -> None:
        self.approvals: list[tuple[int, str]] = []
        self.messages: list[str] = []
        self.alerts: list[str] = []

    async def post_for_approval(self, post_id, text, topic, meta):
        self.approvals.append((post_id, text))

    async def info(self, message):
        self.messages.append(message)

    async def alert(self, message):
        self.alerts.append(message)


def build(tmp_path):
    store = Store(tmp_path / "p.db")
    store.init()
    rt = Runtime(store)
    llm = FakeLLM()
    service = ContentService(store, rt, writer=Writer(store, rt, llm=llm))
    notifier = FakeNotifier()
    service.notifier = notifier
    return store, rt, llm, service, notifier


async def test_author_generates_post_for_approval(tmp_path):
    store, rt, llm, service, notifier = build(tmp_path)
    rt.set("auto_approve", False)

    post_id = await service.create_post(topic="Продуктивность без выгорания")

    post = store.get_post(post_id)
    assert post["status"] == "pending_approval"
    assert post["source"] == "generated"
    assert "Продуктивность без выгорания" in llm.calls[0]["user"]
    assert notifier.approvals[0][0] == post_id


async def test_generated_post_can_auto_publish_in_dry_run(tmp_path):
    store, rt, _llm, service, notifier = build(tmp_path)
    rt.set("dry_run", True)
    rt.set("auto_approve", True)

    post_id = await service.create_post(topic="Автоматизация")

    assert store.get_post(post_id)["status"] == "published_dry"
    assert not notifier.approvals


def test_user_can_queue_multiple_posts_in_order(tmp_path):
    store, rt, _llm, service, _notifier = build(tmp_path)

    ids = service.queue_user_posts(["Первый готовый пост", "Второй готовый пост"])

    assert store.count_queued_posts() == 2
    assert [int(row["id"]) for row in store.queued_posts()] == ids
    assert store.next_queued_post()["text"] == "Первый готовый пост"
    assert all(store.get_post(post_id)["source"] == "user_queue" for post_id in ids)


def test_queue_rejects_oversized_post_without_partial_insert(tmp_path):
    store, rt, _llm, service, _notifier = build(tmp_path)
    rt.set("max_post_chars", 20)

    with pytest.raises(ValueError, match="Пост 2"):
        service.queue_user_posts(["Короткий", "Очень длинный пользовательский пост"])

    assert store.count_queued_posts() == 0


async def test_schedule_takes_user_post_instead_of_calling_llm(tmp_path):
    store, rt, llm, service, notifier = build(tmp_path)
    rt.set("auto_approve", False)
    post_id = service.queue_user_posts(["Заранее подготовленный пост"])[0]

    selected = await service.prepare_next_queued_post()

    assert selected == post_id
    assert store.get_post(post_id)["status"] == "pending_approval"
    assert store.count_queued_posts() == 0
    assert llm.calls == []
    assert notifier.approvals[0][0] == post_id


async def test_rephrase_adds_separate_queued_variant(tmp_path):
    store, _rt, llm, service, _notifier = build(tmp_path)
    original_id = service.queue_user_posts(["Исходный пользовательский текст"])[0]

    variant_id = await service.rephrase_to_queue(original_id, "сделай динамичнее")

    assert variant_id != original_id
    assert store.get_post(original_id)["status"] == "queued"
    variant = store.get_post(variant_id)
    assert variant["status"] == "queued"
    assert variant["source"] == "rephrased"
    assert "сделай динамичнее" in llm.calls[0]["user"]
    assert store.count_queued_posts() == 2


async def test_regenerate_rephrases_pending_post(tmp_path):
    store, rt, _llm, service, _notifier = build(tmp_path)
    rt.set("auto_approve", False)
    first = await service.create_post(topic="Тема")

    second = await service.regenerate(first, instruction="короче")

    assert second != first
    assert store.get_post(first)["status"] == "rejected"
    assert store.get_post(second)["status"] == "pending_approval"
    assert store.get_post(second)["source"] == "rephrased"


async def test_publish_without_token_fails_gracefully(tmp_path):
    store, rt, _llm, service, notifier = build(tmp_path)
    rt.set("auto_approve", True)
    rt.set("dry_run", False)

    post_id = await service.create_post(topic="Тема")

    post = store.get_post(post_id)
    assert post["status"] == "failed"
    assert "Threads не подключён" in post["error"]
    assert notifier.alerts


async def test_writer_failure_does_not_create_post(tmp_path):
    store, rt, _llm, _service, _notifier = build(tmp_path)

    class BoomWriter:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("LLM недоступен")

    service = ContentService(store, rt, writer=BoomWriter())

    with pytest.raises(RuntimeError, match="LLM недоступен"):
        await service.create_post(topic="Тема для сбоя")

    assert store.recent_posts() == []


async def test_new_generated_post_gets_stock_image(tmp_path, monkeypatch):
    import struct

    from app.config import settings
    from app.stock import inspect_image, write_stock_file

    monkeypatch.setattr(settings, "stock_media_dir", str(tmp_path / "stock"))
    store, rt, _llm, service, notifier = build(tmp_path)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", 640, 640)
        + b"\x08\x06\x00\x00\x00payload"
    )
    info = inspect_image(data)
    write_stock_file("auto.png", data)
    image_id, _ = store.add_stock_image(
        token="auto-token",
        storage_name="auto.png",
        mime_type=info.mime_type,
        size_bytes=info.size_bytes,
        width=info.width,
        height=info.height,
        sha256=info.sha256,
    )

    post_id = await service.create_post(topic="Тема")

    assigned = store.get_post_image(post_id)
    assert assigned is not None
    assert int(assigned["id"]) == image_id
    assert notifier.approvals[0][0] == post_id


async def test_project_knowledge_reaches_author_and_rephrase(tmp_path):
    store, _rt, llm, service, _notifier = build(tmp_path)
    store.add_project_knowledge(
        "Источник истины",
        "Продукт называется Orbit и предназначен только для дизайнеров.",
    )

    await service.create_post(topic="Orbit")
    queued_id = service.queue_user_posts(["Расскажем о продукте"])[0]
    await service.rephrase_to_queue(queued_id)

    assert len(llm.calls) == 2
    assert all("Продукт называется Orbit" in call["user"] for call in llm.calls)


async def test_custom_author_instructions_reach_generation_and_rephrase(tmp_path):
    store, rt, llm, service, _notifier = build(tmp_path)
    rt.set("writer_instructions", "Добавляй один практический пример")

    await service.create_post(topic="Тема")
    queued_id = service.queue_user_posts(["Исходный текст"])[0]
    await service.rephrase_to_queue(queued_id)

    assert all("Добавляй один практический пример" in call["user"] for call in llm.calls)


async def test_multipost_sends_same_russian_prompt_to_every_model(tmp_path):
    store, rt, llm, service, _notifier = build(tmp_path)
    rt.set("brand_language", "en")  # multipost обязан писать по-русски

    variants = await service.multipost("Автоматизация", ["model-a", "model-b"])

    assert [variant.model for variant in variants] == ["model-a", "model-b"]
    assert len(llm.calls) == 2
    assert [call["model"] for call in llm.calls] == ["model-a", "model-b"]
    # один и тот же промпт для всех моделей
    assert llm.calls[0]["user"] == llm.calls[1]["user"]
    assert "ЯЗЫК ПОСТА: ru" in llm.calls[0]["user"]
    assert "ПИШИ ПОСТ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ." in llm.calls[0]["user"]

    for variant in variants:
        assert variant.post_id is not None
        post = store.get_post(int(variant.post_id))
        assert post["status"] == "pending_approval"
        assert post["source"] == "multipost"


async def test_multipost_isolates_model_failure(tmp_path):
    store, _rt, _llm, service, _notifier = build(tmp_path)

    class PartialLLM:
        async def complete(self, *, model: str, **kwargs) -> str:
            if model == "bad-model":
                raise RuntimeError("модель недоступна")
            return json.dumps(
                {"text": "Рабочий вариант на русском.", "topic_tag": ""},
                ensure_ascii=False,
            )

    service.writer.llm = PartialLLM()

    variants = await service.multipost("Тема", ["bad-model", "good-model"])

    assert variants[0].post_id is None
    assert "модель недоступна" in variants[0].error
    assert variants[1].post_id is not None
    assert store.get_post(int(variants[1].post_id))["status"] == "pending_approval"
    assert len(store.recent_posts()) == 1


async def test_multipost_variant_can_be_queued_or_discarded(tmp_path):
    store, _rt, _llm, service, _notifier = build(tmp_path)
    first, second = await service.multipost("Тема", ["model-a", "model-b"])
    first_id = int(first.post_id)
    second_id = int(second.post_id)

    assert service.queue_multipost_variant(first_id)
    assert store.get_post(first_id)["status"] == "queued"
    assert store.count_queued_posts() == 1

    assert service.discard_multipost_variant(second_id)
    assert store.get_post(second_id) is None

    # вариант, уже отправленный в очередь, тоже можно удалить
    assert service.discard_multipost_variant(first_id)
    assert store.get_post(first_id) is None
    assert store.count_queued_posts() == 0
