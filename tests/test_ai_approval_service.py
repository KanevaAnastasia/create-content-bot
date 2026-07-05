from __future__ import annotations

import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.ai_approval_service import (
    AI_APPROVAL_STATUS_APPROVE,
    AI_APPROVAL_STATUS_HUMAN_REVIEW,
    AI_APPROVAL_STATUS_REGENERATE_IMAGE,
    AI_APPROVAL_STATUS_REGENERATE_VIDEO,
    AIApprovalConfig,
    AIApprovalResult,
    ai_approval_history_entry,
    call_ai_approval_model,
    check_scene_prompts,
    check_start_frame,
    check_video,
    format_ai_approval_summary,
    get_ai_approval_config,
    normalize_ai_approval_result,
    transcribe_video_audio,
)
from app.llm_queue import LLMCallTimeoutError


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


def _make_test_video(path: Path) -> bytes:
    import app.ai_approval_service as svc

    ffmpeg = svc._resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg is not available")
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=5:duration=1.2",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return path.read_bytes()


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


def test_normalize_ai_approval_promotes_severe_approve_warnings_to_critical() -> None:
    result = normalize_ai_approval_result(
        {
            "status": "APPROVE",
            "overall_score": 1.0,
            "stage": "image",
            "critical_issues": [],
            "warnings": [
                "The prompt requested a top-down view of the grandmother adding flour to the honey mixture, "
                "with the cat lounging on a chair. The provided image shows a close-up of hands mixing ingredients, "
                "with two cats at the bottom and the grandmother at the top. This is a significant deviation from "
                "the requested scene.",
                "The prompt explicitly states the orange cat should be on a chair, observing the process. "
                "The image shows two cats, one orange and one white, at the bottom of the frame, not on a chair. "
                "The white cat is also not described in the prompt's EXPECTED CHARACTERS.",
                "The prompt describes a top-down view where the grandmother's hands are visible. The image shows "
                "the grandmother's head and shoulders at the top, and her hands are not directly visible in the "
                "mixing process. Instead, other hands are shown.",
            ],
        },
        stage="image",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_REGENERATE_IMAGE
    assert result.overall_score < _cfg().human_review_threshold
    assert len(result.critical_issues) == 3
    assert any("significant deviation" in issue for issue in result.critical_issues)


def test_normalize_ai_approval_does_not_turn_positive_note_into_critical() -> None:
    result = normalize_ai_approval_result(
        {
            "status": "APPROVE",
            "overall_score": 0.99,
            "stage": "image",
            "critical_issues": [],
            "warnings": [],
            "positive_notes": ["No visible text or watermark is present."],
        },
        stage="image",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert result.critical_issues == []
    assert result.positive_notes == ["No visible text or watermark is present."]


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


def test_call_ai_approval_model_reuses_vertex_helpers_and_dedupes_media(monkeypatch) -> None:
    import sys
    import types

    calls = {"init": 0, "model": 0}
    captured: dict[str, object] = {}

    class _Resp:
        text = '{"status":"APPROVE","overall_score":0.91}'

    class _Part:
        @staticmethod
        def from_text(text):
            return {"text": text}

        @staticmethod
        def from_data(*, data, mime_type):
            return {"data": data, "mime_type": mime_type}

    class _Model:
        def __init__(self, model_name):
            calls["model"] += 1
            self.model_name = model_name

        def generate_content(self, parts, **_kwargs):
            captured["parts"] = parts
            captured["model"] = self.model_name
            return _Resp()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setattr("app.ai_approval_service.safe_llm_call", lambda fn: fn())

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: calls.__setitem__("init", calls["init"] + 1)
    fake_vm = types.ModuleType("vertexai.generative_models")
    fake_vm.GenerativeModel = _Model
    fake_vm.Part = _Part
    fake_vm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_vm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_vm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_vm)

    png = b"\x89PNG\r\n\x1a\nimage"
    mp4 = b"\x00\x00\x00\x18ftypmp42fake-video"
    result = call_ai_approval_model(
        "approve this",
        images=[png, png, mp4],
        config=AIApprovalConfig(enabled=True, model="approval-model"),
    )

    assert result["status"] == "APPROVE"
    assert captured["model"] == "approval-model"
    assert captured["parts"] == [
        {"text": "approve this"},
        {"data": png, "mime_type": "image/png"},
        {"data": mp4, "mime_type": "video/mp4"},
    ]
    assert calls == {"init": 1, "model": 1}


def test_call_ai_approval_model_stops_fallback_chain_on_timeout(monkeypatch) -> None:
    calls = {"safe": 0}

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("AI_APPROVAL_MODEL_FALLBACKS", "fallback-a,fallback-b")

    def _timeout(_fn, **_kwargs):
        calls["safe"] += 1
        raise LLMCallTimeoutError("timeout")

    monkeypatch.setattr("app.ai_approval_service.safe_llm_call", _timeout)
    monkeypatch.setattr("app.ai_approval_service.vertex_part_from_text", lambda *_args: {"text": "p"})
    monkeypatch.setattr("app.ai_approval_service.get_vertex_safety_settings", lambda: [])

    with pytest.raises(RuntimeError, match="timed out"):
        call_ai_approval_model(
            "primary prompt",
            fallback_prompts=["neutral fallback"],
            config=AIApprovalConfig(enabled=True, model="primary-model"),
        )

    assert calls == {"safe": 1}


def test_check_scene_prompts_requires_character_and_prop_consistency(monkeypatch) -> None:
    import app.ai_approval_service as svc

    def _fake_model(prompt, *, images, config):
        assert images is None
        assert "missing grandma face" in prompt
        assert "back-only, cropped, hidden, tiny, faceless" in prompt
        assert "substituted by a different woman" in prompt
        assert "wrong cat color" in prompt
        assert "giant cat" in prompt
        assert "tiny grandma" in prompt
        assert "tableware, tools, cookware, props, or ingredients" in prompt
        assert "For SCENE NUMBER 1" in prompt
        assert "CHARACTERS SPEAKS" in prompt
        assert "scene 1 VIDEO PROMPT speech does not explicitly say the dish name" in prompt
        assert "START FRAME PROMPT" in prompt
        assert "END FRAME PROMPT" in prompt
        assert "VIDEO PROMPT" in prompt
        assert '"short_description": "рыжий кот"' in prompt
        assert '"name": "Тимоша"' not in prompt
        assert '"zaya"' not in prompt
        assert "рыжевато-кремовый" in prompt
        return {
            "status": "APPROVE",
            "overall_score": 0.92,
            "stage": "prompt",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = check_scene_prompts(
        start_frame_prompt="Grandma and the orange cat near a bowl with flour.",
        end_frame_prompt="The same grandma and orange cat near finished dough.",
        video_prompt="Grandma mixes flour in a ceramic bowl with a wooden spoon.",
        scene_description="Mix dough.",
        recipe_text="Mix flour.",
        scene_number=1,
        expected_characters=["grandma", "cat"],
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE


def test_ai_approval_summary_includes_stage_and_decision() -> None:
    result = AIApprovalResult(
        status=AI_APPROVAL_STATUS_APPROVE,
        stage="prompt",
        overall_score=0.93,
        positive_notes=["prompt is clean"],
    )

    text = format_ai_approval_summary(result)

    assert "decision: prompt / APPROVE" in text
    assert "0.93" in text


def test_character_descriptions_for_approval_hide_names_and_canonical_keys() -> None:
    import app.ai_approval_service as svc

    out = svc._character_descriptions_for_approval(["zaya", "markiz"])
    text = str(out)

    assert "белая кошка" in out
    assert "черно-белый кот" in out
    assert "zaya" not in text
    assert "markiz" not in text
    assert "Зая" not in text
    assert "Маркиз" not in text


def test_check_start_frame_requires_reference_identity_and_normal_scale(monkeypatch) -> None:
    import app.ai_approval_service as svc

    def _fake_model(prompt, *, images, config):
        assert images == [b"image", b"reference"]
        assert "wrong cat coat color" in prompt
        assert "giant cat" in prompt
        assert "tiny grandma" in prompt
        assert "front or three-quarter view" in prompt
        assert "replaced by a different woman" in prompt
        assert "split-screen, collage, stitched image" in prompt
        assert "horizontal/vertical seam" in prompt
        assert "CHARACTER DESCRIPTIONS" in prompt
        assert "end frame image" in prompt
        return {
            "status": "APPROVE",
            "overall_score": 0.9,
            "stage": "image",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = check_start_frame(
        image_bytes=b"image",
        scene_prompt="End frame with grandma and the orange cat at normal scale.",
        scene_description="Serve dough.",
        recipe_text="Serve.",
        scene_number=1,
        previous_reference_images=[b"reference"],
        expected_characters=["grandma", "cat"],
        frame_kind="end",
        config=_cfg(),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE


def test_extract_video_frames_from_real_mp4(tmp_path) -> None:
    import app.ai_approval_service as svc

    video_path = tmp_path / "sample.mp4"
    _make_test_video(video_path)

    frames = svc.extract_video_frames(video_path=video_path, frame_count=4)

    assert len(frames) == 4
    for frame in frames:
        assert frame.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(BytesIO(frame)) as image:
            assert image.size == (64, 64)


def test_check_video_sends_extracted_frames_to_existing_checks(monkeypatch, tmp_path) -> None:
    import app.ai_approval_service as svc

    video_bytes = _make_test_video(tmp_path / "sample.mp4")

    def _fake_model(prompt, *, images, config):
        assert "sampled video frames" in prompt
        assert len(images) == 4
        assert all(image.startswith(b"\x89PNG\r\n\x1a\n") for image in images)
        return {
            "status": "APPROVE",
            "overall_score": 0.91,
            "stage": "video",
            "scene_match_score": 0.9,
            "character_consistency_score": 0.9,
            "food_accuracy_score": 0.9,
            "artifact_risk_score": 0.1,
            "style_consistency_score": 0.9,
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    cfg = AIApprovalConfig(
        enabled=True,
        model="test-model",
        strictness="balanced",
        max_regenerate_attempts=2,
        image_threshold=0.85,
        video_threshold=0.85,
        human_review_threshold=0.70,
        extract_video_frames=True,
        frame_count=4,
        audio_transcription_enabled=False,
    )

    result = check_video(
        video_bytes=video_bytes,
        scene_prompt="A colorful cartoon cooking scene with smooth motion.",
        recipe_text="Mix ingredients.",
        scene_number=1,
        config=cfg,
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert result.warnings == []


def test_check_video_attaches_original_mp4_for_pronunciation_review(monkeypatch, tmp_path) -> None:
    import app.ai_approval_service as svc

    video_bytes = _make_test_video(tmp_path / "sample.mp4")

    def _fake_model(prompt, *, images, config):
        assert "original MP4 for speech/pronunciation review" in prompt
        assert "pronunciation must sound natural" in prompt
        assert "spoken words have natural accent/stress" in prompt
        assert "Wrong stress" in prompt
        assert "giant cat" in prompt
        assert "Grandma's face must remain visible" in prompt
        assert "different woman's face" in prompt
        assert "split-screen, collage-like, stitched" in prompt
        assert "visible seam artifacts" in prompt
        assert images[-1] == video_bytes
        assert all(image.startswith(b"\x89PNG\r\n\x1a\n") for image in images[:-1])
        return {
            "status": "APPROVE",
            "overall_score": 0.91,
            "stage": "video",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    cfg = AIApprovalConfig(
        enabled=True,
        model="test-model",
        strictness="balanced",
        max_regenerate_attempts=2,
        image_threshold=0.85,
        video_threshold=0.85,
        human_review_threshold=0.70,
        extract_video_frames=True,
        frame_count=3,
        audio_transcription_enabled=False,
    )

    result = check_video(
        video_bytes=video_bytes,
        scene_prompt="Grandma says the phrase while the orange cat watches.",
        expected_speech_text="Аккуратно перемешиваем муку.",
        recipe_text="Mix flour.",
        scene_number=1,
        config=cfg,
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE


def test_transcribe_video_audio_recognizes_russian_speech_with_model(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"

    def _fake_model(prompt, *, images, config):
        assert images == [video_bytes]
        assert "EXPECTED SPEECH" in prompt
        assert "Russian word stress" in prompt
        assert "тепЕрь" in prompt
        return {
            "phrase_completed": True,
            "match_score": 0.93,
            "missing_words": [],
            "extra_words": [],
            "cut_off_detected": False,
            "long_pauses_detected": False,
            "speech_speed": "normal",
            "recognized_text": "теперь приготовим крем",
            "status": "APPROVE",
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = transcribe_video_audio(
        video_bytes=video_bytes,
        expected_speech_text="тепЕрь пригОтовим крем",
        duration=6,
        config=AIApprovalConfig(audio_transcription_enabled=True, model="test-model"),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert result.phrase_completed is True
    assert result.match_score == 0.93
    assert result.recognized_text == "теперь приготовим крем"
    assert result.speech_speed == "normal"


def test_transcribe_video_audio_rejects_wrong_russian_stress(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"

    def _fake_model(prompt, *, images, config):
        assert images == [video_bytes]
        assert "тают / тает: stress is тАют / тАет" in prompt
        assert "recognized_text can be correct while stress is wrong" in prompt
        return {
            "phrase_completed": True,
            "match_score": 0.95,
            "missing_words": [],
            "extra_words": [],
            "cut_off_detected": False,
            "long_pauses_detected": False,
            "speech_speed": "normal",
            "recognized_text": "они тают во рту",
            "pronunciation_issues": ["тают pronounced with stress on ю"],
            "wrong_stress_words": ["тают"],
            "phrase_overflow_detected": False,
            "unfinished_line_detected": False,
            "status": "APPROVE",
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = transcribe_video_audio(
        video_bytes=video_bytes,
        expected_speech_text="они тают во рту",
        duration=6,
        config=AIApprovalConfig(audio_transcription_enabled=True, model="test-model"),
    )

    assert result.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO
    assert result.wrong_stress_words == ["тают"]
    assert result.pronunciation_issues == ["тают pronounced with stress on ю"]


def test_check_video_marks_speech_mismatch_as_regenerate(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"
    calls = {"n": 0}

    def _fake_model(_prompt, *, images, config):
        calls["n"] += 1
        assert images
        if calls["n"] == 1:
            return {
                "phrase_completed": False,
                "match_score": 0.2,
                "missing_words": ["крем"],
                "extra_words": ["кот"],
                "cut_off_detected": False,
                "long_pauses_detected": False,
                "speech_speed": "unknown",
                "recognized_text": "кот сидит рядом",
                "status": "HUMAN_REVIEW",
            }
        return {
            "status": "APPROVE",
            "overall_score": 0.95,
            "stage": "video",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)
    monkeypatch.setattr(svc, "extract_video_frames", lambda **_kwargs: [b"\x89PNG\r\n\x1a\nframe"])

    result = check_video(
        video_bytes=video_bytes,
        scene_prompt="Grandma speaks while cooking.",
        expected_speech_text="готовим крем",
        config=AIApprovalConfig(audio_transcription_enabled=True, model="test-model"),
    )

    assert calls["n"] == 2
    assert result.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO
    assert result.speech_completion_score == 0.2
    assert result.critical_issues == ["missing spoken words: крем", "extra spoken words: кот"]


def test_transcribe_video_audio_rejects_rising_intonation(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"

    def _fake_model(prompt, *, images, config):
        assert images == [video_bytes]
        assert "rises at the end like a question" in prompt
        assert "Extra filler, paraphrase, or omitted words are defects" in prompt
        return {
            "phrase_completed": True,
            "match_score": 0.97,
            "missing_words": [],
            "extra_words": [],
            "cut_off_detected": False,
            "long_pauses_detected": False,
            "speech_speed": "normal",
            "recognized_text": "пирожки готовы",
            "pronunciation_issues": [],
            "wrong_stress_words": [],
            "rising_intonation_detected": True,
            "intonation_issues": ["final word rises like a question"],
            "phrase_overflow_detected": False,
            "unfinished_line_detected": False,
            "status": "APPROVE",
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = transcribe_video_audio(
        video_bytes=video_bytes,
        expected_speech_text="пирожки готовы",
        duration=6,
        config=AIApprovalConfig(audio_transcription_enabled=True, model="test-model"),
    )

    assert result.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO
    assert result.rising_intonation_detected is True
    assert result.intonation_issues == ["final word rises like a question"]


def test_check_video_marks_wrong_stress_as_regenerate(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"
    calls = {"n": 0}

    def _fake_model(_prompt, *, images, config):
        calls["n"] += 1
        assert images
        if calls["n"] == 1:
            return {
                "phrase_completed": True,
                "match_score": 0.96,
                "missing_words": [],
                "extra_words": [],
                "cut_off_detected": False,
                "long_pauses_detected": False,
                "speech_speed": "normal",
                "recognized_text": "они тают во рту",
                "pronunciation_issues": ["тают pronounced as таЮт"],
                "wrong_stress_words": ["тают"],
                "phrase_overflow_detected": False,
                "unfinished_line_detected": False,
                "status": "APPROVE",
            }
        return {
            "status": "APPROVE",
            "overall_score": 0.95,
            "stage": "video",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)
    monkeypatch.setattr(svc, "extract_video_frames", lambda **_kwargs: [b"\x89PNG\r\n\x1a\nframe"])

    result = check_video(
        video_bytes=video_bytes,
        scene_prompt="Grandma speaks while cooking.",
        expected_speech_text="они тают во рту",
        config=AIApprovalConfig(audio_transcription_enabled=True, model="test-model"),
    )

    assert calls["n"] == 2
    assert result.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO
    assert result.speech_recognized_text == "они тают во рту"
    assert result.speech_wrong_stress_words == ["тают"]
    assert result.critical_issues == [
        "wrong word stress: тают",
        "pronunciation issue: тают pronounced as таЮт",
    ]


def test_ai_approval_history_entry_keeps_video_speech_diagnostics() -> None:
    result = AIApprovalResult(
        status=AI_APPROVAL_STATUS_REGENERATE_VIDEO,
        stage="video",
        overall_score=0.2,
        critical_issues=["wrong word stress: тают"],
        warnings=["visible text uncertain"],
        positive_notes=["cartoon style"],
        speech_recognized_text="они тают во рту",
        speech_wrong_stress_words=["тают"],
        speech_missing_words=["готовы"],
        speech_extra_words=["ну"],
        model_name="gemini-test",
        prompt_variant=2,
    )

    entry = ai_approval_history_entry(result, attempt=3)

    assert entry["model"] == "gemini-test"
    assert entry["prompt_variant"] == 2
    assert entry["recognized_text"] == "они тают во рту"
    assert entry["wrong_stress_words"] == ["тают"]
    assert entry["critical_issues"] == ["wrong word stress: тают"]
    assert entry["warnings"] == ["visible text uncertain"]
    assert entry["positive_notes"] == ["cartoon style"]


def test_check_video_prompt_hard_fails_echpochmak_visual_regressions(monkeypatch) -> None:
    import app.ai_approval_service as svc

    captured: dict[str, object] = {}

    def _fake_model(prompt, *, images, fallback_prompts=None, config):
        captured["prompt"] = prompt
        captured["fallbacks"] = fallback_prompts or []
        assert images
        return {
            "status": "APPROVE",
            "overall_score": 0.95,
            "stage": "video",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)
    monkeypatch.setattr(svc, "extract_video_frames", lambda **_kwargs: [b"\x89PNG\r\n\x1a\nframe"])

    result = check_video(
        video_bytes=b"\x00\x00\x00\x18ftypmp42fake-video",
        scene_prompt="Grandma serves echpochmaki with a white cat nearby.",
        expected_speech_text="Эчпочмаки готовы.",
        language="ru",
        config=AIApprovalConfig(audio_transcription_enabled=False, model="test-model"),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    prompt = str(captured["prompt"])
    fallback = "\n".join(str(item) for item in captured["fallbacks"])
    for needle in (
        "duplicated white cats",
        "white cat with long ears",
        "cat inside an oven",
        "Any visible text, logo, watermark, caption",
        "Russian speech, wrong stress",
        "rising question-like intonation",
    ):
        assert needle in prompt or needle in fallback


def test_check_scene_prompts_provides_neutral_fallback_prompt(monkeypatch) -> None:
    import app.ai_approval_service as svc

    captured: dict[str, object] = {}

    def _fake_model(prompt, *, images=None, fallback_prompts=None, config):
        captured["prompt"] = prompt
        captured["images"] = images
        captured["fallbacks"] = fallback_prompts or []
        assert config.model == "test-model"
        return {
            "status": "APPROVE",
            "overall_score": 0.95,
            "stage": "prompt",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)

    result = check_scene_prompts(
        start_frame_prompt="Cartoon grandma near the table.",
        end_frame_prompt="",
        video_prompt="CHARACTERS SPEAKS: Эчпочмаки готовы.",
        scene_description="Serve the dish.",
        recipe_text="Эчпочмаки.",
        config=AIApprovalConfig(model="test-model"),
    )

    assert result.status == AI_APPROVAL_STATUS_APPROVE
    assert captured["images"] is None
    fallbacks = captured["fallbacks"]
    assert isinstance(fallbacks, list)
    assert len(fallbacks) == 1
    assert "neutral production QA checker" in fallbacks[0]


def test_ru_video_without_attached_audio_is_not_silent_approve(monkeypatch, tmp_path) -> None:
    import app.ai_approval_service as svc

    missing_path = tmp_path / "missing.mp4"

    def _fake_model(_prompt, *, images, fallback_prompts=None, config):
        _ = fallback_prompts, config
        assert images == [b"\x89PNG\r\n\x1a\nframe"]
        return {
            "status": "APPROVE",
            "overall_score": 0.95,
            "stage": "video",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)
    monkeypatch.setattr(svc, "extract_video_frames", lambda **_kwargs: [b"\x89PNG\r\n\x1a\nframe"])

    result = check_video(
        video_path=missing_path,
        scene_prompt="Grandma speaks.",
        expected_speech_text="Эчпочмаки готовы.",
        language="ru",
        config=AIApprovalConfig(audio_transcription_enabled=True, model="test-model"),
    )

    assert result.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO
    assert "RU speech QA did not receive original MP4/audio input" in result.critical_issues


def test_check_video_marks_rising_intonation_as_regenerate(monkeypatch) -> None:
    import app.ai_approval_service as svc

    video_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"
    calls = {"n": 0}

    def _fake_model(_prompt, *, images, config):
        calls["n"] += 1
        assert images
        if calls["n"] == 1:
            return {
                "phrase_completed": True,
                "match_score": 0.96,
                "missing_words": [],
                "extra_words": [],
                "cut_off_detected": False,
                "long_pauses_detected": False,
                "speech_speed": "normal",
                "recognized_text": "пирог готов",
                "pronunciation_issues": [],
                "wrong_stress_words": [],
                "rising_intonation_detected": True,
                "intonation_issues": ["final cadence sounds like a question"],
                "phrase_overflow_detected": False,
                "unfinished_line_detected": False,
                "status": "APPROVE",
            }
        return {
            "status": "APPROVE",
            "overall_score": 0.95,
            "stage": "video",
            "critical_issues": [],
            "warnings": [],
        }

    monkeypatch.setattr(svc, "call_ai_approval_model", _fake_model)
    monkeypatch.setattr(svc, "extract_video_frames", lambda **_kwargs: [b"\x89PNG\r\n\x1a\nframe"])

    result = check_video(
        video_bytes=video_bytes,
        scene_prompt="Grandma speaks while cooking.",
        expected_speech_text="пирог готов",
        config=AIApprovalConfig(audio_transcription_enabled=True, model="test-model"),
    )

    assert calls["n"] == 2
    assert result.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO
    assert result.critical_issues == [
        "rising/question intonation at end of declarative line",
        "intonation issue: final cadence sounds like a question",
    ]


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
