"""AI QA approval layer for scene prompts, start frames, and videos."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .character_catalog import character_prompt_catalog, format_character_short_descriptions, sanitize_character_names_and_keys
from .llm_utils import extract_json_object, safe_llm_call

logger = logging.getLogger("bot")

AI_APPROVAL_STATUS_APPROVE = "APPROVE"
AI_APPROVAL_STATUS_FIX_PROMPT = "FIX_PROMPT"
AI_APPROVAL_STATUS_REGENERATE_IMAGE = "REGENERATE_IMAGE"
AI_APPROVAL_STATUS_REGENERATE_VIDEO = "REGENERATE_VIDEO"
AI_APPROVAL_STATUS_HUMAN_REVIEW = "HUMAN_REVIEW"

AIApprovalStage = Literal["prompt", "image", "video", "final"]
ALLOWED_STATUSES = {
    AI_APPROVAL_STATUS_APPROVE,
    AI_APPROVAL_STATUS_FIX_PROMPT,
    AI_APPROVAL_STATUS_REGENERATE_IMAGE,
    AI_APPROVAL_STATUS_REGENERATE_VIDEO,
    AI_APPROVAL_STATUS_HUMAN_REVIEW,
}

AI_APPROVAL_MODEL_ENV_NAME = "AI_APPROVAL_MODEL"
AI_APPROVAL_ENABLED_ENV_NAME = "AI_APPROVAL_ENABLED"
AI_APPROVAL_STRICTNESS_ENV_NAME = "AI_APPROVAL_STRICTNESS"
AI_APPROVAL_MAX_REGENERATE_ATTEMPTS_ENV_NAME = "AI_APPROVAL_MAX_REGENERATE_ATTEMPTS"
AI_APPROVAL_IMAGE_THRESHOLD_ENV_NAME = "AI_APPROVAL_IMAGE_THRESHOLD"
AI_APPROVAL_VIDEO_THRESHOLD_ENV_NAME = "AI_APPROVAL_VIDEO_THRESHOLD"
AI_APPROVAL_HUMAN_REVIEW_THRESHOLD_ENV_NAME = "AI_APPROVAL_HUMAN_REVIEW_THRESHOLD"
AI_APPROVAL_EXTRACT_VIDEO_FRAMES_ENV_NAME = "AI_APPROVAL_EXTRACT_VIDEO_FRAMES"
AI_APPROVAL_FRAME_COUNT_ENV_NAME = "AI_APPROVAL_FRAME_COUNT"
AI_APPROVAL_AUDIO_TRANSCRIPTION_ENABLED_ENV_NAME = "AI_APPROVAL_AUDIO_TRANSCRIPTION_ENABLED"


@dataclass(frozen=True, slots=True)
class AIApprovalConfig:
    enabled: bool = True
    model: str = "gemini-2.5-flash-lite"
    strictness: str = "balanced"
    max_regenerate_attempts: int = 2
    image_threshold: float = 0.85
    video_threshold: float = 0.85
    human_review_threshold: float = 0.70
    extract_video_frames: bool = True
    frame_count: int = 5
    audio_transcription_enabled: bool = False


@dataclass(slots=True)
class AIApprovalResult:
    status: str = AI_APPROVAL_STATUS_HUMAN_REVIEW
    overall_score: float = 0.0
    stage: AIApprovalStage = "final"
    scene_match_score: float = 0.0
    character_consistency_score: float = 0.0
    food_accuracy_score: float = 0.0
    artifact_risk_score: float = 1.0
    style_consistency_score: float = 0.0
    speech_completion_score: float | None = None
    critical_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    positive_notes: list[str] = field(default_factory=list)
    recommended_action: str = ""
    raw_model_response: str | None = None
    model_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SpeechTranscriptionResult:
    phrase_completed: bool = False
    match_score: float = 0.0
    missing_words: list[str] = field(default_factory=list)
    extra_words: list[str] = field(default_factory=list)
    cut_off_detected: bool = False
    long_pauses_detected: bool = False
    speech_speed: str = "unknown"
    recognized_text: str = ""
    phrase_overflow_detected: bool = False
    unfinished_line_detected: bool = False
    status: str = AI_APPROVAL_STATUS_HUMAN_REVIEW

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def get_ai_approval_config() -> AIApprovalConfig:
    strictness = (os.getenv(AI_APPROVAL_STRICTNESS_ENV_NAME) or "balanced").strip().lower()
    if strictness == "conservative":
        approve_default = 0.90
        human_default = 0.75
    elif strictness == "aggressive":
        approve_default = 0.80
        human_default = 0.65
    else:
        strictness = "balanced"
        approve_default = 0.85
        human_default = 0.70
    model = os.getenv(AI_APPROVAL_MODEL_ENV_NAME) or "gemini-2.5-flash-lite"
    return AIApprovalConfig(
        enabled=_bool_env(AI_APPROVAL_ENABLED_ENV_NAME, True),
        model=model.strip() or "gemini-2.5-flash-lite",
        strictness=strictness,
        max_regenerate_attempts=max(0, _int_env(AI_APPROVAL_MAX_REGENERATE_ATTEMPTS_ENV_NAME, 2)),
        image_threshold=_float_env(AI_APPROVAL_IMAGE_THRESHOLD_ENV_NAME, approve_default),
        video_threshold=_float_env(AI_APPROVAL_VIDEO_THRESHOLD_ENV_NAME, approve_default),
        human_review_threshold=_float_env(AI_APPROVAL_HUMAN_REVIEW_THRESHOLD_ENV_NAME, human_default),
        extract_video_frames=_bool_env(AI_APPROVAL_EXTRACT_VIDEO_FRAMES_ENV_NAME, True),
        frame_count=max(1, _int_env(AI_APPROVAL_FRAME_COUNT_ENV_NAME, 5)),
        audio_transcription_enabled=_bool_env(AI_APPROVAL_AUDIO_TRANSCRIPTION_ENABLED_ENV_NAME, False),
    )


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _character_descriptions_for_approval(keys: list[str] | None) -> dict[str, dict[str, str]]:
    """Expose only sanitized appearance data to QA prompts, without names or canonical keys."""
    catalog = character_prompt_catalog(keys or None)
    out: dict[str, dict[str, str]] = {}
    for key, item in catalog.items():
        label = str(item.get("short_description") or key).strip() or key
        out[label] = {
            "short_description": label,
            "description": sanitize_character_names_and_keys(str(item.get("description") or "")),
        }
    return out


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "y", "on"}:
            return True
        if low in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _stage_failure_status(stage: str) -> str:
    if stage == "prompt":
        return AI_APPROVAL_STATUS_FIX_PROMPT
    if stage == "image":
        return AI_APPROVAL_STATUS_REGENERATE_IMAGE
    if stage == "video":
        return AI_APPROVAL_STATUS_REGENERATE_VIDEO
    return AI_APPROVAL_STATUS_HUMAN_REVIEW


def threshold_for_stage(stage: str, config: AIApprovalConfig | None = None) -> float:
    cfg = config or get_ai_approval_config()
    if stage == "image":
        return cfg.image_threshold
    if stage == "video":
        return cfg.video_threshold
    return max(cfg.image_threshold, cfg.video_threshold)


SEVERE_AI_APPROVAL_REASON_PATTERNS: tuple[str, ...] = (
    "significant deviation",
    "major deviation",
    "major mismatch",
    "does not match",
    "do not match",
    "not match",
    "mismatch with the prompt",
    "wrong character",
    "wrong cat",
    "wrong color",
    "wrong coat",
    "wrong food",
    "missing required",
    "missing grandmother",
    "missing grandma",
    "missing cat",
    "extra character",
    "not described in the prompt",
    "not described in prompt",
    "not in expected characters",
    "not on a chair",
    "not directly visible",
    "hands are not visible",
    "instead, other hands",
    "giant cat",
    "tiny grandma",
    "strongly distorted",
    "visible subtitles",
    "visible text",
    "watermark",
    "photorealistic",
    "значительное отклонение",
    "сильно отличается",
    "не соответствует",
    "не совпадает",
    "не описан",
    "не описана",
    "не указано",
    "лишний персонаж",
    "лишняя кошка",
    "лишний кот",
    "нет бабушки",
    "нет кота",
    "не видно бабушку",
    "не видно кота",
    "не видны руки",
    "гигантский кот",
    "крошечная бабушка",
    "неправильный цвет",
    "неправильный окрас",
    "субтитры",
    "водяной знак",
    "фотореализм",
)


def _looks_like_severe_ai_approval_reason(text: str) -> bool:
    low = " ".join(str(text or "").strip().lower().split())
    if not low:
        return False
    return any(pattern in low for pattern in SEVERE_AI_APPROVAL_REASON_PATTERNS)


def normalize_ai_approval_result(
    data: dict[str, Any],
    *,
    stage: AIApprovalStage,
    raw_model_response: str | None = None,
    model_name: str | None = None,
    config: AIApprovalConfig | None = None,
) -> AIApprovalResult:
    cfg = config or get_ai_approval_config()
    score = _clamp01(data.get("overall_score"), 0.0)
    critical = _string_list(data.get("critical_issues"))
    warnings = _string_list(data.get("warnings"))
    positive_notes = _string_list(data.get("positive_notes"))
    recommended_action = str(data.get("recommended_action") or "").strip()
    severe_reasons = [item for item in [*warnings, recommended_action] if _looks_like_severe_ai_approval_reason(item)]
    for reason in severe_reasons:
        if reason not in critical:
            critical.append(reason)
    if severe_reasons:
        score = min(score, max(0.0, cfg.human_review_threshold - 0.01))
    status = str(data.get("status") or "").strip().upper()
    if status not in ALLOWED_STATUSES:
        status = AI_APPROVAL_STATUS_HUMAN_REVIEW
    if critical:
        status = _stage_failure_status(stage)
    elif score >= threshold_for_stage(stage, cfg):
        status = AI_APPROVAL_STATUS_APPROVE
    elif score >= cfg.human_review_threshold:
        status = AI_APPROVAL_STATUS_HUMAN_REVIEW
    else:
        status = _stage_failure_status(stage)
    if stage == "prompt" and status == AI_APPROVAL_STATUS_REGENERATE_IMAGE:
        status = AI_APPROVAL_STATUS_FIX_PROMPT
    if stage == "prompt" and status == AI_APPROVAL_STATUS_REGENERATE_VIDEO:
        status = AI_APPROVAL_STATUS_FIX_PROMPT
    if stage == "image" and status in {AI_APPROVAL_STATUS_FIX_PROMPT, AI_APPROVAL_STATUS_REGENERATE_VIDEO}:
        status = AI_APPROVAL_STATUS_REGENERATE_IMAGE
    if stage == "video" and status in {AI_APPROVAL_STATUS_FIX_PROMPT, AI_APPROVAL_STATUS_REGENERATE_IMAGE}:
        status = AI_APPROVAL_STATUS_REGENERATE_VIDEO
    return AIApprovalResult(
        status=status,
        overall_score=score,
        stage=stage,
        scene_match_score=_clamp01(data.get("scene_match_score"), 0.0),
        character_consistency_score=_clamp01(data.get("character_consistency_score"), 0.0),
        food_accuracy_score=_clamp01(data.get("food_accuracy_score"), 0.0),
        artifact_risk_score=_clamp01(data.get("artifact_risk_score"), 1.0),
        style_consistency_score=_clamp01(data.get("style_consistency_score"), 0.0),
        speech_completion_score=(
            None if data.get("speech_completion_score") is None else _clamp01(data.get("speech_completion_score"), 0.0)
        ),
        critical_issues=critical,
        warnings=warnings,
        positive_notes=positive_notes,
        recommended_action=recommended_action,
        raw_model_response=raw_model_response,
        model_name=model_name,
    )


def human_review_result(
    *,
    stage: AIApprovalStage,
    reason: str,
    raw_model_response: str | None = None,
    model_name: str | None = None,
) -> AIApprovalResult:
    return AIApprovalResult(
        status=AI_APPROVAL_STATUS_HUMAN_REVIEW,
        overall_score=0.0,
        stage=stage,
        warnings=[reason],
        recommended_action="manual_review",
        raw_model_response=raw_model_response,
        model_name=model_name,
    )


def call_ai_approval_model(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    response_schema: dict[str, Any] | None = None,
    config: AIApprovalConfig | None = None,
    max_json_attempts: int = 2,
) -> dict[str, Any]:
    """Calls Gemini for AI approval and returns a parsed JSON object."""
    _ = response_schema
    cfg = config or get_ai_approval_config()
    if not cfg.enabled:
        raise RuntimeError("AI approval is disabled by AI_APPROVAL_ENABLED")
    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import (  # type: ignore
            GenerativeModel,
            HarmBlockThreshold,
            HarmCategory,
            Part,
            SafetySetting,
        )
    except Exception as e:
        raise RuntimeError("Missing dependency. Install with: pip install google-cloud-aiplatform") from e
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1").strip() or "us-central1"
    if not project:
        raise RuntimeError("Missing environment variable: GOOGLE_CLOUD_PROJECT")
    vertexai.init(project=project, location=location)
    safety_settings = [
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    ]
    last_raw = ""
    for attempt in range(1, max_json_attempts + 1):
        request_text = prompt
        if attempt > 1:
            request_text += "\n\nYour previous response was not valid JSON. Return only one valid JSON object."

        def _call() -> str:
            parts: list[Any] = [Part.from_text(request_text)]
            for image in images or []:
                parts.append(Part.from_data(data=image, mime_type=_guess_media_mime_type(image)))
            model = GenerativeModel(cfg.model)
            resp = model.generate_content(parts, safety_settings=safety_settings)
            text = getattr(resp, "text", None)
            if isinstance(text, str) and text.strip():
                return text.strip()
            candidates = getattr(resp, "candidates", None) or []
            chunks: list[str] = []
            for candidate in candidates[:1]:
                content = getattr(candidate, "content", None)
                for part in getattr(content, "parts", None) or []:
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t.strip():
                        chunks.append(t)
            if chunks:
                return "\n".join(chunks).strip()
            raise RuntimeError("AI approval Gemini returned empty response.")

        last_raw = safe_llm_call(_call)
        logger.info(
            "ai_approval.model_response model=%s attempt=%s chars=%s raw=%s",
            cfg.model,
            attempt,
            len(last_raw),
            last_raw[:4000],
        )
        try:
            obj = extract_json_object(last_raw)
        except Exception:
            logger.warning("ai_approval.invalid_json attempt=%s raw=%s", attempt, last_raw[:4000])
            continue
        obj["_raw_model_response"] = last_raw
        return obj
    raise ValueError(f"AI approval model did not return valid JSON. Last response: {last_raw[:1200]}")


def _guess_media_mime_type(data: bytes) -> str:
    if data.startswith(b"\x00\x00\x00") and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _resolve_ffmpeg() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.exception("ai_approval.imageio_ffmpeg_unavailable")
        return None


def _video_bytes_for_ai_input(
    *,
    video_bytes: bytes | None = None,
    video_path: str | os.PathLike[str] | None = None,
) -> bytes | None:
    if video_bytes:
        return video_bytes
    if video_path is None:
        return None
    try:
        data = Path(video_path).read_bytes()
    except Exception:
        logger.exception("ai_approval.video_read_for_model_failed")
        return None
    return data or None


def _is_local_video_analysis_issue(text: str) -> bool:
    low = str(text or "").strip().lower()
    return (
        "speech transcription is not configured" in low
        or "cannot evaluate speech completion" in low
        or "unable to extract video frames" in low
        or "could not extract video frames" in low
        or "не удалось извлечь кадры видео" in low
    )


def _downgrade_local_video_analysis_issues(data: dict[str, Any]) -> None:
    critical = _string_list(data.get("critical_issues"))
    if not critical:
        return
    local_issues = [x for x in critical if _is_local_video_analysis_issue(x)]
    if not local_issues:
        return
    data["critical_issues"] = [x for x in critical if x not in local_issues]
    logger.info("ai_approval.ignored_local_video_analysis_issues issues=%s", local_issues)


def _has_only_local_video_analysis_critical_issues(data: dict[str, Any]) -> bool:
    critical = _string_list(data.get("critical_issues"))
    return bool(critical) and all(_is_local_video_analysis_issue(x) for x in critical)


def _json_schema_text(stage: str, statuses: str) -> str:
    return (
        "{\n"
        f'  "status": "{statuses}",\n'
        '  "overall_score": 0.0,\n'
        f'  "stage": "{stage}",\n'
        '  "scene_match_score": 0.0,\n'
        '  "character_consistency_score": 0.0,\n'
        '  "food_accuracy_score": 0.0,\n'
        '  "artifact_risk_score": 0.0,\n'
        '  "style_consistency_score": 0.0,\n'
        '  "speech_completion_score": null,\n'
        '  "critical_issues": [],\n'
        '  "warnings": [],\n'
        '  "positive_notes": [],\n'
        '  "recommended_action": ""\n'
        "}"
    )


def check_scene_prompts(
    *,
    start_frame_prompt: str,
    video_prompt: str,
    end_frame_prompt: str | None = None,
    scene_description: str = "",
    recipe_text: str = "",
    scene_number: int | str = "",
    expected_characters: list[str] | None = None,
    config: AIApprovalConfig | None = None,
) -> AIApprovalResult:
    cfg = config or get_ai_approval_config()
    if not cfg.enabled:
        return human_review_result(stage="prompt", reason="AI approval is disabled", model_name=cfg.model)
    try:
        expected_keys = [str(x).strip().lower() for x in (expected_characters or []) if str(x).strip()]
        character_descriptions = _character_descriptions_for_approval(expected_keys or None)
        expected_character_labels = format_character_short_descriptions(expected_keys) if expected_keys else ""
        prompt = (
            "You are a strict QA agent for prompts before generating Grandma Kitchen cartoon media.\n\n"
            "Evaluate the START FRAME PROMPT, optional END FRAME PROMPT, and VIDEO PROMPT together.\n\n"
            "Hard prompt requirements:\n"
            "- If grandma is expected or mentioned, the prompts must clearly preserve her face, kind expression, "
            "normal cartoon proportions, and canonical outfit from CHARACTER DESCRIPTIONS.\n"
            "- If a cat is expected or mentioned, the prompts must name/preserve the correct cat identity and coat color. "
            "Do not approve if the orange cat becomes gray/brown/black, the white cat gains dark spots/stripes, or the black-and-white cat loses the "
            "black-and-white pattern described in CHARACTER DESCRIPTIONS.\n"
            "- Any expected character must keep normal relative scale: no giant cat, no tiny grandma, no character "
            "that becomes toy-sized or oversized unless explicitly intended.\n"
            "- If VIDEO PROMPT mentions tableware, tools, cookware, props, or ingredients, the START FRAME PROMPT must "
            "also include those visible items so the video has a coherent first frame.\n"
            "- For SCENE NUMBER 1, the VIDEO PROMPT must make the spoken line name the dish explicitly in the "
            "`CHARACTERS SPEAKS:` phrase. Check the actual spoken words, not only START FRAME, action, "
            "scene description, recipe text, or dish visuals. The viewer must hear what dish is being cooked "
            "in the first scene.\n"
            "- The end frame prompt, when present, must preserve the same characters, identities, food, props, and scale "
            "while changing only the intended final state/action.\n"
            "- Prompts must forbid visible text, captions, subtitles, watermarks, logos, and photorealism.\n\n"
            "Critical issues:\n"
            "- missing grandma face when grandma is expected\n"
            "- missing required cat when a cat is expected\n"
            "- wrong cat color or wrong cat identity versus CHARACTER DESCRIPTIONS\n"
            "- scene 1 VIDEO PROMPT speech does not explicitly say the dish name after CHARACTERS SPEAKS\n"
            "- video prompt uses ingredients/tableware/props absent from the start frame prompt\n"
            "- start/end frame prompts do not preserve character identity from references\n"
            "- abnormal character scale such as a giant cat or tiny grandma\n"
            "- prompts allow text, captions, watermarks, or photorealism\n\n"
            "Scoring rule: if you mention any significant deviation, mismatch, missing required character, wrong cat "
            "identity/color, or extra character, you must put it in critical_issues, set status to FIX_PROMPT, and keep "
            "overall_score below the approval threshold. Never return APPROVE for a negative finding.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('prompt', 'APPROVE | FIX_PROMPT | HUMAN_REVIEW')}\n\n"
            f"RECIPE:\n{recipe_text or '-'}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"SCENE DESCRIPTION:\n{sanitize_character_names_and_keys(scene_description) or '-'}\n"
            f"EXPECTED CHARACTERS: {expected_character_labels or '-'}\n"
            f"CHARACTER DESCRIPTIONS:\n{json.dumps(character_descriptions, ensure_ascii=False, indent=2)}\n\n"
            f"START FRAME PROMPT:\n{sanitize_character_names_and_keys(start_frame_prompt) or '-'}\n\n"
            f"END FRAME PROMPT:\n{sanitize_character_names_and_keys(end_frame_prompt or '') or '-'}\n\n"
            f"VIDEO PROMPT:\n{sanitize_character_names_and_keys(video_prompt) or '-'}\n"
        )
        data = call_ai_approval_model(prompt, images=None, config=cfg)
        return normalize_ai_approval_result(
            data,
            stage="prompt",
            raw_model_response=data.get("_raw_model_response"),
            model_name=cfg.model,
            config=cfg,
        )
    except Exception as e:
        logger.exception("ai_approval.prompt_check_failed")
        return human_review_result(stage="prompt", reason=f"ИИ апрув не смог проверить промпты: {e}", model_name=cfg.model)


def check_start_frame(
    *,
    image_bytes: bytes | None = None,
    image_path: str | os.PathLike[str] | None = None,
    scene_prompt: str,
    scene_description: str,
    recipe_text: str,
    scene_number: int | str,
    previous_reference_images: list[bytes] | None = None,
    expected_characters: list[str] | None = None,
    expected_objects: list[str] | None = None,
    style_requirements: str | None = None,
    frame_kind: Literal["start", "end"] = "start",
    config: AIApprovalConfig | None = None,
) -> AIApprovalResult:
    cfg = config or get_ai_approval_config()
    if not cfg.enabled:
        return human_review_result(stage="image", reason="AI approval is disabled", model_name=cfg.model)
    try:
        if image_bytes is None and image_path is not None:
            image_bytes = Path(image_path).read_bytes()
        if not image_bytes:
            return human_review_result(stage="image", reason="start frame file is empty or missing", model_name=cfg.model)
        expected_keys = [str(x).strip().lower() for x in (expected_characters or []) if str(x).strip()]
        character_descriptions = _character_descriptions_for_approval(expected_keys or None)
        expected_character_labels = format_character_short_descriptions(expected_keys) if expected_keys else ""
        prompt = (
            "You are a strict QA agent for AI-generated cartoon cooking videos.\n\n"
            f"Your task is to compare the expected scene prompt with the provided {frame_kind} frame image.\n\n"
            "The video style must be:\n"
            "- colorful cartoon style\n"
            "- soft stylized shading\n"
            "- non realistic rendering\n"
            "- no photorealism\n"
            "- warm cozy lighting\n"
            "- cozy grandmother cooking atmosphere\n\n"
            "Evaluate:\n"
            "1. Scene match\n"
            "2. Character consistency\n"
            "3. Food and recipe accuracy\n"
            "4. Visual artifacts\n"
            "5. Cartoon style consistency\n"
            "6. Absence of subtitles, captions, typography, watermarks, and any visible text\n"
            "7. Whether the frame is suitable as a start frame for a short cooking video\n\n"
            "Viral retention checks:\n"
            "- the first frame is immediately understandable\n"
            "- the first scene is not boring preparation if a result/payoff frame is expected\n"
            "- food looks appetizing and readable\n"
            "- cozy grandma/cat style is preserved\n"
            "- grandmother looks kind and friendly\n"
            "- cat looks cute, not scary\n"
            "- grandma and every cat/character match their reference identity and CHARACTER DESCRIPTIONS\n"
            "- character scale is normal: no giant cat, no tiny grandma, no toy-sized people unless explicitly intended\n"
            "- no text covers faces or food\n\n"
            "Be strict. Do not approve if there are visible deformations, wrong characters, wrong food, "
            "text on screen, photorealism, or major mismatch with the prompt.\n\n"
            "Critical issues:\n"
            "- visible subtitles or captions\n"
            "- any on-screen text\n"
            "- watermark\n"
            "- photorealistic style\n"
            "- missing grandmother when required\n"
            "- wrong main dish\n"
            "- strongly distorted face\n"
            "- strongly distorted hands\n"
            "- missing required cat\n"
            "- wrong cat coat color or wrong cat identity versus CHARACTER DESCRIPTIONS\n"
            "- character style changed heavily\n"
            "- any prompt-mentioned character does not match its description in CHARACTER DESCRIPTIONS\n"
            "- abnormal character scale such as a giant cat, tiny grandma, or oversized secondary character\n"
            "- image does not match the scene\n\n"
            "Scoring rule: if you mention any significant deviation, mismatch, missing required character, wrong cat "
            "identity/color, extra character, or abnormal scale, you must put it in critical_issues, set status to "
            "REGENERATE_IMAGE, and keep overall_score below the approval threshold. Never return APPROVE for a negative "
            "finding.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('image', 'APPROVE | REGENERATE_IMAGE | HUMAN_REVIEW')}\n\n"
            f"RECIPE:\n{recipe_text or '-'}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"SCENE DESCRIPTION:\n{sanitize_character_names_and_keys(scene_description) or '-'}\n\n"
            f"SCENE PROMPT:\n{sanitize_character_names_and_keys(scene_prompt) or '-'}\n"
            f"EXPECTED CHARACTERS: {expected_character_labels or '-'}\n"
            f"CHARACTER DESCRIPTIONS:\n{json.dumps(character_descriptions, ensure_ascii=False, indent=2)}\n"
            f"EXPECTED OBJECTS: {', '.join(expected_objects or []) or '-'}\n"
            f"STYLE REQUIREMENTS:\n{style_requirements or '-'}\n"
        )
        images = [image_bytes]
        images.extend(previous_reference_images or [])
        data = call_ai_approval_model(prompt, images=images, config=cfg)
        return normalize_ai_approval_result(
            data,
            stage="image",
            raw_model_response=data.get("_raw_model_response"),
            model_name=cfg.model,
            config=cfg,
        )
    except Exception as e:
        logger.exception("ai_approval.image_check_failed")
        return human_review_result(stage="image", reason=f"ИИ апрув не смог проверить стартовый фрейм: {e}", model_name=cfg.model)


def _video_duration_seconds(video_path: str | os.PathLike[str]) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
    else:
        ffmpeg = _resolve_ffmpeg()
        if not ffmpeg:
            return None
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(video_path),
            "-f",
            "null",
            "-",
        ]
        duration_re = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
            match = duration_re.search(proc.stderr or proc.stdout or "")
            if not match:
                return None
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except Exception:
            logger.exception("ai_approval.video_duration_probe_failed")
            return None
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(proc.stdout.strip())
    except Exception:
        logger.exception("ai_approval.video_duration_probe_failed")
        return None


def extract_video_frames(
    *,
    video_bytes: bytes | None = None,
    video_path: str | os.PathLike[str] | None = None,
    frame_count: int = 5,
) -> list[bytes]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    with tempfile.TemporaryDirectory(prefix="ai_approval_video_") as tmp:
        tmp_path = Path(tmp)
        source = Path(video_path) if video_path is not None else tmp_path / "source.mp4"
        if video_path is None:
            if not video_bytes:
                raise RuntimeError("video bytes are empty")
            source.write_bytes(video_bytes)
        if not source.exists() or source.stat().st_size <= 0:
            raise RuntimeError("video file is empty or missing")
        duration = _video_duration_seconds(source) or 0.0
        if duration > 0:
            points = [duration * (i + 1) / (frame_count + 1) for i in range(frame_count)]
        else:
            points = [0.0]
        frames: list[bytes] = []
        for i, ts in enumerate(points):
            out = tmp_path / f"frame_{i:02d}.png"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{ts:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(720,iw)':-2",
                    str(out),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            if out.exists() and out.stat().st_size > 0:
                frames.append(out.read_bytes())
        if not frames:
            raise RuntimeError("ffmpeg did not extract any frames")
        return frames


def transcribe_video_audio(
    *,
    video_bytes: bytes | None = None,
    video_path: str | os.PathLike[str] | None = None,
    expected_speech_text: str | None = None,
    duration: int | float | None = None,
    config: AIApprovalConfig | None = None,
) -> SpeechTranscriptionResult:
    """Transcribe and compare video speech through the configured multimodal approval model."""
    cfg = config or get_ai_approval_config()
    if not cfg.audio_transcription_enabled:
        return SpeechTranscriptionResult(
            status=AI_APPROVAL_STATUS_HUMAN_REVIEW,
            speech_speed="unknown",
            recognized_text="",
        )
    video_input = _video_bytes_for_ai_input(video_bytes=video_bytes, video_path=video_path)
    if not video_input:
        return SpeechTranscriptionResult(
            status=AI_APPROVAL_STATUS_HUMAN_REVIEW,
            speech_speed="unknown",
            recognized_text="",
        )
    prompt = (
        "You are a speech recognition QA agent for short AI-generated cooking videos.\n\n"
        "Listen to the attached MP4 and transcribe the spoken speech. Ignore background noise and do not "
        "describe visuals. If no intelligible speech is present, return an empty recognized_text.\n\n"
        "Compare the recognized speech to EXPECTED SPEECH. Treat [пауза] as a pause marker. The recognized_text should be "
        "plain text without stress capitalization unless the audio clearly spells letters.\n\n"
        "Evaluate:\n"
        "1. Whether the expected phrase is completed before the video ends.\n"
        "2. Approximate semantic/word match score from 0 to 1.\n"
        "3. Missing and extra words.\n"
        "4. Whether speech cuts off mid-word or mid-phrase.\n"
        "5. Whether pauses are too long for a short cooking clip.\n"
        "6. Speech speed: slow, normal, fast, too_fast, or unknown.\n"
        "7. Whether pronunciation sounds natural for the language of EXPECTED SPEECH. For Russian, Russian word stress should sound natural.\n"
        "8. Whether the line appears to overflow the clip timing.\n"
        "9. Whether the line sounds unfinished at the end.\n\n"
        "Return only valid JSON:\n"
        "{\n"
        '  "phrase_completed": false,\n'
        '  "match_score": 0.0,\n'
        '  "missing_words": [],\n'
        '  "extra_words": [],\n'
        '  "cut_off_detected": false,\n'
        '  "long_pauses_detected": false,\n'
        '  "speech_speed": "unknown",\n'
        '  "recognized_text": "",\n'
        '  "phrase_overflow_detected": false,\n'
        '  "unfinished_line_detected": false,\n'
        '  "status": "HUMAN_REVIEW"\n'
        "}\n\n"
        f"EXPECTED SPEECH:\n{expected_speech_text or '-'}\n"
        f"EXPECTED DURATION: {duration if duration is not None else '-'}\n"
    )
    try:
        data = call_ai_approval_model(prompt, images=[video_input], config=cfg)
    except Exception:
        logger.exception("ai_approval.video_audio_transcription_failed")
        return SpeechTranscriptionResult(
            status=AI_APPROVAL_STATUS_HUMAN_REVIEW,
            speech_speed="unknown",
            recognized_text="",
        )
    phrase_completed = _bool_value(data.get("phrase_completed"), False)
    match_score = _clamp01(data.get("match_score"), 0.0)
    cut_off = _bool_value(data.get("cut_off_detected"), False)
    long_pauses = _bool_value(data.get("long_pauses_detected"), False)
    phrase_overflow = _bool_value(data.get("phrase_overflow_detected"), False)
    unfinished_line = _bool_value(data.get("unfinished_line_detected"), False)
    status = str(data.get("status") or "").strip().upper()
    if status not in ALLOWED_STATUSES:
        status = AI_APPROVAL_STATUS_HUMAN_REVIEW
    if phrase_completed and match_score >= 0.85 and not cut_off and not phrase_overflow and not unfinished_line:
        status = AI_APPROVAL_STATUS_APPROVE
    elif cut_off or phrase_overflow or unfinished_line or match_score < 0.70:
        status = AI_APPROVAL_STATUS_REGENERATE_VIDEO
    else:
        status = AI_APPROVAL_STATUS_HUMAN_REVIEW
    return SpeechTranscriptionResult(
        phrase_completed=phrase_completed,
        match_score=match_score,
        missing_words=_string_list(data.get("missing_words")),
        extra_words=_string_list(data.get("extra_words")),
        cut_off_detected=cut_off,
        long_pauses_detected=long_pauses,
        speech_speed=str(data.get("speech_speed") or "unknown").strip() or "unknown",
        recognized_text=str(data.get("recognized_text") or "").strip(),
        phrase_overflow_detected=phrase_overflow,
        unfinished_line_detected=unfinished_line,
        status=status,
    )


def check_video(
    *,
    video_bytes: bytes | None = None,
    video_path: str | os.PathLike[str] | None = None,
    scene_prompt: str,
    expected_speech_text: str | None = None,
    duration: int | float | None = None,
    recipe_text: str = "",
    scene_number: int | str = "",
    previous_reference_images: list[bytes] | None = None,
    generated_start_frame: bytes | None = None,
    config: AIApprovalConfig | None = None,
) -> AIApprovalResult:
    cfg = config or get_ai_approval_config()
    if not cfg.enabled:
        return human_review_result(stage="video", reason="AI approval is disabled", model_name=cfg.model)
    try:
        if video_path is None and not video_bytes:
            return human_review_result(stage="video", reason="video file is empty or missing", model_name=cfg.model)
        warnings: list[str] = []
        frames: list[bytes] = []
        attached_original_video = False
        if cfg.extract_video_frames:
            try:
                frames = extract_video_frames(video_bytes=video_bytes, video_path=video_path, frame_count=cfg.frame_count)
            except Exception as e:
                logger.exception("ai_approval.video_frame_extraction_failed")
                warnings.append(f"Не удалось извлечь кадры видео: {e}")
        if generated_start_frame:
            frames.insert(0, generated_start_frame)
        if previous_reference_images:
            frames.extend(previous_reference_images)
        attached_original_video_for_speech = False
        if expected_speech_text:
            video_input = _video_bytes_for_ai_input(video_bytes=video_bytes, video_path=video_path)
            if video_input:
                frames.append(video_input)
                attached_original_video_for_speech = True
        if cfg.extract_video_frames and not frames:
            video_input = _video_bytes_for_ai_input(video_bytes=video_bytes, video_path=video_path)
            if video_input:
                frames.append(video_input)
                attached_original_video = True
                warnings.append("video frames unavailable; sending the original video to AI approval")
        if cfg.extract_video_frames and not frames:
            return human_review_result(
                stage="video",
                reason="ИИ апрув не смог извлечь кадры видео и не смог передать видео модели для проверки.",
                model_name=cfg.model,
            )
        speech = transcribe_video_audio(
            video_bytes=video_bytes,
            video_path=video_path,
            expected_speech_text=expected_speech_text,
            duration=duration,
            config=cfg,
        )
        speech_note = (
            "Speech transcription is disabled in this environment. Do not treat missing transcription "
            "as a critical issue; evaluate visual scene quality and visible lip/mouth activity only."
            if not cfg.audio_transcription_enabled
            else "Speech transcription is enabled; use SPEECH QA RESULT."
        )
        media_note = (
            "The attached media includes the original MP4 video because local frame extraction is unavailable. "
            "This is a valid analysis input. Do not fail the scene merely because local frame extraction failed."
            if attached_original_video
            else "The attached media contains sampled video frames plus the original MP4 for speech/pronunciation review."
            if attached_original_video_for_speech
            else "The attached media contains sampled video frames and optional reference/start frames."
        )
        prompt = (
            "You are reviewing sampled frames from an AI-generated short cooking video.\n\n"
            "Compare the frames against:\n"
            "- the scene prompt\n"
            "- the expected recipe step\n"
            "- the previous character references if provided\n"
            "- the visual style requirements\n\n"
            "Important rules:\n"
            "- No subtitles\n"
            "- No captions\n"
            "- No visible text\n"
            "- No typography\n"
            "- No watermarks\n"
            "- No photorealism\n"
            "- Grandma must stay consistent\n"
            "- Cat must stay consistent if present\n"
            "- Grandma, cat, and any other recurring characters must match the provided reference/start images and canonical identities\n"
            "- Character scale must remain normal: no giant cat, tiny grandma, or accidental oversized/undersized character\n"
            "- Food must match the recipe step\n"
            "- Major hand, face, mouth, or object deformation is a critical issue\n"
            "- If the scene includes lipsync, grandmother's mouth should appear naturally active during speech frames\n"
            "- If the original video/audio is attached and speech is present, pronunciation must sound natural for the spoken language. "
            "Wrong stress, robotic intonation, swallowed syllables, clipped endings, or unnatural emphasis are content defects.\n"
            "- The action should be simple and match the prompt\n"
            "- The video should be suitable for a cozy short-form cooking video\n\n"
            "Viral retention checks:\n"
            "- strong hook or clear recipe progress is visible\n"
            "- the scene gives a reason to continue watching\n"
            "- no long empty action without movement, texture change, emotion, or visual progress\n"
            "- final/payoff scenes show a clear visual payoff such as cut, spoon, crunch, cheese pull, plating, grandma smile, or cat reaction\n"
            "- cozy cartoon food-blog tone is preserved\n"
            "- grandma looks friendly, cat looks cute, food looks appetizing\n"
            "- lip sync matches the expected phrase closely enough for the clip duration\n"
            "- spoken words have natural accent/stress and do not sound mispronounced\n"
            "- the spoken phrase appears feasible within the expected duration\n\n"
            "Technical limitations are not content defects:\n"
            "- If speech transcription is disabled, do not mark it as a critical issue.\n"
            "- If the original MP4 is not attached, do not invent pronunciation defects; mention pronunciation as uncertain in warnings.\n"
            "- If local frame extraction failed but an MP4 video or reference/start frame is attached, review the attached media.\n\n"
            "Scoring rule: if you mention any significant deviation, mismatch, missing required character, wrong cat "
            "identity/color, extra character, abnormal scale, or unnatural word stress/pronunciation, you must put it "
            "in critical_issues, set status to REGENERATE_VIDEO, and keep overall_score below the approval threshold. "
            "Never return APPROVE for a negative finding.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('video', 'APPROVE | REGENERATE_VIDEO | HUMAN_REVIEW')}\n\n"
            f"MEDIA INPUT NOTE:\n{media_note}\n"
            f"SPEECH INPUT NOTE:\n{speech_note}\n\n"
            f"RECIPE:\n{recipe_text or '-'}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"SCENE PROMPT:\n{sanitize_character_names_and_keys(scene_prompt) or '-'}\n"
            f"EXPECTED SPEECH TEXT:\n{expected_speech_text or '-'}\n"
            f"EXPECTED DURATION: {duration if duration is not None else '-'}\n"
            f"SPEECH QA RESULT:\n{json.dumps(speech.to_dict(), ensure_ascii=False)}\n"
            f"LOCAL WARNINGS:\n{json.dumps(warnings, ensure_ascii=False)}\n"
        )
        data = call_ai_approval_model(prompt, images=frames, config=cfg)
        if _has_only_local_video_analysis_critical_issues(data):
            retry_prompt = (
                prompt
                + "\n\nThe previous evaluation treated local QA limitations as scene defects. "
                "Re-evaluate the attached media now. Do not include missing speech transcription or local frame "
                "extraction failure in critical_issues, because they are tooling limitations, not video defects."
            )
            data = call_ai_approval_model(retry_prompt, images=frames, config=cfg)
        _downgrade_local_video_analysis_issues(data)
        data.setdefault("warnings", [])
        if isinstance(data["warnings"], list):
            data["warnings"].extend(warnings)
        if cfg.audio_transcription_enabled:
            data["speech_completion_score"] = speech.match_score
            if speech.cut_off_detected or speech.phrase_overflow_detected or speech.unfinished_line_detected or speech.speech_speed == "too_fast" or speech.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO:
                data.setdefault("critical_issues", [])
                if isinstance(data["critical_issues"], list):
                    issues: list[str] = []
                    if speech.cut_off_detected:
                        issues.append("audio cut off detected")
                    if speech.phrase_overflow_detected:
                        issues.append("phrase overflow detected")
                    if speech.unfinished_line_detected:
                        issues.append("unfinished line detected")
                    if speech.speech_speed == "too_fast":
                        issues.append("speech delivered too fast")
                    if speech.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO and not issues:
                        issues.append("speech transcription does not match expected phrase")
                    data["critical_issues"].extend(issues)
        return normalize_ai_approval_result(
            data,
            stage="video",
            raw_model_response=data.get("_raw_model_response"),
            model_name=cfg.model,
            config=cfg,
        )
    except Exception as e:
        logger.exception("ai_approval.video_check_failed")
        return human_review_result(stage="video", reason=f"ИИ апрув не смог проверить видео: {e}", model_name=cfg.model)


def make_final_ai_approval_decision(results: list[AIApprovalResult]) -> AIApprovalResult:
    if not results:
        return human_review_result(stage="final", reason="no AI approval checks were provided")
    critical: list[str] = []
    warnings: list[str] = []
    positives: list[str] = []
    for result in results:
        critical.extend(result.critical_issues)
        warnings.extend(result.warnings)
        positives.extend(result.positive_notes)
        if result.status != AI_APPROVAL_STATUS_APPROVE:
            return AIApprovalResult(
                status=result.status,
                overall_score=result.overall_score,
                stage="final",
                scene_match_score=result.scene_match_score,
                character_consistency_score=result.character_consistency_score,
                food_accuracy_score=result.food_accuracy_score,
                artifact_risk_score=result.artifact_risk_score,
                style_consistency_score=result.style_consistency_score,
                speech_completion_score=result.speech_completion_score,
                critical_issues=critical,
                warnings=warnings,
                positive_notes=positives,
                recommended_action=result.recommended_action,
                raw_model_response=result.raw_model_response,
                model_name=result.model_name,
            )
    avg = sum(r.overall_score for r in results) / len(results)
    return AIApprovalResult(
        status=AI_APPROVAL_STATUS_APPROVE,
        overall_score=avg,
        stage="final",
        scene_match_score=sum(r.scene_match_score for r in results) / len(results),
        character_consistency_score=sum(r.character_consistency_score for r in results) / len(results),
        food_accuracy_score=sum(r.food_accuracy_score for r in results) / len(results),
        artifact_risk_score=max(r.artifact_risk_score for r in results),
        style_consistency_score=sum(r.style_consistency_score for r in results) / len(results),
        speech_completion_score=None,
        critical_issues=critical,
        warnings=warnings,
        positive_notes=positives,
        recommended_action="continue",
        model_name=results[-1].model_name,
    )


def ai_approval_history_entry(
    result: AIApprovalResult,
    *,
    attempt: int,
) -> dict[str, Any]:
    return {
        "stage": result.stage,
        "attempt": attempt,
        "status": result.status,
        "overall_score": result.overall_score,
        "critical_issues": list(result.critical_issues),
        "warnings": list(result.warnings),
        "recommended_action": result.recommended_action,
        "raw_model_response": result.raw_model_response,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_name": result.model_name,
    }


def format_ai_approval_summary(result: AIApprovalResult) -> str:
    reasons = result.critical_issues or result.warnings or result.positive_notes
    reason_text = ", ".join(reasons[:3]) if reasons else (result.recommended_action or "без дополнительных замечаний")
    if result.status == AI_APPROVAL_STATUS_HUMAN_REVIEW:
        return (
            "🤖 ИИ апрув отправил сцену на ручную проверку.\n"
            f"Оценка: {result.overall_score:.2f}\n"
            f"Причина: {reason_text}."
        )
    return (
        f"🤖 ИИ апрув: {result.status}\n"
        f"Оценка: {result.overall_score:.2f}\n"
        f"Причины: {reason_text}."
    )


def encode_video_bytes_for_debug(video_bytes: bytes, *, max_bytes: int = 256) -> str:
    return base64.b64encode(video_bytes[:max_bytes]).decode("ascii")
