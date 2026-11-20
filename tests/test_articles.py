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
from app.article_formatting import render_article

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
    store.change(saved["id"], saved["revision"], allowed={"publication_uncertain"}, state="working", images={"square": "saved.jpg"})
    store.recover_interrupted()
    assert store.issue(row["id"])["state"] == "failed"
    assert store.issue(row["id"])["images"] == {"square": "saved.jpg"}


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


def test_one_generation_uses_style_references_and_reuses_original_after_crop_failure(setup, monkeypatch):
    from app import vertex_images
    calls = []
    def image(prompt, *, aspect_ratio, reference_images):
        calls.append(aspect_ratio)
        assert reference_images == [Path(p).read_bytes() for p in store.snapshot()["reference_paths"]]
        assert "центральный квадрат" in prompt and "на одной высоте" in prompt
        data = io.BytesIO()
        Image.new("RGB", (90, 160)).save(data, format="JPEG")
        return data.getvalue()
    monkeypatch.setattr(vertex_images, "generate_image_vertex", image)
    row = make_row("working", image_version=1, image_prompt="cat and towels")
    with monkeypatch.context() as patch:
        patch.setattr(gen, "crop_square", lambda *a: (_ for _ in ()).throw(OSError("disk interrupted")))
        with pytest.raises(OSError):
            gen.generate_missing_images(store.snapshot(), row)
    assert Path(store.issue(row["id"])["images"]["vertical"]).exists()
    result = gen.generate_missing_images(store.snapshot(), store.issue(row["id"]))
    assert calls == ["9:16"]
    import hashlib
    reference = Path(store.snapshot()["reference_paths"][0])
    assert store.issue(row["id"])["image_reference_manifest"] == [{
        "file": reference.name, "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "bytes": reference.stat().st_size, "dimensions": [160, 90]}]
    with Image.open(result["square"]) as square:
        assert square.size == (90, 90)
    with Image.open(result["vertical"]) as vertical:
        assert vertical.size == (90, 160)


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
                   images={"vertical": str(image), "square": str(image)})
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


def test_image_correction_replaces_both_formats_and_preserves_text(setup, monkeypatch):
    row = make_row("ready", text="Approved", teaser="Approved teaser", text_approved=True, images_approved=True,
                   images={"square": "keep.jpg", "vertical": "old.jpg"}, image_version=1)
    generate = AsyncMock()
    monkeypatch.setattr(flow, "generate", generate)
    u = NS(effective_chat=NS(id=123), effective_message=NS(text="Пусть кот смотрит на полотенце", reply_text=AsyncMock()))
    asyncio.run(flow.apply_reply(u, NS(bot=bot()), {"id": row["id"], "revision": row["revision"], "kind": "imageedit"}))
    saved = store.issue(row["id"])
    assert saved["images"] == {} and saved["image_version"] == 2
    assert saved["text"] == "Approved" and saved["text_approved"]
    assert not saved["images_approved"]
    assert saved["versions"][-1]["images"]["vertical"] == "old.jpg"


@pytest.mark.parametrize("copy", ["Approved article", "🥖 Хлеб\n\n**3. Подберите размер крошки**\n\nБез изменений <>&."])
def test_publish_uses_persisted_random_id_and_verifies_provider_result(setup, copy):
    from telethon.types import InputFile
    article_text, spans = render_article(copy)
    row = make_row("publishing", text=copy, images={"square": "cover.jpg"},
                   publish_random_id=1234, publish_started_at=START.isoformat())
    message = NS(id=30, peer_id=NS(channel_id=789), message=article_text, raw_text=article_text, photo=True)
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
    assert client.requests[0].message == article_text
    assert [(entity.offset, entity.length) for entity in client.requests[0].entities] == spans
    client.upload_file.assert_awaited_once_with("cover.jpg")
    assert store.issue(row["id"])["channel_message_id"] == 30
    client.get_messages.assert_awaited_once()
    assert asyncio.run(pub.find_post(client, NS(id=789), store.issue(row["id"]))) == 30


def test_bold_preview_preserves_text_and_uses_telegram_utf16_offsets(setup):
    copy = "**🥖 Хлеб**\n\n**3. Подберите размер крошки**\n\nТекст <>& и *обычная звёздочка*."
    row = make_row("text", text=copy, teaser="Что сделать с хлебом?")
    b = bot()
    asyncio.run(flow.show(b, 123, row))
    preview = b.send_message.call_args_list[0].kwargs
    expected = copy.replace("**", "")
    assert preview["text"] == expected
    assert preview["parse_mode"] is None
    encoded = expected.encode("utf-16-le")
    headings = [encoded[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le") for e in preview["entities"]]
    assert headings == ["🥖 Хлеб", "3. Подберите размер крошки"]
    assert all(e.type == "bold" for e in preview["entities"])
    assert render_article("Обычный текст <>&, без **парного маркера") == ("Обычный текст <>&, без **парного маркера", [])


def private_test_target():
    return {"channel_id": 987, "username": None, "title": "for_me", "publisher_id": 456, "access_hash": 654}


def test_manual_test_preserves_primary_schedule_and_destination(setup):
    topics = [{"title": str(i), "angle": "a"} for i in range(5)]
    with store.transaction() as data:
        data["prepared_topics"] = topics
    before = store.snapshot()
    target = private_test_target()
    row = store.create_test(target, topics)
    target["channel_id"] = 111
    after = store.snapshot()
    for key in ("channel_id", "username", "publisher_id", "next_run_at", "prepared_topics"):
        assert after[key] == before[key]
    assert store.target_for(row)["channel_id"] == 987
    assert store.blocks_advertising(123)
    with pytest.raises(ValueError):
        store.create_test(private_test_target(), topics)
    store.change(row["id"], row["revision"], allowed={"topics"}, state="cancelled")
    scheduled = store.claim_due(START)
    assert not scheduled.get("test_mode")
    assert store.target_for(scheduled)["channel_id"] == 789
    assert scheduled["topics"] == topics


@pytest.mark.parametrize("invalid", [{"channel_id": 789}, {"publisher_id": 999}, {"access_hash": None}])
def test_test_channel_must_be_explicit_and_separate(setup, invalid):
    target = private_test_target() | invalid
    with pytest.raises(ValueError):
        store.create_test(target, [{"title": str(i), "angle": "a"} for i in range(5)])
    assert not store.snapshot()["issues"]


def test_missing_test_destination_never_falls_back_to_primary(setup):
    with pytest.raises(ValueError):
        store.target_for({"test_mode": True})


def test_publisher_uses_issue_target_instead_of_primary_config(setup, monkeypatch):
    target = private_test_target()
    row = {"test_mode": True, "target": target}
    @asynccontextmanager
    async def connected(config):
        assert config == target
        yield NS(), NS(id=987), NS(id=456)
    monkeypatch.setattr(pub, "channel_client", connected)
    send = AsyncMock(return_value=41)
    monkeypatch.setattr(pub, "_publish_connected", send)
    assert asyncio.run(pub.publish(store.snapshot(), row)) == 41
    assert send.call_args.args[1].id == 987
    assert store.post_url(row, 41) == "https://t.me/c/987/41"


def test_test_approval_screen_and_handoff_use_private_destination(setup, monkeypatch):
    photo = setup / "photo.jpg"
    photo.write_bytes(b"image")
    row = store.create_test(private_test_target(), [{"title": str(i), "angle": "a"} for i in range(5)])
    row = store.change(row["id"], row["revision"], allowed={"topics"}, state="ready", topic=row["topics"][0],
                       text="Approved", teaser="Как сделать полотенца мягче", text_approved=True, images_approved=True,
                       images={"square": str(photo), "vertical": str(photo)})
    b = bot()
    asyncio.run(flow.show(b, 123, row))
    screen = b.send_message.call_args.kwargs
    assert 'Тест для канала «for_me»' in screen["text"]
    assert screen["reply_markup"].inline_keyboard[0][0].text == "Опубликовать в for_me"
    send = AsyncMock(return_value=41)
    monkeypatch.setattr(flow, "publish", send)
    asyncio.run(flow.callback(update(row, "publish"), NS(bot=b)))
    saved = store.issue(row["id"])
    assert saved["url"] == "https://t.me/c/987/41"
    assert saved["target"]["channel_id"] == 987
    assert set(saved["delivery"]) == {"photo", "url", "teaser"}
    assert store.snapshot()["channel_id"] == 789


def test_channel_client_resolves_private_peer_and_verifies_id(setup, monkeypatch):
    import telethon
    from telethon.types import InputPeerChannel
    from app import syntax_telethon_fallback as fallback
    client = NS(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                get_me=AsyncMock(return_value=NS(id=456)),
                get_entity=AsyncMock(return_value=NS(id=987, creator=True, admin_rights=None)))
    monkeypatch.setattr(telethon, "TelegramClient", lambda *a: client)
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test")
    candidate = NS(kind="file", value="test.session")
    monkeypatch.setattr(fallback, "_acquire_syntax_session_candidate", lambda *a: candidate)
    released = []
    monkeypatch.setattr(fallback, "_release_syntax_session_candidate", lambda c: released.append(c))
    async def connect():
        async with pub.channel_client(private_test_target()) as (_, channel, __):
            return channel.id
    assert asyncio.run(connect()) == 987
    peer = client.get_entity.call_args.args[0]
    assert isinstance(peer, InputPeerChannel)
    assert (peer.channel_id, peer.access_hash) == (987, 654)
    client.get_entity.return_value = NS(id=789, creator=True, admin_rights=None)
    with pytest.raises(ValueError):
        asyncio.run(connect())
    assert released == [candidate, candidate]


def selected_topics():
    return [{"title": f"Кулинарная тема {i}", "angle": "Полезный приём для кухни", "category": "culinary",
             "section": gen.CULINARY_SECTIONS[i]}
            for i in range(4)] + [
            {"title": f"Бытовая тема {i}", "angle": "Полезный приём для дома", "category": "household",
             "section": gen.HOUSEHOLD_SECTIONS[i]} for i in range(1)]


def test_topics_request_only_five_cards_and_enforce_four_culinary_one_household(setup, monkeypatch):
    calls = []
    def generate(prompt):
        calls.append(prompt)
        return json.dumps({"topics": selected_topics()}, ensure_ascii=False)
    monkeypatch.setattr(gen, "openai_generate_sync", generate)
    values = gen.topics(store.snapshot())
    assert [v["category"] for v in values] == ["culinary"] * 4 + ["household"]
    assert len({v["section"] for v in values[:4]}) == 4
    assert values[-1]["title"] == "Бытовая тема 0"
    assert len(calls) == 1
    assert '"candidates"' not in calls[0] and '"scores"' not in calls[0]
    assert '"examples"' not in calls[0]


@pytest.mark.parametrize("problem", ["narrow", "wrong_ratio", "wrong_section", "long_title", "long_angle", "duplicate", "newline"])
def test_topics_reject_narrow_misclassified_or_unreadable_cards(setup, monkeypatch, problem):
    candidates = selected_topics()
    if problem == "narrow":
        for value in candidates[:4]:
            value["section"] = "baking"
    elif problem == "wrong_ratio":
        candidates[0].update(category="household", section="laundry")
    elif problem == "wrong_section":
        candidates[0]["section"] = "laundry"
    elif problem == "long_title":
        candidates[0]["title"] = "а" * (gen.TOPIC_TITLE_LIMIT + 1)
    elif problem == "long_angle":
        candidates[0]["angle"] = "а" * 141
    elif problem == "duplicate":
        candidates[0]["title"] = candidates[1]["title"]
    else:
        candidates[0]["title"] = "Два\nзаголовка"
    monkeypatch.setattr(gen, "openai_generate_sync", lambda *a: json.dumps({"topics": candidates}))
    with pytest.raises(ValueError):
        gen.topics(store.snapshot())


def test_article_and_teaser_are_generated_once_with_previous_approved_copy(setup, monkeypatch):
    text = "Полезный совет. " * 50
    raw = {"text": text + "Это просто.", "teaser": "Каша без комочков", "image_prompt": "Grandmother — warm light"}
    prompts = []
    def generate(prompt):
        prompts.append(prompt)
        return json.dumps(raw, ensure_ascii=False)
    monkeypatch.setattr(gen, "openai_generate_sync", generate)
    result = gen.draft(store.snapshot(), {"topic": {"title": "Каша"}, "teaser": "Предыдущий анонс"})
    assert result == raw
    assert "Предыдущий анонс" in prompts[0]
    assert "**3. Подберите размер крошки**" in prompts[0]
    assert "каждый короткий подзаголовок шага выделяй жирным" in prompts[0]
    assert len(prompts) == 1


def test_article_request_uses_responses_api_with_full_policy_and_no_second_pass(setup, monkeypatch):
    from app import llm_utils as llm
    from app.writing_style import HUMAN_WRITING_RULES
    requests = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-astra")
    monkeypatch.setattr(llm, "selected_text_model", lambda: None)
    monkeypatch.setattr(llm, "safe_llm_call", lambda fn, **kwargs: fn())
    class Response:
        ok = True
        def __init__(self, output):
            self.output = output
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def json(self):
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": self.output}]}]}
    def post(url, **kwargs):
        requests.append((url, kwargs["json"]))
        return Response(json.dumps({"topics": selected_topics()}))
    monkeypatch.setattr(llm.requests, "post", post)
    assert len(gen.topics(store.snapshot())) == 5
    assert len(requests) == 1
    for url, payload in requests:
        assert url == "https://api.openai.com/v1/responses"
        assert payload["model"] == "gpt-6-astra"
        assert payload["input"][0]["content"][0]["text"].startswith(HUMAN_WRITING_RULES + "\n\n")


@pytest.mark.parametrize("teaser", ["а" * 61, "Что сделать с хлебом?\nРассказываем подробно."])
def test_long_or_multiline_teaser_is_rejected_without_rewrite(setup, monkeypatch, teaser):
    calls = []
    def generate(prompt):
        calls.append(prompt)
        return json.dumps({"text": "Текст статьи. " * 100, "teaser": teaser, "image_prompt": "cat"})
    monkeypatch.setattr(gen, "openai_generate_sync", generate)
    with pytest.raises(ValueError):
        gen.draft(store.snapshot(), {"topic": {"title": "Хлеб"}})
    assert len(calls) == 1


def test_image_approval_does_not_upload_images_again(setup, monkeypatch):
    row = make_row("images", text_approved=True, images={"square": "cover.jpg", "vertical": "story.jpg"})
    b = bot()
    publish = AsyncMock()
    monkeypatch.setattr(flow, "publish", publish)
    asyncio.run(flow.callback(update(row, "approve"), NS(bot=b)))
    assert store.issue(row["id"])["state"] == "ready"
    b.send_photo.assert_not_called()
    publish.assert_not_called()
    assert "квадратную картинку 1:1" in b.send_message.call_args.kwargs["text"]


@pytest.mark.parametrize("state", ["images", "ready"])
def test_regenerate_starts_a_new_image_version_without_touching_approved_text(setup, monkeypatch, state):
    row = make_row(state, text="Approved article", teaser="Короткий анонс", text_approved=True,
                   images_approved=True, image_version=3, square_crop=40, image_feedback="Кот рядом с бабушкой",
                   images={"square": "cover.jpg", "vertical": "story.jpg"},
                   image_reference_manifest=[{"file": "original.jpg"}])
    b = bot()
    generation, publish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(flow, "generate", generation)
    monkeypatch.setattr(flow, "publish", publish)
    asyncio.run(flow.callback(update(row, "regenerate"), NS(bot=b)))
    saved = store.issue(row["id"])
    assert saved["state"] == "working" and saved["operation"] == "images"
    assert saved["image_version"] == 4 and saved["square_crop"] == 50
    assert saved["text"] == row["text"] and saved["teaser"] == row["teaser"] and saved["text_approved"]
    assert saved["image_feedback"] == row["image_feedback"]
    assert not saved["images"] and not saved["images_approved"] and not saved["image_reference_manifest"]
    assert saved["versions"][-1]["images"] == row["images"]
    assert saved["versions"][-1]["image_reference_manifest"] == row["image_reference_manifest"]
    generation.assert_awaited_once()
    publish.assert_not_called()
    asyncio.run(flow.callback(update(row, "regenerate"), NS(bot=b)))
    generation.assert_awaited_once()


def test_new_image_version_does_not_reuse_old_vertical_as_reference(setup, monkeypatch):
    from app import vertex_images
    original = setup / "old-generated.jpg"
    Image.new("RGB", (90, 160), "red").save(original)
    row = make_row("images", image_version=1, image_prompt="Generic apron and floor-level cat",
                   images={"vertical": str(original), "square": str(original)})
    row = store.change(row["id"], row["revision"], allowed={"images"}, state="working", **flow.new_image_version(row))
    calls = []
    def generate(prompt, *, reference_images, aspect_ratio):
        calls.append(reference_images)
        assert original.read_bytes() not in reference_images
        assert "Замок идентичности бабушки" in prompt
        assert "референсы — единственный" in prompt and "Персонажи в кадре: бабушка, рыжий кот" in prompt
        data = io.BytesIO()
        Image.new("RGB", (90, 160), "blue").save(data, format="JPEG")
        return data.getvalue()
    monkeypatch.setattr(vertex_images, "generate_image_vertex", generate)
    images = gen.generate_missing_images(store.snapshot(), row)
    assert len(calls) == 1
    assert calls[0] == [Path(p).read_bytes() for p in store.snapshot()["reference_paths"]]
    assert images["vertical"] != str(original) and original.is_file()


def test_article_image_uses_the_recipe_project_style_identity_and_atmosphere():
    from app.handlers.handlers_media_scene import _IMAGE_REFERENCE_STYLE_BLOCK, _GRANDMA_FACE_IDENTITY_LOCK, PIXAR_REFERENCE_STYLE_PHRASE
    from app.image_atmosphere import SOVIET_IMAGE_ATMOSPHERE
    from app.character_catalog import character_identity_lines
    prompt = gen.article_image_prompt({"image_prompt": "Бабушка пересыпает сухари в банку", "image_feedback": "Кот смотрит в камеру"})
    for rule in (_IMAGE_REFERENCE_STYLE_BLOCK, _GRANDMA_FACE_IDENTITY_LOCK, SOVIET_IMAGE_ATMOSPHERE, PIXAR_REFERENCE_STYLE_PHRASE):
        assert rule in prompt
    for description in character_identity_lines(["grandma", "cat"]):
        assert description in prompt
    assert "Кот смотрит в камеру" in prompt
    assert "Create one vertical 9:16 high quality 3D storybook" not in prompt


def test_reference_button_shows_actual_configured_originals_without_generation(setup, monkeypatch):
    row = make_row("images")
    b = bot()
    generate = AsyncMock()
    monkeypatch.setattr(flow, "generate", generate)
    seen = []
    async def photo(**kwargs):
        seen.append(kwargs["photo"].read())
    b.send_photo.side_effect = photo
    asyncio.run(flow.callback(update(row, "references"), NS(bot=b)))
    assert seen == [Path(p).read_bytes() for p in store.snapshot()["reference_paths"]]
    assert store.issue(row["id"]) == row
    generate.assert_not_called()


def test_square_reframing_never_generates_and_keeps_vertical_original(setup, monkeypatch):
    vertical = setup / "vertical.jpg"
    img = Image.new("RGB", (90, 160))
    for y in range(160):
        for x in range(90):
            img.putpixel((x, y), (y, y, y))
    img.save(vertical)
    original = vertical.read_bytes()
    row = make_row("ready", images={"vertical": str(vertical)}, image_version=1, images_approved=True,
                   text="Approved", text_approved=True)
    from app import vertex_images
    monkeypatch.setattr(vertex_images, "generate_image_vertex", lambda *a, **k: pytest.fail("No paid call for crop"))
    b = bot()
    asyncio.run(flow.callback(update(row, "crop_up"), NS(bot=b)))
    saved = store.issue(row["id"])
    assert saved["state"] == "images" and not saved["images_approved"]
    assert saved["text_approved"] and saved["text"] == "Approved"
    assert saved["square_crop"] == 40 and vertical.read_bytes() == original
    with Image.open(saved["images"]["square"]) as square:
        assert square.size == (90, 90)
        assert abs(square.getpixel((45, 0))[0] - 28) <= 2


def test_resume_saved_article_after_new_topics_does_not_regenerate(setup, monkeypatch):
    row = make_row("topics", text="Saved text", teaser="Что сделать с засохшим хлебом?")
    b = bot()
    generate = AsyncMock()
    monkeypatch.setattr(flow, "generate", generate)
    asyncio.run(flow.show(b, 123, row))
    assert any(button.callback_data.endswith(":resume") for line in b.send_message.call_args.kwargs["reply_markup"].inline_keyboard for button in line)
    asyncio.run(flow.callback(update(row, "resume"), NS(bot=b)))
    saved = store.issue(row["id"])
    assert saved["state"] == "topic" and saved["text"] == "Saved text"
    asyncio.run(flow.callback(update(saved, "draft"), NS(bot=b)))
    assert store.issue(row["id"])["state"] == "text"
    generate.assert_not_called()


@pytest.mark.parametrize("state,previous", [("topic", "topics"), ("text", "topic"), ("images", "text"), ("ready", "images")])
def test_back_keeps_materials_and_requires_reapproval(setup, monkeypatch, state, previous):
    row = make_row(state, text="Saved text", teaser="Saved teaser", images={"square": "h.jpg", "vertical": "v.jpg"},
                   text_approved=True, images_approved=True)
    monkeypatch.setattr(flow, "show", AsyncMock())
    generate, publish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(flow, "generate", generate)
    monkeypatch.setattr(flow, "publish", publish)
    asyncio.run(flow.callback(update(row, "back"), NS(bot=bot())))
    saved = store.issue(row["id"])
    assert saved["state"] == previous
    assert saved["text"] == row["text"] and saved["images"] == row["images"]
    assert not saved["images_approved"]
    if previous in {"topics", "topic", "text"}:
        assert not saved["text_approved"]
    assert saved["revision"] == row["revision"] + 1
    generate.assert_not_called()
    publish.assert_not_called()


def test_cancel_screen_can_restore_the_step_and_legacy_cancellation(setup):
    row = make_row("topic")
    b = bot()
    asyncio.run(flow.callback(update(row, "cancel"), NS(bot=b)))
    cancelled = store.issue(row["id"])
    assert cancelled["cancelled_from"] == "topic"
    buttons = b.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].text == "Предыдущий шаг"
    asyncio.run(flow.callback(update(cancelled, "back"), NS(bot=b)))
    assert store.issue(row["id"])["state"] == "topic"
    with store.transaction() as data:
        current = data["issues"][row["id"]]
        current["state"] = "cancelled"
        current.pop("cancelled_from")
    assert store.go_back(row["id"], current["revision"])["state"] == "topic"


@pytest.mark.parametrize("state", ["publishing", "publication_uncertain", "published", "working"])
def test_back_cannot_cross_publication_or_running_generation(setup, state):
    row = make_row(state)
    assert store.previous_state(row) is None
    with pytest.raises(ValueError):
        store.go_back(row["id"], row["revision"])


def test_cancelled_article_cannot_resume_over_new_active_issue(setup):
    old = make_row("cancelled")
    current = store.claim_due(START + timedelta(days=3))
    with pytest.raises(ValueError):
        store.go_back(old["id"], old["revision"])
    assert store.current()["id"] == current["id"]


def test_back_rejects_stale_buttons(setup):
    row = make_row("text", text="Saved text")
    store.go_back(row["id"], row["revision"])
    with pytest.raises(ValueError):
        store.go_back(row["id"], row["revision"])


def test_forward_after_back_reuses_existing_text_and_images(setup, monkeypatch):
    photo = setup / "saved.jpg"
    photo.write_bytes(b"saved")
    row = make_row("topic", text="Saved article", teaser="Saved teaser", image_version=2,
                   images={"square": str(photo), "vertical": str(photo)})
    forbidden = AsyncMock(side_effect=AssertionError("Saved material must not be regenerated"))
    monkeypatch.setattr(flow, "generate", forbidden)
    asyncio.run(flow.callback(update(row, "draft"), NS(bot=bot())))
    saved = store.issue(row["id"])
    assert saved["state"] == "text"
    asyncio.run(flow.callback(update(saved, "images"), NS(bot=bot())))
    saved = store.issue(row["id"])
    assert saved["state"] == "images" and saved["text_approved"]
    assert saved["images"] == row["images"] and saved["image_version"] == 2
    forbidden.assert_not_called()


def test_selecting_different_topic_archives_old_material_and_clears_approvals(setup):
    row = make_row("topics", text="Old article", teaser="Old teaser", image_prompt="Old scene", feedback="Old correction",
                   images={"square": "old.jpg"}, text_approved=True, images_approved=True)
    asyncio.run(flow.callback(update(row, "choose1"), NS(bot=bot())))
    saved = store.issue(row["id"])
    assert saved["topic"] == row["topics"][1]
    assert not saved["text"] and not saved["images"] and not saved["feedback"]
    assert not saved["text_approved"] and not saved["images_approved"]
    assert saved["versions"][-1]["text"] == "Old article"
    assert saved["versions"][-1]["images"] == {"square": "old.jpg"}


def test_back_from_failed_image_generation_keeps_partial_output(setup):
    row = make_row("failed", text="Saved text", images={"square": "saved.jpg"}, operation="images")
    saved = store.go_back(row["id"], row["revision"])
    assert saved["state"] == "text"
    assert saved["images"] == {"square": "saved.jpg"}
