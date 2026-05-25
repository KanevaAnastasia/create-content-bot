from __future__ import annotations

from app.ai_approval_service import (
    AI_APPROVAL_STATUS_APPROVE,
    AI_APPROVAL_STATUS_HUMAN_REVIEW,
    AI_APPROVAL_STATUS_REGENERATE_IMAGE,
    AIApprovalConfig,
    check_video,
    get_ai_approval_config,
    normalize_ai_approval_result,
)


def _cfg() -> AIApprovalConfig:
    return AIApprovalConfig(
        enabled=True,
        model="test-model",
        strictness="balanced",
        max_regenerate_attempts=2,
        image_threshold=0.85,
        video_threshold=0.85,
        human_review_threshold=0.70,
        extract_video_frames=True,
        frame_count=5,
        audio_transcription_enabled=False,
    )


def test_normalize_ai_approval_valid_json_high_score_approves() -> None:
    result = normalize_ai_approval_result(
        {
            "status": "APPROVE",
            "overall_score": 0.91,
            "stage": "prompt",
            "scene_match_score": 0.9,
            "character_consistency_score": 0.88,
            "food_accuracy_score": 0.92,
            "artifact_risk_score": 0.1,
            "style_consistency_score": 0.95,
            "critical_issues": [],
            "warnings": [],
            "positive_notes": ["scene matches prompt"],
            "recommended_action": "continue",
        },
        stage="prompt",
        raw_model_response='{"status":"APPROVE"}',
        model_name="test-model",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert result.overall_score == 0.91
    assert result.raw_model_response == '{"status":"APPROVE"}'


def test_normalize_ai_approval_critical_issue_overrides_score() -> None:
    result = normalize_ai_approval_result(
        {
            "status": "APPROVE",
            "overall_score": 0.97,
            "stage": "image",
            "critical_issues": ["visible subtitles"],
        },
        stage="image",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_REGENERATE_IMAGE
    assert result.critical_issues == ["visible subtitles"]


def test_normalize_ai_approval_mid_score_goes_to_human_review() -> None:
    result = normalize_ai_approval_result(
        {"status": "APPROVE", "overall_score": 0.78, "stage": "video"},
        stage="video",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_HUMAN_REVIEW


def test_ai_approval_default_model_ignores_main_gemini_model(monkeypatch) -> None:
    monkeypatch.delenv("AI_APPROVAL_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")

    cfg = get_ai_approval_config()

    assert cfg.model == "gemini-2.5-flash-lite"


def test_check_video_continues_with_start_frame_when_ffmpeg_missing(monkeypatch) -> None:
    import app.ai_approval_service as svc

    def _missing_ffmpeg(**_kwargs):
        raise RuntimeError("ffmpeg is not available")

    def _fake_model(_prompt, *, images, config):
        assert images == [b"start-frame"]
        return {
            "status": "APPROVE",
            "overall_score": 0.91,
            "stage": "video",
            "warnings": [],
        }

    monkeypatch.setattr(svc, "extract_video_frames", _missing_ffmpeg)
    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = check_video(
        video_bytes=b"mp4",
        scene_prompt="prompt",
        generated_start_frame=b"start-frame",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert any("ffmpeg is not available" in w for w in result.warnings)


def test_check_video_sends_original_video_when_ffmpeg_missing_without_start_frame(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"

    def _missing_ffmpeg(**_kwargs):
        raise RuntimeError("ffmpeg is not available")

    def _fake_model(_prompt, *, images, config):
        assert images == [video_bytes]
        return {
            "status": "APPROVE",
            "overall_score": 0.9,
            "stage": "video",
            "warnings": [],
        }

    monkeypatch.setattr(svc, "extract_video_frames", _missing_ffmpeg)
    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = check_video(
        video_bytes=video_bytes,
        scene_prompt="prompt",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert any("ffmpeg is not available" in w for w in result.warnings)
    assert any("original video" in w for w in result.warnings)


def test_check_video_retries_when_model_treats_local_limitations_as_critical(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"
    calls = []

    def _missing_ffmpeg(**_kwargs):
        raise RuntimeError("ffmpeg is not available")

    def _fake_model(prompt, *, images, config):
        calls.append(prompt)
        assert images == [video_bytes]
        if len(calls) == 1:
            assert "speech transcription is not configured" not in prompt
            return {
                "status": "HUMAN_REVIEW",
                "overall_score": 0.0,
                "stage": "video",
                "critical_issues": [
                    "Speech transcription is not configured, cannot evaluate speech completion.",
                    "Unable to extract video frames for analysis.",
                ],
                "warnings": [],
            }
        assert "tooling limitations" in prompt
        return {
            "status": "APPROVE",
            "overall_score": 0.9,
            "stage": "video",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "extract_video_frames", _missing_ffmpeg)
    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = check_video(
        video_bytes=video_bytes,
        scene_prompt="prompt",
        config=_cfg(),
    )

    assert len(calls) == 2
    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert result.critical_issues == []
