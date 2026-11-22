import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import advertising_queue as queue
from app import advertising_product_files as files
from app import advertising_speech as speech
from app.models import AdvertisingProduct, AdvertisingQueueSchedule, Base, BotUser

START = datetime(2026, 11, 15, 6, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(queue, "SessionLocal", factory)
    from app import state, session_store
    monkeypatch.setattr(state, "SESSIONS", {})
    monkeypatch.setattr(session_store, "save_session", lambda *args: None)
    queue._LOCKS.clear()
    with factory() as session:
        session.add(AdvertisingQueueSchedule(id=1, next_run_at=START, enabled=True))
        session.add(BotUser(telegram_id=123, normalized_username=queue.QUIET_MODE_OWNER_USERNAME, role="admin", is_active=True))
        session.commit()
    yield factory
    engine.dispose()


def add_product(db, **kwargs):
    with db() as session:
        row = AdvertisingProduct(title="Товар", images=["photo.jpg"], **kwargs)
        session.add(row)
        session.commit()
        return row.id


def prepared():
    return {"source": "Товар", "brief": {"product_name": "Товар"}, "campaigns": [
        {"title": f"Вариант {i}", "idea": "Показать применение", "prepared_scene_plan": {"text_plan": "Полный сценарий"}}
        for i in range(5)]}


def test_cadence_first_at_nine_moscow_and_one_product_per_slot(db):
    later = add_product(db, position=2)
    first = add_product(db, position=1)
    assert queue.claim_due_product(START - timedelta(seconds=1)) is None
    assert queue.claim_due_product(START) == first
    assert queue.claim_due_product(START) is None
    with db() as session:
        assert queue.aware(session.get(AdvertisingQueueSchedule, 1).next_run_at) == START + timedelta(days=2)
        session.get(AdvertisingProduct, first).status = "completed"
        session.commit()
    assert queue.claim_due_product(START + timedelta(days=1)) is None
    assert queue.claim_due_product(START + timedelta(days=2)) == later


def test_restart_catches_up_once_without_burst(db):
    first = add_product(db)
    assert queue.claim_due_product(START + timedelta(days=5)) == first
    with db() as session:
        assert queue.aware(session.get(AdvertisingQueueSchedule, 1).next_run_at) == START + timedelta(days=6)


def test_empty_slot_is_not_triggered_by_an_afternoon_import(db):
    assert queue.claim_due_product(START) is None
    add_product(db)
    assert queue.claim_due_product(START + timedelta(hours=4)) is None


def test_interrupted_preparation_reuses_slot(db):
    first = add_product(db)
    queue.claim_due_product(START)
    assert queue.claim_due_product(START + timedelta(minutes=29)) is None
    assert queue.claim_due_product(START + timedelta(minutes=31)) == first
    with db() as session:
        assert queue.aware(session.get(AdvertisingQueueSchedule, 1).next_run_at) == START + timedelta(days=2)


@pytest.mark.parametrize("state,extra,busy", [
    ("", {}, False), ("waiting_scene_approval", {}, True),
    ("ready_for_production", {"production": {}}, True),
    ("waiting_social_descriptions_approval", {}, True),
    ("ready_for_production", {"social_descriptions_sent": True}, False),
])
def test_busy_includes_review_and_external_final_render(state, extra, busy):
    assert queue.session_is_busy({"state": state, **extra}) is busy


def test_ready_waits_then_delivers_all_scenarios_with_photos(db):
    from app.state import SESSIONS
    product = add_product(db, status="ready", prepared=prepared())
    SESSIONS[123] = {"state": "waiting_scene_media", "generation_run_id": "old"}
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(queue.deliver_ready_product(bot, 123))
    bot.send_message.assert_not_called()
    assert SESSIONS[123]["generation_run_id"] == "old"
    SESSIONS[123] = {"state": "ready_for_production", "social_descriptions_sent": True}
    asyncio.run(queue.deliver_ready_product(bot, 123))
    assert bot.send_message.await_count == 7
    assert SESSIONS[123][queue.PRODUCT_KEY] == str(product)
    assert SESSIONS[123][queue.PHOTOS_KEY] == ["photo.jpg"]
    assert SESSIONS[123]["advertising_format"] == "clip"
    assert "generation_run_id" not in SESSIONS[123]
    asyncio.run(queue.deliver_ready_product(bot, 123))
    assert bot.send_message.await_count == 7
    with db() as session:
        assert session.get(AdvertisingProduct, product).status == "offered"


def test_delivery_retries_without_regenerating_or_repeating_sent_chunks(db):
    product = add_product(db, status="ready", prepared=prepared())
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[None, RuntimeError("offline")]))
    with pytest.raises(RuntimeError):
        asyncio.run(queue.deliver_ready_product(bot, 123))
    bot.send_message = AsyncMock()
    asyncio.run(queue.deliver_ready_product(bot, 123))
    assert bot.send_message.await_count == 6
    assert bot.send_message.call_args_list[0].kwargs["text"].startswith("Вариант 1")


def test_paused_schedule_does_not_offer_or_claim(db):
    add_product(db, status="ready", prepared=prepared())
    with db() as session:
        session.get(AdvertisingQueueSchedule, 1).enabled = False
        session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(queue.deliver_ready_product(bot, 123))
    assert queue.claim_due_product(START) is None
    bot.send_message.assert_not_called()


@pytest.mark.parametrize("completion", [
    {"social_descriptions_sent": True},
    {"state": "", "advertising_post_image_paths": ["approved.png"]},
])
def test_refill_is_separate_once_and_only_after_last_product_finishes(db, completion):
    first = add_product(db, status="offered")
    second = add_product(db, status="pending")
    bot = SimpleNamespace(send_message=AsyncMock())
    done = {queue.PRODUCT_KEY: str(first), **completion}
    asyncio.run(queue.finish_product_if_complete(bot, 123, done))
    bot.send_message.assert_not_called()
    with db() as session:
        session.get(AdvertisingProduct, second).status = "offered"
        session.commit()
    done[queue.PRODUCT_KEY] = str(second)
    asyncio.run(queue.finish_product_if_complete(bot, 123, done))
    asyncio.run(queue.finish_product_if_complete(bot, 123, done))
    bot.send_message.assert_awaited_once_with(chat_id=123, text="Пополните базу")


def test_speech_rewrites_instead_of_truncation(monkeypatch):
    phrase = "Этот товар пригодится в повседневных делах: посмотрите, как удобно им пользоваться дома, и решите, подходит ли он именно вам."
    assert 123 <= len(phrase) <= 130
    monkeypatch.setattr(speech, "gemini_generate_sync", lambda *args, **kwargs: json.dumps({"phrases": [{"index": 0, "text": phrase}]}))
    scenes = [{"speech_ru": "Слишком коротко."}]
    speech.validate_scene_speech(scenes, {"speech_min_chars": 123, "speech_max_chars": 130})
    assert scenes[0]["speech_ru"] == phrase


def test_speech_failure_does_not_accept_invalid_phrase(monkeypatch):
    monkeypatch.setattr(speech, "gemini_generate_sync", lambda *args, **kwargs: '{"phrases": []}')
    with pytest.raises(ValueError):
        speech.validate_scene_speech([{"speech_ru": "Нет."}], {"speech_min_chars": 123, "speech_max_chars": 130})


def workbook_bytes(*, with_image=True):
    # A real minimal OOXML fixture with sparse columns and an embedded drawing.
    out = io.BytesIO()
    m, r, p, xdr, a = (files.NS[key] for key in ("m", "r", "p", "xdr", "a"))
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("xl/workbook.xml", f'<workbook xmlns="{m}" xmlns:r="{r}"><sheets><sheet name="Товары" sheetId="1" r:id="s1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{p}"><Relationship Id="s1" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{m}" xmlns:r="{r}"><sheetData>'
                    '<row r="1"><c r="A1" t="inlineStr"><is><t>Название</t></is></c><c r="B1" t="inlineStr"><is><t>Картинки</t></is></c><c r="C1" t="inlineStr"><is><t>Комментарий</t></is></c></row>'
                    '<row r="2"><c r="A2" t="inlineStr"><is><t>Чайник</t></is></c><c r="C2" t="inlineStr"><is><t>Для кухни</t></is></c></row>'
                    '</sheetData><drawing r:id="d1"/></worksheet>')
        zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", f'<Relationships xmlns="{p}"><Relationship Id="d1" Target="../drawings/drawing1.xml"/></Relationships>')
        zf.writestr("xl/drawings/drawing1.xml", f'<xdr:wsDr xmlns:xdr="{xdr}" xmlns:a="{a}" xmlns:r="{r}"><xdr:twoCellAnchor><xdr:from><xdr:row>1</xdr:row></xdr:from>'
                    + ('<xdr:pic><a:blip r:embed="i1"/></xdr:pic>' if with_image else '') + '</xdr:twoCellAnchor></xdr:wsDr>')
        zf.writestr("xl/drawings/_rels/drawing1.xml.rels", f'<Relationships xmlns="{p}"><Relationship Id="i1" Target="../media/image1.png"/></Relationships>')
        png = io.BytesIO()
        Image.new("RGB", (5, 5), "red").save(png, "PNG")
        zf.writestr("xl/media/image1.png", png.getvalue())
    return out.getvalue()


def test_excel_import_maps_embedded_images_and_sparse_columns():
    rows = files.read_product_workbook(workbook_bytes())
    assert rows[0]["title"] == "Чайник"
    assert rows[0]["format"] == "clip"
    assert rows[0]["comment"] == "Для кухни"
    assert len(rows[0]["photos"]) == 1


def test_excel_rejects_missing_photos_before_import():
    with pytest.raises(ValueError, match="Строка 2"):
        files.read_product_workbook(workbook_bytes(with_image=False))


def test_photo_paths_cannot_read_arbitrary_files(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("private")
    with pytest.raises(ValueError):
        files.photo_path(str(secret))


@pytest.mark.parametrize("mode", ["normal", "advertising", "story", "silence", "long", "copy", "visualization"])
def test_final_ok_never_queues_publication_in_any_mode(monkeypatch, mode):
    from app.handlers import handlers_state_media as media
    from app import publication_copy_store
    from unittest.mock import Mock
    publish = Mock(side_effect=AssertionError("OK must never publish"))
    monkeypatch.setattr(media, "queue_publication_bundle", publish)
    monkeypatch.setattr(media, "mark_generation_run_ready", lambda session: None)
    monkeypatch.setattr(publication_copy_store, "save_bot_publication_copy", lambda *args, **kwargs: None)
    session = {"state": media.STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL, "generation_mode": mode,
               "production": {"post_production": {"renders_by_language": {}}},
               media.SOCIAL_DESCRIPTIONS_PENDING_KEY: {
                   "recipe": "Товар", "origin": "production_final", "copy": media._fallback_social_copy("Товар"),
                   # Even a restored legacy selection must not make OK publish.
                   "selected_target_keys": ["vk:ru"],
               }}
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(media._handle_waiting_social_descriptions_approval_state(update, None, chat_id=123, session=session, text="ОК"))
    publish.assert_not_called()
    assert session["social_descriptions_sent"]
    assert not queue.session_is_busy(session)


def test_explicit_publication_requires_selected_platforms(monkeypatch):
    from app.handlers import handlers_state_media as media
    from unittest.mock import Mock
    publish = Mock()
    monkeypatch.setattr(media, "queue_publication_bundle", publish)
    session = {"state": media.STATE_WAITING_SOCIAL_DESCRIPTIONS_APPROVAL,
               media.SOCIAL_DESCRIPTIONS_PENDING_KEY: {"origin": "production_final", "copy": media._fallback_social_copy("Товар")}}
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(media._handle_waiting_social_descriptions_approval_state(update, None, chat_id=123, session=session,
                                                                       text="Публиковать выбранное"))
    publish.assert_not_called()
    assert not session.get("social_descriptions_sent")
    assert "Не выбрана" in update.message.reply_text.call_args.args[0]


def test_production_preserves_approved_speech_after_unbounded_draft(monkeypatch):
    from app import advertising_generation as advertising
    phrase = "Этот товар пригодится в повседневных делах: посмотрите, как удобно им пользоваться дома, и решите, подходит ли он именно вам."
    monkeypatch.setattr(advertising, "gemini_generate_sync", lambda *args, **kwargs: json.dumps({
        "prompts": [{"speech_by_lang": {"ru": "Переписанная реплика."},
                     "video_prompt_by_lang": {"ru": "Camera pan. CHARACTERS SPEAKS: Переписанная реплика."}}]}))
    result = advertising.generate_advertising_production(
        scenes_plan=[{"scene": 1, "text": phrase, "action": "Показывает товар"}],
        product_brief={"product_name": "Товар", "speech_min_chars": 101},
        target_languages=["ru"],
    )
    scene = result["json_plan"][0]
    assert scene["speech_by_lang"]["ru"] == phrase
    assert phrase in scene["video_prompt_by_lang"]["ru"]
    assert "Camera pan." in scene["video_prompt_by_lang"]["ru"]
    assert "Переписанная реплика." not in scene["video_prompt_by_lang"]["ru"]


def test_migration_seeds_exact_start_and_can_be_reverted():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261113_0027_advertising_queue.py"
    spec = importlib.util.spec_from_file_location("advertising_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            row = connection.execute(text("select enabled, next_run_at from advertising_queue_schedule")).one()
            assert row[0] == 1
            assert str(row[1]).startswith("2026-11-15 06:00:00")
            assert "advertising_products" in inspect(connection).get_table_names()
            migration.downgrade()
            assert "advertising_products" not in inspect(connection).get_table_names()
    engine.dispose()


def test_admin_form_accepts_multiple_photos_and_requires_at_least_one():
    import os
    import sys
    from pathlib import Path
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils.datastructures import MultiValueDict
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin_panel"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")
    import django
    django.setup()
    from pipeline_admin.advertising_admin import ProductForm
    png = io.BytesIO()
    Image.new("RGB", (8, 8)).save(png, "PNG")
    form = ProductForm(data={"title": "Чайник", "format": "post", "position": "1", "comment": ""},
                       files=MultiValueDict({"photos": [SimpleUploadedFile("front.png", png.getvalue()),
                                                       SimpleUploadedFile("back.png", png.getvalue())]}))
    assert form.is_valid(), form.errors
    assert len(form.cleaned_data["photos"]) == 2
    assert form.cleaned_data["format"] == "post"
    assert ProductForm()["format"].value() == "clip"
    empty = ProductForm(data={"title": "Чайник", "position": "1"})
    assert not empty.is_valid()
    assert "photos" in empty.errors


def test_admin_in_progress_product_has_viewable_detail_without_upload_field():
    import os
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin_panel"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")
    import django
    django.setup()
    from django.contrib.admin import AdminSite
    from pipeline_admin.advertising_admin import AdvertisingProductAdmin
    from pipeline_admin.models import AdvertisingProduct as AdminProduct
    model_admin = AdvertisingProductAdmin(AdminProduct, AdminSite())
    request = SimpleNamespace(user=SimpleNamespace(has_perm=lambda *args: True))
    product = AdminProduct(title="Товар", status="offered", images=[])
    assert "photos" not in model_admin.get_fields(request, product)
    assert "preview" in model_admin.get_fields(request, product)
    assert "photos" in model_admin.get_fields(request)


def test_draft_speech_has_no_upper_limit_and_ignores_legacy_maximum(monkeypatch):
    text = "Бабушка показывает средство и объясняет, как внимательно прочитать инструкцию на упаковке перед применением. Затем она выбирает подходящий способ уборки и делится своим планом с котом."
    monkeypatch.setattr(speech, "gemini_generate_sync", lambda *args, **kwargs: pytest.fail("Long draft should not be shortened"))
    scenes = [{"speech_ru": text}]
    speech.validate_scene_speech(scenes, {"speech_min_chars": 101, "speech_max_chars": 130})
    assert scenes[0]["speech_ru"] == text
    assert len(text) > 130


def test_speech_selects_developed_candidate_with_string_index(monkeypatch):
    good = "Этот товар пригодится в повседневных делах: посмотрите, как удобно им пользоваться дома, и решите, подходит ли он именно вам."
    monkeypatch.setattr(speech, "gemini_generate_sync", lambda *args, **kwargs: json.dumps({"phrases": [
        {"index": "0", "candidates": ["Нет.", good]}]}))
    scenes = [{"speech_ru": "Коротко."}]
    speech.validate_scene_speech(scenes, {"speech_min_chars": 101})
    assert scenes[0]["speech_ru"] == good


def test_post_queue_prepares_and_delivers_post_copy_without_clip_speech(db, monkeypatch, tmp_path):
    from app import advertising_generation as generation
    from app.state import SESSIONS
    payload = prepared()
    for campaign in payload["campaigns"]:
        campaign.pop("prepared_scene_plan")
    product = add_product(db, status="preparing", format="post", prepared=payload)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"photo")
    monkeypatch.setattr(queue, "photo_path", lambda _: photo)
    calls = []
    def post(**kwargs):
        calls.append(kwargs["campaign"]["title"])
        return {"post_text": "Готовый рекламный пост", "image_prompts": ["Одна картинка"]}
    monkeypatch.setattr(generation, "generate_advertising_post_package", post)
    monkeypatch.setattr(generation, "generate_advertising_scene_plan", lambda **kwargs: pytest.fail("Post must not generate clip scenes"))
    queue.prepare_product(product)
    assert len(calls) == 5
    queue.prepare_product(product)
    assert len(calls) == 5
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(queue.deliver_ready_product(bot, 123))
    assert SESSIONS[123]["advertising_format"] == "post"
    assert "Готовый рекламный пост" in bot.send_message.call_args_list[1].kwargs["text"]


def test_excel_optional_type_column():
    source = zipfile.ZipFile(io.BytesIO(workbook_bytes()))
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                data = data.decode().replace('</row>', '<c r="D1" t="inlineStr"><is><t>Тип</t></is></c></row>', 1)
                pos = data.index('</row>', data.index('<row r="2">'))
                data = data[:pos] + '<c r="D2" t="inlineStr"><is><t>Пост</t></is></c>' + data[pos:]
                data = data.encode()
            target.writestr(name, data)
    assert files.read_product_workbook(result.getvalue())[0]["format"] == "post"


def test_scene_plan_keeps_full_draft_over_130_characters(monkeypatch):
    from app import advertising_generation as generation
    text = "Бабушка показывает средство и объясняет, как внимательно прочитать инструкцию на упаковке перед применением. Затем она выбирает подходящий способ уборки и делится своим планом с котом."
    monkeypatch.setattr(generation, "gemini_generate_sync", lambda *args, **kwargs: json.dumps({"scenes": [
        {"speech_ru": text, "action": "Показывает товар"} for _ in range(3)]}))
    result = generation.generate_advertising_scene_plan(source_text="Товар", product_brief={"speech_min_chars": 101})
    assert all(scene["text"] == text for scene in result["json_plan"])


def test_format_migration_defaults_existing_products_to_clip():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    migrations = []
    for name in ("20261113_0027_advertising_queue", "20261115_0028_advertising_format"):
        path = Path(__file__).resolve().parents[1] / "migrations/versions" / (name + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrations.append(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migrations[0].upgrade()
            connection.execute(text("INSERT INTO advertising_products (id, title, images, prepared) VALUES ('1', 'Existing', '[]', '{}')"))
            migrations[1].upgrade()
            assert connection.scalar(text("SELECT format FROM advertising_products")) == "clip"
            migrations[1].downgrade()
            migrations[0].downgrade()
    engine.dispose()


def test_return_to_ad_concepts_does_not_reload_old_recipe_from_run(db, monkeypatch):
    from app import session_store
    from app.models import GenerationRun
    from app.handlers.handlers_state_scene import _restore_advertising_campaign_choice
    monkeypatch.setattr(session_store, "SessionLocal", db)
    # The queue fixture replaces save_session for delivery tests; use the real implementation here.
    import importlib
    original_save = importlib.util.spec_from_file_location("app._ad_repair_session_store", session_store.__file__)
    module = importlib.util.module_from_spec(original_save)
    original_save.loader.exec_module(module)
    monkeypatch.setattr(module, "SessionLocal", db)
    with db() as database:
        run = GenerationRun(status="draft", language="ru", script="Яблочный пирог")
        database.add(run)
        database.commit()
        run_id = str(run.id)
    session = {"state": "waiting_recipe_approval", "generation_mode": "advertising",
               "generation_run_id": run_id, "recipe_full_text": "Яблочный пирог",
               "advertising_source_text": "Оливье с комбайном", "advertising_campaigns": [{"title": "Оливье"}]}
    _restore_advertising_campaign_choice(session)
    module.save_session(123, session)
    restored = module.load_session(123)
    assert restored["state"] == "waiting_advertising_campaign_choice"
    assert restored["recipe_full_text"] == "Оливье с комбайном"
    assert not restored.get("generation_run_id")
    with db() as database:
        import uuid
        assert database.get(GenerationRun, uuid.UUID(run_id)).script == "Яблочный пирог"


@pytest.mark.parametrize("format", ["clip", "post"])
def test_reoffer_revised_concepts_without_stale_prepared_scripts(db, format):
    payload = prepared()
    for campaign in payload["campaigns"]:
        campaign.pop("prepared_scene_plan", None)
        campaign.update(hook="Оливье", product_role="Смузи, тесто и крем", cta="Отличный подарок")
    product = add_product(db, status="ready", prepared=payload, format=format)
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(queue.deliver_ready_product(bot, 123))
    assert bot.send_message.await_count == 7
    assert "Отличный подарок" in bot.send_message.call_args_list[1].kwargs["text"]
    with db() as session:
        assert session.get(AdvertisingProduct, product).status == "offered"
