import asyncio
from types import SimpleNamespace

import app.handlers.handlers_state_media as handlers_state_media
from app.scene_media_candidates import register_image_candidate


def test_scene_media_video_ok_uses_liked_image_candidate_and_requests_1080(monkeypatch):
    scene = {
        "duration_sec": 8,
        "text": "Фраза",
        "model": "veo3_fast",
        "video_prompt_by_lang": {"ru": "lang:ru test CHARACTERS SPEAKS: Фраза"},
        "video_results_by_lang": {},
    }
    old_candidate = register_image_candidate(scene, kind="start", image_bytes=b"old-start")
    latest_candidate = register_image_candidate(scene, kind="start", image_bytes=b"latest-start")
    old_candidate["liked"] = True
    scene["start_frame_image_b64"] = latest_candidate["image_b64"]
    session = {
        "target_languages": ["ru"],
        "scene_media_substep": handlers_state_media.SCENE_MEDIA_SUBSTEP_VIDEO,
        "image_scene_index": 0,
        "production": {"json_plan": [scene, {"video_results_by_lang": {}, "video_prompt_by_lang": {"ru": "x"}}]},
    }
    captured = {}

    async def fake_generate_video(*_args, **kwargs):
        captured.update(kwargs)
        return b"\x00\x00\x00\x18ftypisom"

    async def fake_reply_text(_text, **_kwargs):
        return None

    async def fake_reply_video(*, video, caption):
        return SimpleNamespace(message_id=321, video=video, caption=caption)

    monkeypatch.setattr(handlers_state_media, "_generate_video_bytes_with_high_load_retry", fake_generate_video)
    monkeypatch.setattr(handlers_state_media, "_safe_reply_video", lambda message, *, video, caption: fake_reply_video(video=video, caption=caption))
    monkeypatch.setattr(handlers_state_media, "probe_video_bytes_dimensions", lambda _bytes: (1080, 1920))
    monkeypatch.setattr(handlers_state_media, "reply_text_without_keyboard", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(handlers_state_media, "get_last_video_provider", lambda: "veo")
    monkeypatch.setattr(handlers_state_media, "estimated_scene_video_cost", lambda *_args, **_kwargs: {"estimated_total_cost_usd": 0.1})
    monkeypatch.setattr(handlers_state_media, "save_video_result_estimate", lambda *args, **kwargs: None)
    monkeypatch.setattr(handlers_state_media, "_send_next_scene_media_step", lambda *_args, **_kwargs: asyncio.sleep(0))

    update = SimpleNamespace(message=SimpleNamespace(reply_text=fake_reply_text))

    out = asyncio.run(
        handlers_state_media._scene_media_video_ok(
            update,
            context=None,
            chat_id=1,
            session=session,
            scene=scene,
            idx=0,
            total=2,
        )
    )

    assert out is True
    assert captured["start_image"] == b"old-start"
    assert captured["quality"] == "1080"
