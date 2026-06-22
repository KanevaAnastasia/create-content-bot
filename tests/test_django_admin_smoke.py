import importlib.util
import os
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(importlib.util.find_spec("django") is None, reason="django is not installed")


def test_django_admin_session_form_updates_scene_json():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.forms import ChatSessionAdminForm
    from pipeline_admin.models import ChatSession

    instance = ChatSession(
        chat_id=987654321,
        state="waiting_scene_media",
        generation_run_id=None,
        session_json={
            "state": "waiting_scene_media",
            "image_scene_index": 0,
            "production": {
                "json_plan": [
                    {
                        "scene": 1,
                        "start_frame_prompt": "old start",
                        "end_frame_prompt": "old end",
                        "video_prompt_by_lang": {"ru": "old"},
                    }
                ]
            },
        },
    )
    form = ChatSessionAdminForm(
        data={
            "chat_id": "987654321",
            "state": "waiting_scene_media",
            "generation_run_id": "",
            "session_json": instance.session_json,
            "selected_concept": "",
            "viral_concept_brief": '{"dish_name": "Пирог"}',
            "recipe_full_text": "",
            "scenes_plan_json": "[]",
            "production_plan_json": '[{"scene": 1, "start_frame_prompt": "old start", "end_frame_prompt": "old end"}]',
            "scene_index": "0",
            "start_frame_prompt": "new start",
            "end_frame_requested": "on",
            "end_frame_prompt": "new end",
            "video_prompt_by_lang_json": '{"ru": "new"}',
            "speech_by_lang_json": "{}",
            "syntax_retry_context_json": "{}",
            "video_approvals_by_lang_json": '{"ru": false}',
        },
        instance=instance,
    )
    form.validate_unique = lambda: None

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    scene = saved.session_json["production"]["json_plan"][0]
    assert scene["start_frame_prompt"] == "new start"
    assert scene["end_frame_requested"] is True
    assert scene["video_prompt_by_lang"]["ru"] == "new"
    assert saved.session_json["viral_concept_brief"] == {"dish_name": "Пирог"}


def test_django_admin_session_form_rejects_wrong_json_shapes():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.forms import ChatSessionAdminForm
    from pipeline_admin.models import ChatSession

    instance = ChatSession(
        chat_id=987654322,
        state="waiting_scene_media",
        generation_run_id=None,
        session_json={"state": "waiting_scene_media", "image_scene_index": "broken"},
    )
    form = ChatSessionAdminForm(
        data={
            "chat_id": "987654322",
            "state": "waiting_scene_media",
            "generation_run_id": "",
            "session_json": instance.session_json,
            "selected_concept": "",
            "viral_concept_brief": "[]",
            "recipe_full_text": "",
            "scenes_plan_json": "{}",
            "production_plan_json": "{}",
            "scene_index": "0",
            "start_frame_prompt": "",
            "end_frame_requested": "",
            "end_frame_prompt": "",
            "video_prompt_by_lang_json": "[]",
            "speech_by_lang_json": "[]",
            "syntax_retry_context_json": "[]",
            "video_approvals_by_lang_json": "[]",
        },
        instance=instance,
    )
    form.validate_unique = lambda: None

    assert form.is_valid() is False
    assert "viral_concept_brief" in form.errors
    assert "production_plan_json" in form.errors


def test_django_admin_json_fields_accept_decoded_values():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django

    django.setup()

    from pipeline_admin.models import PipelineStepCheckpoint

    field = PipelineStepCheckpoint._meta.get_field("payload_json")

    assert field.from_db_value({"ok": True}, None, None) == {"ok": True}
    assert field.from_db_value([{"ok": True}], None, None) == [{"ok": True}]


def test_django_admin_image_candidate_form_replaces_blob():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django
    from django.core.files.uploadedfile import SimpleUploadedFile

    django.setup()

    from pipeline_admin.forms import SceneImageCandidateAdminForm
    from pipeline_admin.models import SceneImageCandidate

    instance = SceneImageCandidate(candidate_id="img-1", kind="start", image_blob=b"old")
    form = SceneImageCandidateAdminForm(
        data={"kind": "start", "liked": "on", "approved_for_video": ""},
        files={"replacement_image": SimpleUploadedFile("frame.png", b"new-image", content_type="image/png")},
        instance=instance,
    )

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    assert saved.image_blob == b"new-image"
    assert saved.liked is True


def test_django_admin_video_candidate_form_replaces_blob():
    admin_dir = Path(__file__).resolve().parents[1] / "admin_panel"
    sys.path.insert(0, str(admin_dir))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")

    import django
    from django.core.files.uploadedfile import SimpleUploadedFile

    django.setup()

    from pipeline_admin.forms import SceneVideoCandidateAdminForm
    from pipeline_admin.models import SceneVideoCandidate

    instance = SceneVideoCandidate(candidate_id="vid-1", language="ru", video_blob=b"old", speed_coef=1.2)
    form = SceneVideoCandidateAdminForm(
        data={"liked": "", "approved_for_production": "on", "speed_coef": "1.1"},
        files={"replacement_video": SimpleUploadedFile("scene.mp4", b"new-video", content_type="video/mp4")},
        instance=instance,
    )

    assert form.is_valid(), form.errors
    saved = form.save(commit=False)
    assert saved.video_blob == b"new-video"
    assert saved.approved_for_production is True
    assert saved.speed_coef == 1.1
