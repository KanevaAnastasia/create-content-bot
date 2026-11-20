import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from telegram.ext import ApplicationHandlerStop

from app import article_store as store, article_generation as gen, article_workflow as flow, article_publisher as pub

START = datetime(2026, 11, 21, 6, tzinfo=timezone.utc)
PRIORITY_CHECK = flow.advertising_has_priority


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "articles")
    monkeypatch.setattr(flow, "_LOCK", asyncio.Lock())
    monkeypatch.setattr(flow, "advertising_has_priority", lambda: False)
    from app import state, advertising_queue
    monkeypatch.setattr(state, "SESSIONS", {})
    monkeypatch.setattr(advertising_queue, "_LOCKS", {})
    original_claim = store.claim_due
    monkeypatch.setattr(store, "claim_due", lambda now=None: original_claim(now or START))
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({"posts": [{"id": 19, "text": "Образец"}]}))
    ref = tmp_path / "ref.jpg"
    Image.new("RGB", (160, 90)).save(ref)
    store.configure(owner_id=123, publisher_id=456, channel_id=789, username="ubabushkidoma",
                    next_run_at=START, samples_path=samples, reference_paths=[ref])
    return tmp_path


def make_row(state="topics", **fields):
    row = store.claim_due(START)
    topics = [{"title": f"Тема {i}", "angle": f"Польза {i}"} for i in range(5)]
    return store.change(row["id"], row["revision"], allowed={"working"}, state=state, topics=topics,
                        topic=topics[0], **fields)


def bot():
    return NS(send_message=AsyncMock(return_value=NS(message_id=81)), send_photo=AsyncMock(return_value=NS(message_id=82)))


def update(row, action, user_id=123):
    return NS(effective_user=NS(id=user_id), effective_chat=NS(id=user_id, type="private"),
              callback_query=NS(data=f'ar:{row["id"]}:{row["revision"]}:{action}', answer=AsyncMock(),
                                message=NS(reply_text=AsyncMock())))


def test_schedule_three_days_no_burst_and_unfinished_blocks(setup):
    assert store.claim_due(START - timedelta(seconds=1)) is None
    row = store.claim_due(START + timedelta(days=7))
    assert datetime.fromisoformat(store.snapshot()["next_run_at"]) == START + timedelta(days=9)
    assert store.claim_due(START + timedelta(days=20)) is None
    store.change(row["id"], row["revision"], allowed={"working"}, state="cancelled")
    assert store.claim_due(START + timedelta(days=9))


def test_revision_protects_approved_copy(setup):
    row = make_row("text", text="Approved")
    store.change(row["id"], row["revision"], allowed={"text"}, state="ready")
    with pytest.raises(ValueError):
        store.change(row["id"], row["revision"], allowed={"text"}, text="Stale edit")
    assert store.issue(row["id"])["text"] == "Approved"


def test_restart_never_replays_generation_or_publication(setup):
    row = make_row("publishing", text="Final", publish_random_id=78)
    store.recover_interrupted()
    saved = store.issue(row["id"])
    assert saved["state"] == "publication_uncertain"
    assert saved["publish_random_id"] == 78
    store.change(saved["id"], saved["revision"], allowed={"publication_uncertain"}, state="working", images={"horizontal": "saved.jpg"})
    store.recover_interrupted()
    assert store.issue(row["id"])["state"] == "failed"
    assert store.issue(row["id"])["images"] == {"horizontal": "saved.jpg"}


def test_active_video_defers_article_without_claiming(setup, monkeypatch):
    from app import state
    state.SESSIONS[123] = {"state": "waiting_scene_approval", "recipe": "Keep me"}
    b = bot()
    asyncio.run(flow.tick(b))
    assert not store.snapshot()["issues"]
    assert state.SESSIONS[123]["recipe"] == "Keep me"
    b.send_message.assert_not_called()


def test_waiting_advertising_defers_article(setup, monkeypatch):
    monkeypatch.setattr(flow, "advertising_has_priority", lambda: True)
    asyncio.run(flow.tick(bot()))
    assert not store.snapshot()["issues"]


def test_article_blocks_advertising_delivery_and_preparation(setup, monkeypatch):
    from app import advertising_queue as ads
    make_row()
    monkeypatch.setattr(ads, "owner_chat_id", lambda: 123)
    monkeypatch.setattr(ads, "finish_product_if_complete", AsyncMock())
    def forbidden(*args, **kwargs):
        raise AssertionError("Advertisement must wait")
    monkeypatch.setattr(ads, "SessionLocal", forbidden)
    monkeypatch.setattr(ads, "claim_due_product", forbidden)
    asyncio.run(ads.tick(bot()))


def test_new_video_started_during_gpt_defers_delivery(setup, monkeypatch):
    from app import state
    def topics(*args):
        state.SESSIONS[123] = {"state": "waiting_recipe_approval"}
        return [{"title": str(i), "angle": "a"} for i in range(5)]
    monkeypatch.setattr(gen, "topics", topics)
    b = bot()
    asyncio.run(flow.tick(b))
    assert store.current()["state"] == "topics"
    b.send_message.assert_not_called()
    state.SESSIONS.clear()
    asyncio.run(flow.tick(b))
    assert b.send_message.call_count == 1


def test_topics_exactly_five_distinct(setup, monkeypatch):
    monkeypatch.setattr(gen, "openai_generate_sync", lambda *a: json.dumps({"topics": [{"title": "same", "angle": "x"}] * 5}))
    with pytest.raises(ValueError):
        gen.topics(store.snapshot())


def test_overlong_draft_rejected_not_truncated(setup, monkeypatch):
    monkeypatch.setattr(gen, "openai_generate_sync", lambda *a: json.dumps({"text": "x" * 3801, "teaser": "Hello", "image_prompt": "Cat"}))
    with pytest.raises(ValueError):
        gen.draft(store.snapshot(), {"topic": {"title": "Towels"}})
    assert gen.utf16_length("🐱") == 2


def test_partial_images_resume_only_missing_orientation(setup, monkeypatch):
    from app import vertex_images
    calls = []
    def image(*args, aspect_ratio, **kwargs):
        calls.append(aspect_ratio)
        if aspect_ratio == "9:16" and len(calls) == 2:
            raise RuntimeError("Provider interrupted")
        size = (160, 90) if aspect_ratio == "16:9" else (90, 160)
        data = io.BytesIO()
        Image.new("RGB", size).save(data, format="JPEG")
        return data.getvalue()
    monkeypatch.setattr(vertex_images, "generate_image_vertex", image)
    row = make_row("working", image_version=1, image_prompt="cat and towels")
    with pytest.raises(RuntimeError):
        gen.generate_missing_images(store.snapshot(), row)
    assert Path(store.issue(row["id"])["images"]["horizontal"]).exists()
    gen.generate_missing_images(store.snapshot(), store.issue(row["id"]))
    assert calls == ["16:9", "9:16", "9:16"]


def test_topic_choice_waits_for_separate_approval(setup, monkeypatch):
    row = make_row()
    forbidden = AsyncMock(side_effect=AssertionError("No GPT before theme approval"))
    monkeypatch.setattr(flow, "generate", forbidden)
    asyncio.run(flow.callback(update(row, "choose2"), NS(bot=bot())))
    assert store.issue(row["id"])["state"] == "topic"
    assert store.issue(row["id"])["topic"]["title"] == "Тема 2"


def test_stale_or_foreign_callback_does_nothing(setup):
    row = make_row()
    asyncio.run(flow.callback(update(row, "choose0", user_id=999), NS(bot=bot())))
    assert store.issue(row["id"])["state"] == "topics"
    store.change(row["id"], row["revision"], allowed={"topics"}, state="cancelled")
    asyncio.run(flow.callback(update(row, "choose0"), NS(bot=bot())))
    assert store.issue(row["id"])["state"] == "cancelled"


def test_publication_requires_both_approvals(setup, monkeypatch):
    row = make_row("ready", text_approved=True, images_approved=False)
    send = AsyncMock()
    monkeypatch.setattr(flow, "publish", send)
    asyncio.run(flow.callback(update(row, "publish"), NS(bot=bot())))
    send.assert_not_called()


def test_approval_of_images_is_not_publication(setup, monkeypatch):
    row = make_row("images", text_approved=True)
    monkeypatch.setattr(flow, "show", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(flow, "publish", send)
    asyncio.run(flow.callback(update(row, "approve"), NS(bot=bot())))
    assert store.issue(row["id"])["state"] == "ready"
    send.assert_not_called()


def test_uncertain_publish_only_reconciles_and_handoff_is_separate(setup, monkeypatch):
    image = setup / "story.jpg"
    image.write_bytes(b"image")
    row = make_row("ready", text_approved=True, images_approved=True, text="Approved text", teaser="Как сделать полотенца мягче",
                   images={"vertical": str(image), "horizontal": str(image)})
    send = AsyncMock(side_effect=[TimeoutError(), 25])
    monkeypatch.setattr(flow, "publish", send)
    b = bot()
    asyncio.run(flow.callback(update(row, "publish"), NS(bot=b)))
    saved = store.issue(row["id"])
    assert saved["state"] == "publication_uncertain"
    assert b.send_photo.call_count == 0
    asyncio.run(flow.callback(update(saved, "check"), NS(bot=b)))
    assert send.call_args.kwargs["check_only"] is True
    saved = store.issue(row["id"])
    assert saved["state"] == "published"
    assert saved["url"] == "https://t.me/ubabushkidoma/25"
    assert set(saved["delivery"]) == {"photo", "url", "teaser"}
    texts = [c.kwargs["text"] for c in b.send_message.call_args_list]
    assert saved["url"] in texts and saved["teaser"] in texts


def test_completed_handoff_not_repeated(setup):
    row = make_row("published", delivery={"photo": 1, "url": 2, "teaser": 3})
    b = bot()
    asyncio.run(flow.deliver_handoff(b, 123, row))
    b.send_photo.assert_not_called()
    b.send_message.assert_not_called()


def test_article_reply_consumed_without_recipe_state_change(setup):
    row = make_row()
    store.remember_reply(900, row, "custom")
    u = NS(effective_user=NS(id=123), effective_chat=NS(id=123, type="private"),
           effective_message=NS(reply_to_message=NS(message_id=900), text="New topic"))
    tasks = []
    def create_task(coro):
        tasks.append(coro)
        coro.close()
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(flow.reply(u, NS(application=NS(create_task=create_task))))
    assert len(tasks) == 1


def test_pre_submit_failure_is_distinguished_from_uncertain(setup, monkeypatch):
    @asynccontextmanager
    async def unavailable(config):
        raise ValueError("Session busy")
        yield
    monkeypatch.setattr(pub, "channel_client", unavailable)
    with pytest.raises(pub.NotSubmitted):
        asyncio.run(pub.publish({}, {}))


def test_existing_published_id_is_verified_without_send(setup):
    client = NS(get_messages=AsyncMock(return_value=NS(id=25, raw_text="Approved", photo=True)))
    result = asyncio.run(pub.find_post(client, NS(id=789), {"channel_message_id": 25, "text": "Approved"}))
    assert result == 25


@pytest.mark.parametrize("status,due,enabled,expected", [
    ("pending", False, True, False), ("pending", True, True, True),
    ("ready", False, True, True), ("preparing", False, True, True),
    ("offered", False, True, True), ("failed", False, True, True),
    ("completed", True, True, False), ("ready", True, False, False),
])
def test_advertising_priority_uses_real_queue_state(monkeypatch, status, due, enabled, expected):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import advertising_queue as ads
    from app.models import Base, AdvertisingProduct, AdvertisingQueueSchedule
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AdvertisingProduct.__table__, AdvertisingQueueSchedule.__table__])
    factory = sessionmaker(engine)
    monkeypatch.setattr(ads, "SessionLocal", factory)
    with factory() as db:
        db.add(AdvertisingQueueSchedule(id=1, enabled=enabled, next_run_at=START if due else START + timedelta(days=1)))
        db.add(AdvertisingProduct(title="Реклама", images=[], status=status))
        db.commit()
    assert PRIORITY_CHECK(START) is expected
    engine.dispose()


def test_prepared_first_topics_wait_until_due_without_second_gpt_call(setup, monkeypatch):
    with store.transaction() as data:
        data["prepared_topics"] = [{"title": str(i), "angle": "a"} for i in range(5)]
    assert store.claim_due(START - timedelta(seconds=1)) is None
    def forbidden(*args):
        raise AssertionError("First topics already generated")
    monkeypatch.setattr(gen, "topics", forbidden)
    b = bot()
    asyncio.run(flow.tick(b))
    assert store.current()["state"] == "topics"
    assert not store.snapshot().get("prepared_topics")
    assert b.send_message.call_count == 1


def test_story_handoff_keeps_editorial_slot_until_all_three_messages(setup):
    row = make_row("published", delivery={"photo": 1})
    assert store.blocks_advertising(123)
    assert store.claim_due(START + timedelta(days=10)) is None
    store.checkpoint(row["id"], delivery={"photo": 1, "url": 2, "teaser": 3})
    assert not store.blocks_advertising(123)


def test_vertical_correction_preserves_horizontal_and_text(setup, monkeypatch):
    row = make_row("ready", text="Approved", teaser="Approved teaser", text_approved=True, images_approved=True,
                   images={"horizontal": "keep.jpg", "vertical": "old.jpg"}, image_version=1)
    generate = AsyncMock()
    monkeypatch.setattr(flow, "generate", generate)
    u = NS(effective_chat=NS(id=123), effective_message=NS(text="Пусть кот смотрит на полотенце", reply_text=AsyncMock()))
    asyncio.run(flow.apply_reply(u, NS(bot=bot()), {"id": row["id"], "revision": row["revision"], "kind": "edit_v"}))
    saved = store.issue(row["id"])
    assert saved["images"] == {"horizontal": "keep.jpg"}
    assert saved["text"] == "Approved" and saved["text_approved"]
    assert not saved["images_approved"]
    assert saved["versions"][-1]["images"]["vertical"] == "old.jpg"


def test_publish_uses_persisted_random_id_and_verifies_provider_result(setup):
    from telethon.types import InputFile
    row = make_row("publishing", text="Approved article", images={"horizontal": "cover.jpg"},
                   publish_random_id=1234, publish_started_at=START.isoformat())
    message = NS(id=30, peer_id=NS(channel_id=789), message=row["text"], raw_text=row["text"], photo=True)
    class Client:
        def __init__(self):
            self.requests = []
            self.upload_file = AsyncMock(return_value=InputFile(1, 1, "cover.jpg", "hash"))
            self.get_messages = AsyncMock(return_value=message)
        async def iter_messages(self, *args, **kwargs):
            for msg in []:
                yield msg
        async def __call__(self, request):
            self.requests.append(request)
            return NS(updates=[NS(message=message)])
    client = Client()
    result = asyncio.run(pub._publish_connected(client, NS(id=789), NS(premium=True), row, {"started": False}, check_only=False))
    assert result == 30
    assert client.requests[0].random_id == 1234
    assert client.requests[0].message == "Approved article"
    assert store.issue(row["id"])["channel_message_id"] == 30
    client.get_messages.assert_awaited_once()
