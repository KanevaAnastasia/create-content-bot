from app.scene_media_candidates import (
    DEFAULT_SCENE_VIDEO_SPEED_COEF,
    register_image_candidate,
    register_media_message_candidate,
    register_video_candidate,
    resolve_candidate_by_message,
    resolve_scene_image_for_video,
    resolve_scene_video_for_production,
)


def test_resolve_scene_image_for_video_prefers_last_liked_candidate():
    scene = {}
    first = register_image_candidate(scene, kind="start", image_bytes=b"one")
    second = register_image_candidate(scene, kind="start", image_bytes=b"two")
    first["liked"] = True

    selected = resolve_scene_image_for_video(scene, kind="start")

    assert selected is first
    assert selected["image_b64"]
    assert second["candidate_id"] != first["candidate_id"]


def test_resolve_scene_video_for_production_prefers_last_liked_then_latest():
    scene = {}
    first = register_video_candidate(scene, language="ru", video_bytes=b"one")
    second = register_video_candidate(scene, language="ru", video_bytes=b"two")
    second["approved_for_production"] = True

    assert resolve_scene_video_for_production(scene, "ru") is second

    first["liked"] = True
    assert resolve_scene_video_for_production(scene, "ru") is first
    assert first["speed_coef"] == DEFAULT_SCENE_VIDEO_SPEED_COEF


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
