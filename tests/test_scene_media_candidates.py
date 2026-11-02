from app.scene_media_candidates import (
    DEFAULT_SCENE_VIDEO_SPEED_COEF,
    _speech_text_from_prompt,
    default_scene_video_speed_coef,
    register_image_candidate,
    register_media_message_candidate,
    register_video_candidate,
    resolve_candidate_by_message,
    resolve_scene_image_for_video,
    resolve_latest_approved_scene_video_for_final_build,
    resolve_scene_video_for_production,
)
from app.scene_media_store import merge_missing_scene_text_from_approved_candidate


def test_candidate_prompt_speech_parser_ignores_inline_instruction_blocks():
    prompt = (
        "CHARACTERS SPEAKS: Granny's apple pie: tender crust, crispy topping. "
        "voice_lock_full_grandma: female voice English language. "
        "speech_timing_plan: - 0.00-6.00s: Granny's apple pie. "
        "Render this video in vertical portrait 9:16 aspect ratio."
    )

    assert _speech_text_from_prompt(prompt) == "Granny's apple pie: tender crust, crispy topping."


def test_merge_missing_scene_text_from_candidate_not_blocked_by_video_prompt():
    scene = {
        "video_prompt_by_lang": {"ru": "CHARACTERS SPEAKS: старая подсказка"},
        "video_candidates_by_lang": {
            "ru": [
                {
                    "approved_for_production": True,
                    "speech_by_lang": {"ru": "Нормальная реплика"},
                    "speech_cues_by_lang": {
                        "ru": [{"text": "Нормальная реплика", "start_sec": 0.0, "end_sec": 6.0}]
                    },
                }
            ]
        },
    }

    merge_missing_scene_text_from_approved_candidate(scene)

    assert scene["speech_by_lang"] == {"ru": "Нормальная реплика"}
    assert scene["speech_cues_by_lang"]["ru"][0]["text"] == "Нормальная реплика"
    assert scene["video_prompt_by_lang"] == {"ru": "CHARACTERS SPEAKS: старая подсказка"}


def test_resolve_scene_image_for_video_prefers_last_liked_candidate():
    scene = {}
    first = register_image_candidate(scene, kind="start", image_bytes=b"one")
    second = register_image_candidate(scene, kind="start", image_bytes=b"two")
    first["liked"] = True

    selected = resolve_scene_image_for_video(scene, kind="start")

    assert selected is first
    assert selected["image_b64"]
    assert second["candidate_id"] != first["candidate_id"]


def test_resolve_scene_video_for_production_prefers_approved_over_unapproved_liked():
    scene = {}
    first = register_video_candidate(scene, language="ru", video_bytes=b"one")
    second = register_video_candidate(scene, language="ru", video_bytes=b"two")
    second["approved_for_production"] = True

    assert resolve_scene_video_for_production(scene, "ru") is second

    first["liked"] = True
    assert resolve_scene_video_for_production(scene, "ru") is second
    assert first["speed_coef"] == DEFAULT_SCENE_VIDEO_SPEED_COEF


def test_default_scene_video_speed_coef_depends_on_model():
    assert default_scene_video_speed_coef({"model": "kling"}) == 1.0
    assert default_scene_video_speed_coef({"model": "kling_3_0"}) == 1.0
    assert default_scene_video_speed_coef({"model": "veo3_fast"}) == 1.1
    assert default_scene_video_speed_coef({"model": "veo"}) == 1.1
    assert default_scene_video_speed_coef({"video_provider": "veo"}) == 1.1


def test_register_video_candidate_uses_model_speed_default():
    kling_scene = {"model": "kling"}
    veo_scene = {"model": "veo3_fast"}

    kling = register_video_candidate(kling_scene, language="ru", video_bytes=b"kling")
    veo = register_video_candidate(veo_scene, language="ru", video_bytes=b"veo")

    assert kling["speed_coef"] == 1.0
    assert veo["speed_coef"] == 1.1


def test_resolve_scene_video_for_production_prefers_liked_among_approved():
    scene = {}
    first = register_video_candidate(scene, language="ru", video_bytes=b"one")
    second = register_video_candidate(scene, language="ru", video_bytes=b"two")
    first["approved_for_production"] = True
    second["approved_for_production"] = True
    first["liked"] = True

    assert resolve_scene_video_for_production(scene, "ru") is first


def test_resolve_latest_approved_scene_video_for_final_build_uses_newest_approved_only():
    scene = {}
    first = register_video_candidate(scene, language="ru", video_bytes=b"one")
    second = register_video_candidate(scene, language="ru", video_bytes=b"two")
    third = register_video_candidate(scene, language="ru", video_bytes=b"three")
    fourth = register_video_candidate(scene, language="ru", video_bytes=b"four")
    first["approved_for_production"] = True
    second["liked"] = True
    third["approved_for_production"] = True

    assert resolve_latest_approved_scene_video_for_final_build(scene, "ru") is third
    assert fourth["approved_for_production"] is False


def test_resolve_latest_approved_scene_video_for_final_build_skips_english_ru_snapshot():
    scene = {}
    first = register_video_candidate(scene, language="ru", video_bytes=b"one")
    second = register_video_candidate(scene, language="ru", video_bytes=b"two")
    first["approved_for_production"] = True
    first["speech_by_lang"] = {"ru": "Русская фраза"}
    second["approved_for_production"] = True
    second["speech_by_lang"] = {"ru": "English phrase"}
    second["video_prompt_by_lang"] = {"ru": "CHARACTERS SPEAKS: English phrase"}

    assert resolve_latest_approved_scene_video_for_final_build(scene, "ru") is first


def test_resolve_latest_approved_scene_video_for_final_build_rejects_unapproved_only_candidates():
    scene = {}
    register_video_candidate(scene, language="ru", video_bytes=b"one")
    register_video_candidate(scene, language="ru", video_bytes=b"two")

    assert resolve_latest_approved_scene_video_for_final_build(scene, "ru") is None


def test_resolve_candidate_by_message_marks_target_candidate_liked():
    scene = {}
    image_candidate = register_image_candidate(scene, kind="start", image_bytes=b"img")
    session = {}
    register_media_message_candidate(
        session,
        message_id=123,
        scene_idx=0,
        media_type="image",
        candidate_id=image_candidate["candidate_id"],
        kind="start",
    )

    resolved = resolve_candidate_by_message(
        session,
        [scene],
        message_id=123,
        liked=True,
    )

    assert resolved is not None
    assert image_candidate["liked"] is True
