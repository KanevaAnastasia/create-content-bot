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
from .llm_utils import (
    GeminiQuotaError,
    _dedupe_binary_payloads,
    _extract_text_from_vertex_response,
    _summarize_empty_vertex_response,
    extract_json_object,
    get_vertex_generative_model,
    get_vertex_safety_settings,
    safe_llm_call,
    vertex_part_from_data,
    vertex_part_from_text,
)
from .llm_queue import LLMCallTimeoutError

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
AI_APPROVAL_MODEL_FALLBACKS_ENV_NAME = "AI_APPROVAL_MODEL_FALLBACKS"
AI_APPROVAL_STOP_ON_TIMEOUT_ENV_NAME = "AI_APPROVAL_STOP_ON_TIMEOUT"

RUSSIAN_PRONUNCIATION_QA_GUIDE = (
    "Russian pronunciation/stress traps to check carefully when they appear in EXPECTED SPEECH:\n"
    "- тают / тает: stress is тАют / тАет, never таЮт / таЕт.\n"
    "- растает / растают: stress is растАет / растАют.\n"
    "- свёкла must sound with ё, not свекла.\n"
    "- кухонный: кУхонный.\n"
    "- сливовый: слИвовый.\n"
    "If audio has a wrong stress even when the recognized text matches, report it in wrong_stress_words and set status to REGENERATE_VIDEO."
)


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
    speech_recognized_text: str = ""
    speech_missing_words: list[str] = field(default_factory=list)
    speech_extra_words: list[str] = field(default_factory=list)
    speech_wrong_stress_words: list[str] = field(default_factory=list)
    critical_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    positive_notes: list[str] = field(default_factory=list)
    recommended_action: str = ""
    raw_model_response: str | None = None
    model_name: str | None = None
    prompt_variant: int | None = None

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
    pronunciation_issues: list[str] = field(default_factory=list)
    wrong_stress_words: list[str] = field(default_factory=list)
    rising_intonation_detected: bool = False
    intonation_issues: list[str] = field(default_factory=list)
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


def _safe_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _ai_approval_model_ids(primary_model: str) -> list[str]:
    primary = str(primary_model or "").strip() or "gemini-2.5-flash-lite"
    raw = (os.getenv(AI_APPROVAL_MODEL_FALLBACKS_ENV_NAME) or "").strip()
    if raw:
        fallbacks = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        fallbacks = ["gemini-2.5-flash", "gemini-2.5-pro"]
    out: list[str] = []
    for model in [primary, *fallbacks]:
        if model and model not in out:
            out.append(model)
    return out


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
    "extra cat",
    "same cat appears twice",
    "same cat appears two times",
    "same cat is duplicated",
    "same-looking cat",
    "same looking cat",
    "identical cats",
    "duplicate cat",
    "duplicated cat",
    "duplicate cats",
    "duplicated cats",
    "clone cat",
    "cloned cat",
    "cat clone",
    "cat clones",
    "two identical cats",
    "multiple identical cats",
    "same identity cat",
    "same-identity cat",
    "wrong art style",
    "style mismatch",
    "different style",
    "changed illustration style",
    "does not match reference style",
    "not match reference style",
    "storybook style",
    "children's book style",
    "childrens book style",
    "disney-like",
    "pixar-like",
    "anime style",
    "watercolor style",
    "oil painting style",
    "generic cute cartoon",
    "different-looking character",
    "different-looking grandma",
    "wrong color",
    "wrong coat",
    "wrong food",
    "two orange cats",
    "two white cats",
    "two black-and-white cats",
    "two black and white cats",
    "duplicated orange cats",
    "duplicated white cats",
    "duplicated black-and-white cats",
    "duplicate orange cat",
    "duplicate white cat",
    "duplicate black-and-white cat",
    "same cat identity",
    "same coat identity",
    "wrong pronunciation",
    "mispronounced",
    "mispronunciation",
    "incorrect pronunciation",
    "unnatural pronunciation",
    "pronunciation issue",
    "wrong stress",
    "incorrect stress",
    "misstressed",
    "wrong word stress",
    "stress error",
    "rising intonation",
    "question-like intonation",
    "missing spoken words",
    "extra spoken words",
    "speech does not match",
    "speech mismatch",
    "clipped speech",
    "cut off speech",
    "cut-off speech",
    "swallowed syllables",
    "swallowed endings",
    "wrong main dish",
    "wrong dish",
    "unrelated dish",
    "generic substitute",
    "dish visual mismatch",
    "canonical dish mismatch",
    "wrong ingredient geometry",
    "wrong ingredient structure",
    "wrong food structure",
    "wrong food texture",
    "wrong serving form",
    "wrong dish structure",
    "wrong dish texture",
    "incorrect ingredient form",
    "incorrect serving form",
    "cropped food",
    "food cropped",
    "dish cropped",
    "cropped dish",
    "severely cropped",
    "cut off dish",
    "cut-off dish",
    "cut off food",
    "cut-off food",
    "missing required",
    "missing grandmother",
    "missing grandma",
    "missing grandma face",
    "missing grandmother face",
    "grandma's face is not visible",
    "grandmother's face is not visible",
    "grandma face is not visible",
    "grandmother face is not visible",
    "grandma face not visible",
    "grandmother face not visible",
    "grandma's face not visible",
    "grandmother's face not visible",
    "no visible grandma face",
    "no visible grandmother face",
    "grandma has no visible face",
    "grandmother has no visible face",
    "only grandma's hand",
    "only grandma's hands",
    "only grandmother's hand",
    "only grandmother's hands",
    "only grandma's arm",
    "only grandmother's arm",
    "only the grandma's hand",
    "only the grandmother's hand",
    "only the grandma's hands",
    "only the grandmother's hands",
    "grandma's hand is visible but her face is not",
    "grandmother's hand is visible but her face is not",
    "cropped grandma face",
    "grandma face cropped",
    "cropped grandmother face",
    "grandmother face cropped",
    "cropped head",
    "head cropped",
    "face cut off",
    "cut off face",
    "head cut off",
    "cut off head",
    "face is cropped",
    "head is cropped",
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
    "два рыжих кот",
    "две рыжие кош",
    "два белых кот",
    "две белые кош",
    "два черно-белых кот",
    "два чёрно-белых кот",
    "две черно-белые кош",
    "две чёрно-белые кош",
    "дубликат кот",
    "дубли кот",
    "повтор того же кот",
    "одинаковые кот",
    "одинаковые кош",
    "тот же кот",
    "та же кош",
    "клон кот",
    "клон кош",
    "склонированный кот",
    "неверное произношение",
    "неправильное произношение",
    "искаженное произношение",
    "искажённое произношение",
    "неверное ударение",
    "неправильное ударение",
    "ударение не туда",
    "лишние слова",
    "пропущенные слова",
    "обрыв речи",
    "речь обрывается",
    "вопросительная интонация",
    "восходящая интонация",
    "нет бабушки",
    "нет лица бабушки",
    "лицо бабушки не видно",
    "не видно лица бабушки",
    "лица бабушки не видно",
    "бабушкино лицо не видно",
    "видна только рука бабушки",
    "видны только руки бабушки",
    "видна лишь рука бабушки",
    "видны лишь руки бабушки",
    "видна только часть бабушки",
    "видно только тело бабушки",
    "нет кота",
    "не видно бабушку",
    "не видно кота",
    "не видны руки",
    "гигантский кот",
    "крошечная бабушка",
    "неправильный цвет",
    "неправильный окрас",
    "не тот стиль",
    "другой стиль",
    "не совпадает со стилем",
    "не совпадает с референс",
    "стиль сказочной",
    "книжная иллюстрация",
    "диснеевский",
    "пиксаровский",
    "аниме",
    "акварельный стиль",
    "другая бабушка",
    "другое лицо",
    "субтитры",
    "водяной знак",
    "фотореализм",
    "не то блюдо",
    "другое блюдо",
    "похожее блюдо",
    "общее блюдо",
    "блюдо обрезано",
    "еда обрезана",
    "сильно обрезан",
    "сильно обрезана",
    "обрезан кадром",
    "обрезана кадром",
)


def _looks_like_severe_ai_approval_reason(text: str) -> bool:
    low = " ".join(str(text or "").strip().lower().split())
    if not low:
        return False
    return any(pattern in low for pattern in SEVERE_AI_APPROVAL_REASON_PATTERNS)


GRANDMA_FACE_VISIBILITY_FAIL_PATTERNS: tuple[str, ...] = (
    "grandma's face is not visible",
    "grandmother's face is not visible",
    "grandma face is not visible",
    "grandmother face is not visible",
    "grandma face not visible",
    "grandmother face not visible",
    "grandma's face not visible",
    "grandmother's face not visible",
    "no visible grandma face",
    "no visible grandmother face",
    "grandma has no visible face",
    "grandmother has no visible face",
    "only grandma's hand",
    "only grandma's hands",
    "only grandmother's hand",
    "only grandmother's hands",
    "only grandma's arm",
    "only grandmother's arm",
    "grandma's hand is visible but her face is not",
    "grandmother's hand is visible but her face is not",
    "нет лица бабушки",
    "лицо бабушки не видно",
    "не видно лица бабушки",
    "лица бабушки не видно",
    "бабушкино лицо не видно",
    "видна только рука бабушки",
    "видны только руки бабушки",
    "видна лишь рука бабушки",
    "видны лишь руки бабушки",
    "видна только часть бабушки",
    "видно только тело бабушки",
)


def _looks_like_grandma_face_visibility_failure(text: str) -> bool:
    low = " ".join(str(text or "").strip().lower().split())
    if not low:
        return False
    return any(pattern in low for pattern in GRANDMA_FACE_VISIBILITY_FAIL_PATTERNS)


def _video_hard_fail_reasons(data: dict[str, Any]) -> list[str]:
    fields: list[Any] = [data.get("recommended_action")]
    for key in (
        "critical_issues",
        "warnings",
    ):
        value = data.get(key)
        if isinstance(value, list):
            fields.extend(value)
        elif value:
            fields.append(value)
    reasons = [str(item).strip() for item in fields if str(item).strip()]
    hard_fails = [reason for reason in reasons if _looks_like_severe_ai_approval_reason(reason)]
    speech_issue_labels = {
        "speech_missing_words": "missing spoken words",
        "speech_extra_words": "extra spoken words",
        "speech_wrong_stress_words": "wrong word stress",
    }
    for key, label in speech_issue_labels.items():
        values = _string_list(data.get(key))
        if values:
            hard_fails.append(f"{label}: {', '.join(values)}")
    return hard_fails


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
    if stage in {"image", "video"}:
        for reason in [*warnings, *positive_notes, recommended_action]:
            if _looks_like_grandma_face_visibility_failure(reason) and reason not in severe_reasons:
                severe_reasons.append(reason)
    if stage == "video":
        for reason in _video_hard_fail_reasons(data):
            if reason not in severe_reasons:
                severe_reasons.append(reason)
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
        speech_recognized_text=str(data.get("speech_recognized_text") or "").strip(),
        speech_missing_words=_string_list(data.get("speech_missing_words")),
        speech_extra_words=_string_list(data.get("speech_extra_words")),
        speech_wrong_stress_words=_string_list(data.get("speech_wrong_stress_words")),
        critical_issues=critical,
        warnings=warnings,
        positive_notes=positive_notes,
        recommended_action=recommended_action,
        raw_model_response=raw_model_response,
        model_name=model_name,
        prompt_variant=_safe_int_or_none(data.get("_prompt_variant")),
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


def _qa_safe_context_excerpt(value: str, *, max_chars: int = 1200) -> str:
    """Compact user/recipe context for safety-sensitive QA fallback prompts."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return "-"
    replacements = {
        "prohibited": "not allowed",
        "forbidden": "not allowed",
        "safety": "quality",
        "scary": "unfriendly",
        "kill": "remove",
        "blood": "red liquid",
    }
    for source, target in replacements.items():
        text = re.sub(source, target, text, flags=re.IGNORECASE)
    if len(text) > max_chars:
        return text[:max_chars].rsplit(" ", 1)[0].strip() + "..."
    return text


def _safe_llm_call_with_retry_kwargs(fn):
    try:
        return safe_llm_call(
            fn,
            max_attempts=2,
            backoff_initial_sec=2.0,
            backoff_max_sec=15.0,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return safe_llm_call(fn)


def _call_ai_approval_model_compatible(
    prompt: str,
    *,
    images: list[bytes] | None,
    config: AIApprovalConfig,
    fallback_prompts: list[str] | None = None,
) -> dict[str, Any]:
    try:
        return call_ai_approval_model(prompt, images=images, fallback_prompts=fallback_prompts, config=config)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return call_ai_approval_model(prompt, images=images, config=config)


def call_ai_approval_model(
    prompt: str,
    *,
    images: list[bytes] | None = None,
    fallback_prompts: list[str] | None = None,
    response_schema: dict[str, Any] | None = None,
    config: AIApprovalConfig | None = None,
    max_json_attempts: int = 2,
) -> dict[str, Any]:
    """Calls Gemini for AI approval and returns a parsed JSON object."""
    _ = response_schema
    cfg = config or get_ai_approval_config()
    if not cfg.enabled:
        raise RuntimeError("AI approval is disabled by AI_APPROVAL_ENABLED")
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1").strip() or "us-central1"
    if not project:
        raise RuntimeError("Missing environment variable: GOOGLE_CLOUD_PROJECT")
    safety_settings = get_vertex_safety_settings()
    media_payloads = _dedupe_binary_payloads(images)
    last_raw = ""
    attempt_notes: list[str] = []
    last_error: BaseException | None = None
    model_ids = _ai_approval_model_ids(cfg.model)
    prompt_variants = [prompt]
    for fallback_prompt in fallback_prompts or []:
        cleaned = str(fallback_prompt or "").strip()
        if cleaned and cleaned not in prompt_variants:
            prompt_variants.append(cleaned)
    for prompt_variant_idx, prompt_variant in enumerate(prompt_variants, start=1):
        for model_id in model_ids:
            for attempt in range(1, max_json_attempts + 1):
                request_text = prompt_variant
                if attempt > 1:
                    request_text += "\n\nYour previous response was not valid JSON. Return only one valid JSON object."

                def _call() -> str:
                    parts: list[Any] = [vertex_part_from_text(project, location, request_text)]
                    for image in media_payloads:
                        parts.append(vertex_part_from_data(project, location, image))
                    model = get_vertex_generative_model(project, location, model_id)
                    resp = model.generate_content(parts, safety_settings=safety_settings)
                    text = _extract_text_from_vertex_response(resp)
                    if text:
                        return text.strip()
                    details = _summarize_empty_vertex_response(resp)
                    suffix = f" ({details})" if details else ""
                    raise RuntimeError(f"AI approval Gemini returned empty response{suffix}.")

                try:
                    last_raw = _safe_llm_call_with_retry_kwargs(_call)
                except GeminiQuotaError as exc:
                    last_error = exc
                    note = (
                        f"prompt_variant={prompt_variant_idx} model={model_id} quota/rate limit: "
                        f"{str(exc).replace(chr(10), ' ')[:500]}"
                    )
                    attempt_notes.append(note)
                    logger.warning("ai_approval.model_quota_or_rate_limit %s", note)
                    break
                except Exception as exc:
                    last_error = exc
                    note = (
                        f"prompt_variant={prompt_variant_idx} model={model_id} attempt={attempt}: "
                        f"{str(exc).replace(chr(10), ' ')[:500]}"
                    )
                    attempt_notes.append(note)
                    logger.warning("ai_approval.model_call_failed %s", note)
                    if isinstance(exc, LLMCallTimeoutError) and _bool_env(AI_APPROVAL_STOP_ON_TIMEOUT_ENV_NAME, True):
                        raise RuntimeError(f"AI approval model timed out; retry the approval check later: {note}") from exc
                    break
                logger.info(
                    "ai_approval.model_response prompt_variant=%s model=%s attempt=%s chars=%s raw=%s",
                    prompt_variant_idx,
                    model_id,
                    attempt,
                    len(last_raw),
                    last_raw[:4000],
                )
                try:
                    obj = extract_json_object(last_raw)
                except Exception:
                    logger.warning(
                        "ai_approval.invalid_json prompt_variant=%s model=%s attempt=%s raw=%s",
                        prompt_variant_idx,
                        model_id,
                        attempt,
                        last_raw[:4000],
                    )
                    attempt_notes.append(f"prompt_variant={prompt_variant_idx} model={model_id} attempt={attempt}: invalid JSON")
                    continue
                obj["_raw_model_response"] = last_raw
                obj["_model_name"] = model_id
                obj["_prompt_variant"] = prompt_variant_idx
                return obj
    details = "; ".join(attempt_notes[-8:])
    if last_error is not None:
        raise RuntimeError(f"AI approval model failed across fallbacks: {details}") from last_error
    raise ValueError(f"AI approval model did not return valid JSON. Last response: {last_raw[:1200]}; {details}")


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
    dish_visual_brief: str = "",
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
            "- Before scoring, perform a culinary visual lookup from your own world knowledge: identify what the "
            "named dish should look like in real life, then compare the prompts against that canonical appearance, "
            "the recipe, and DISH VISUAL REQUIREMENTS. Put a short canonical appearance note in positive_notes or "
            "warnings so the decision shows what you compared against.\n"
            "- If grandma is expected or mentioned, the prompts must clearly preserve her face, kind expression, "
            "normal cartoon proportions, and canonical outfit from CHARACTER DESCRIPTIONS.\n"
            "- If grandma is expected or mentioned, start/end prompts must require her face to be visibly readable "
            "from the front or three-quarter view. Do not allow a back-only, cropped, hidden, tiny, faceless, "
            "or substituted grandma.\n"
            "- If a cat is expected or mentioned, the prompts must name/preserve the correct cat identity and coat color. "
            "Do not approve if the orange cat becomes gray/brown/black, the white cat gains dark spots/stripes, or the black-and-white cat loses the "
            "black-and-white pattern described in CHARACTER DESCRIPTIONS.\n"
            "- Cat identity count: prompts may include several cats only when they are different expected identities. "
            "Never allow duplicate same-identity cats: no two orange cats, no two white cats, no two black-and-white cats, "
            "no kittens/silhouettes/mirrored copies that duplicate an expected cat. If multiple cats appear, each must have "
            "a distinct expected coat/identity.\n"
            "- Every human or animal character must stay visibly cartoon/stylized, never photorealistic or live-action.\n"
            "- Any expected character must keep normal relative scale: no giant cat, no tiny grandma, no character "
            "that becomes toy-sized or oversized unless explicitly intended.\n"
            "- If a pot, saucepan, cauldron, frying pan, or skillet is boiling, simmering, frying, sauteing, steaming, "
            "or visibly cooking, it must be on a visible stovetop burner/hob.\n"
            "- Visible ingredients, toppings, sides, sauces, and garnishes must be supported by RECIPE, "
            "DISH VISUAL REQUIREMENTS, explicit user feedback, or reference images. Do not approve invented garnish "
            "or extra ingredients added only because they are common in some variants of the dish.\n"
            "- If VIDEO PROMPT mentions tableware, tools, cookware, props, or ingredients, the START FRAME PROMPT must "
            "also include those visible items so the video has a coherent first frame.\n"
            "- If the START FRAME PROMPT shows the cooked/final dish or a payoff serving, it must explicitly "
            "preserve the real-world dish appearance from DISH VISUAL REQUIREMENTS: shape, color, texture, "
            "ingredient form, and serving style. If it shows an intermediate recipe step, it must accurately "
            "show the current ingredient/process state and remain visually consistent with the dish; do not "
            "require the finished dish to appear in every preparation frame.\n"
            "- Frame prompts must keep the main food readable enough to verify the dish: do not allow severe crop "
            "of the finished dish, cut/open face, or key food object when that object is the focus of the frame.\n"
            "- Do NOT flag an intermediate preparation prompt merely because it does not show or anchor the "
            "cooked/final dish. Apply the finished-dish visual requirement only when the prompt asks for the "
            "finished dish, cooked dish, plated/served dish, or payoff frame.\n"
            "- For SCENE NUMBER 1, the VIDEO PROMPT must make the spoken line name the dish explicitly in the "
            "`CHARACTERS SPEAKS:` phrase. Check the actual spoken words, not only START FRAME, action, "
            "scene description, recipe text, or dish visuals. The viewer must hear what dish is being cooked "
            "in the first scene.\n"
            "- The end frame prompt, when present, must preserve the same characters, identities, food, props, and scale "
            "while changing only the intended final state/action.\n"
            "- Prompts must forbid visible text, captions, subtitles, watermarks, logos, and photorealism.\n\n"
            "Critical issues:\n"
            "- missing grandma face when grandma is expected\n"
            "- grandma is shown only from behind, cropped away, hidden, tiny, faceless, or substituted by a different woman\n"
            "- missing required cat when a cat is expected\n"
            "- wrong cat color or wrong cat identity versus CHARACTER DESCRIPTIONS\n"
            "- duplicate same-identity cat: two orange cats, two white cats, two black-and-white cats, mirrored cat copies, "
            "or unnamed extra cats with the same coat/identity as an expected cat\n"
            "- realistic, photorealistic, or live-action character style instead of cartoon\n"
            "- cooking pot/pan/cauldron is not on a visible stovetop burner/hob\n"
            "- unsupported visible ingredient, side, topping, sauce, or garnish not present in the recipe/request/brief\n"
            "- scene 1 VIDEO PROMPT speech does not explicitly say the dish name after CHARACTERS SPEAKS\n"
            "- video prompt uses ingredients/tableware/props absent from the start frame prompt\n"
            "- final/payoff start frame prompt does not anchor the finished dish to DISH VISUAL REQUIREMENTS\n"
            "- start/end frame prompt allows the main dish or required food object to be severely cropped or unreadable\n"
            "- intermediate preparation prompt is incorrectly treated as a final/payoff dish frame\n"
            "- intermediate preparation prompt shows an unrelated food/process instead of the current recipe state\n"
            "- start/end frame prompts do not preserve character identity from references\n"
            "- abnormal character scale such as a giant cat or tiny grandma\n"
            "- prompts allow text, captions, watermarks, or photorealism\n\n"
            "Scoring rule: if you mention any significant deviation, mismatch, missing required character, wrong cat "
            "identity/color, or extra character, you must put it in critical_issues, set status to FIX_PROMPT, and keep "
            "overall_score below the approval threshold. Never return APPROVE for a negative finding.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('prompt', 'APPROVE | FIX_PROMPT | HUMAN_REVIEW')}\n\n"
            f"RECIPE:\n{recipe_text or '-'}\n\n"
            f"DISH VISUAL REQUIREMENTS:\n{dish_visual_brief or '-'}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"SCENE DESCRIPTION:\n{sanitize_character_names_and_keys(scene_description) or '-'}\n"
            f"EXPECTED CHARACTERS: {expected_character_labels or '-'}\n"
            f"CHARACTER DESCRIPTIONS:\n{json.dumps(character_descriptions, ensure_ascii=False, indent=2)}\n\n"
            f"START FRAME PROMPT:\n{sanitize_character_names_and_keys(start_frame_prompt) or '-'}\n\n"
            f"END FRAME PROMPT:\n{sanitize_character_names_and_keys(end_frame_prompt or '') or '-'}\n\n"
            f"VIDEO PROMPT:\n{sanitize_character_names_and_keys(video_prompt) or '-'}\n"
        )
        neutral_prompt = (
            "You are a neutral production QA checker for short cartoon cooking prompts. "
            "Review only whether the prompts are coherent, safe for a cozy cartoon cooking video, "
            "visually consistent, free of readable text/logo/watermark requirements, and faithful to the recipe. "
            "If prompts would likely create wrong characters, wrong food, readable text, realistic/live-action drift, "
            "unsafe animal placement near heat/cookware, or missing required speech content, return FIX_PROMPT. "
            "Use HUMAN_REVIEW only if the prompt text cannot be evaluated.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('prompt', 'APPROVE | FIX_PROMPT | HUMAN_REVIEW')}\n\n"
            f"RECIPE SUMMARY:\n{_qa_safe_context_excerpt(recipe_text)}\n\n"
            f"DISH NOTES:\n{_qa_safe_context_excerpt(dish_visual_brief)}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"SCENE DESCRIPTION:\n{_qa_safe_context_excerpt(sanitize_character_names_and_keys(scene_description), max_chars=800)}\n"
            f"EXPECTED CHARACTERS: {expected_character_labels or '-'}\n"
            f"START FRAME PROMPT:\n{_qa_safe_context_excerpt(sanitize_character_names_and_keys(start_frame_prompt), max_chars=1000)}\n\n"
            f"END FRAME PROMPT:\n{_qa_safe_context_excerpt(sanitize_character_names_and_keys(end_frame_prompt or ''), max_chars=700)}\n\n"
            f"VIDEO PROMPT:\n{_qa_safe_context_excerpt(sanitize_character_names_and_keys(video_prompt), max_chars=1000)}\n"
        )
        data = _call_ai_approval_model_compatible(prompt, images=None, fallback_prompts=[neutral_prompt], config=cfg)
        return normalize_ai_approval_result(
            data,
            stage="prompt",
            raw_model_response=data.get("_raw_model_response"),
            model_name=data.get("_model_name") or cfg.model,
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
    scene_number: int | str = "",
    dish_visual_brief: str = "",
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
            "Attached image order:\n"
            "- Image 1 is the generated candidate frame to approve or reject.\n"
            "- All following images, when present, are mandatory character/style reference images. Use them as visual evidence, "
            "not optional inspiration. Compare faces, proportions, costume details, coat colors, cat markings, lighting, "
            "palette, softness, and rendering style against these references before deciding.\n"
            "- If the candidate frame does not clearly preserve the referenced character identities or project style, return "
            "REGENERATE_IMAGE even if the scene composition is otherwise attractive.\n\n"
            "The video style must be:\n"
            "- colorful cartoon style\n"
            "- soft stylized shading\n"
            "- non realistic rendering\n"
            "- no photorealism\n"
            "- warm cozy lighting\n"
            "- cozy grandmother cooking atmosphere\n"
            "- exact visual continuity with attached character/reference images when references are provided\n\n"
            "Evaluate:\n"
            "1. Scene match\n"
            "2. Character consistency\n"
            "3. Food and recipe accuracy against the real-world dish appearance\n"
            "4. Visual artifacts\n"
            "5. Cartoon style consistency\n"
            "6. Absence of subtitles, captions, typography, watermarks, and any visible text\n"
            "7. Whether the frame is a single continuous camera shot, not a collage, split-screen, stitched panel, "
            "or image with a horizontal/vertical seam\n"
            "8. Whether the frame is suitable as a start frame for a short cooking video\n"
            "9. Whether the art style, line quality, shading, palette, character face design, costume rendering, "
            "and cat design match the attached reference images/project style, not merely any cartoon style\n\n"
            "Canonical dish lookup:\n"
            "- Before scoring the image, identify from your own culinary/world knowledge what the named dish should "
            "look like in real life. Use the recipe and DISH VISUAL REQUIREMENTS to resolve the exact variant.\n"
            "- Compare the attached frame against that canonical target: food shape, size, color, surface texture, "
            "interior/cut view if relevant, sauce/broth state, ingredient forms, and serving/process state.\n"
            "- Reject if the visible food uses the wrong ingredient geometry, piece size, surface texture, internal "
            "structure, sauce/binding state, or serving form for the named dish, even when the colors look appetizing.\n"
            "- Add a short note about the canonical appearance you used to positive_notes or warnings. If you are "
            "not confident enough to identify the dish, use HUMAN_REVIEW rather than APPROVE.\n\n"
            "Reference-image verification:\n"
            "- Explicitly compare the candidate frame to each attached reference image that is relevant to the scene.\n"
            "- In positive_notes or warnings, mention that you checked the reference style/identity. If there is a mismatch, "
            "describe it as a critical issue.\n"
            "- Do not approve merely because the candidate is polished, cute, or high quality. A polished frame with the "
            "wrong reference style, wrong face/costume, wrong cat markings, or wrong dish is still REGENERATE_IMAGE.\n\n"
            "Viral retention checks:\n"
            "- the first frame is immediately understandable\n"
            "- the first scene is not boring preparation if a result/payoff frame is expected\n"
            "- food looks appetizing and readable\n"
            "- if the frame shows the cooked/final dish, it looks like the real dish described in DISH VISUAL REQUIREMENTS, not a generic substitute\n"
            "- if the frame shows an intermediate recipe step, ingredients/process state match the recipe and are visually consistent with that dish\n"
            "- cozy grandma/cat style is preserved\n"
            "- grandmother looks kind and friendly\n"
            "- if grandma is expected, her face is clearly visible and readable from the front or three-quarter view, "
            "not back-only, cropped out, hidden, tiny, faceless, or replaced by a different woman\n"
            "- if grandma appears in the frame, her full face and head must stay inside the frame: eyes, nose, mouth, "
            "chin, and head covering cannot be cut by the top or side edge\n"
            "- cat looks cute, not scary\n"
            "- grandma and every cat/character match their reference identity and CHARACTER DESCRIPTIONS\n"
            "- art style matches the attached reference images/project style exactly; do not approve a different "
            "storybook, Disney-like, Pixar-like, anime, watercolor, oil-painting, 3D-render, or generic cute-cartoon style\n"
            "- there is no duplicated same-identity cat: no two orange cats, no two white cats, no two black-and-white cats; "
            "multiple cats are acceptable only when they are distinct expected identities with distinct coats\n"
            "- any pot, saucepan, cauldron, frying pan, or skillet that is cooking is on a visible stovetop burner/hob\n"
            "- no unsupported ingredient, side, topping, sauce, or garnish appears beyond the recipe/request/brief\n"
            "- the main food is not severely cropped; the dish or key food object is inside the frame enough to "
            "verify shape, texture, color, and ingredient state\n"
            "- character scale is normal: no giant cat, no tiny grandma, no toy-sized people unless explicitly intended\n"
            "- no text covers faces or food\n\n"
            "- the frame is one coherent uninterrupted image; reject split panels, collage layouts, stitched seams, "
            "duplicated scene bands, or any horizontal/vertical divider that makes it look glued from multiple images\n\n"
            "Be strict. Do not approve if there are visible deformations, wrong characters, wrong food, "
            "text on screen, photorealism, or major mismatch with the prompt.\n\n"
            "Critical issues:\n"
            "- visible subtitles or captions\n"
            "- any on-screen text\n"
            "- watermark\n"
            "- photorealistic style\n"
            "- wrong art style or style mismatch versus attached references/project style\n"
            "- characters are drawn as a different storybook/Disney-like/generic cartoon design instead of the reference style\n"
            "- missing grandmother when required\n"
            "- missing readable grandma face when grandma is required\n"
            "- grandma is back-only, cropped, hidden, tiny, faceless, or replaced by a different woman\n"
            "- grandma's face or head is cut off by the frame edge\n"
            "- wrong main dish\n"
            "- dish visual mismatch: the visible food has the wrong ingredient geometry, piece size, surface texture, "
            "internal structure, sauce/binding state, or serving form for the named dish\n"
            "- strongly distorted face\n"
            "- strongly distorted hands\n"
            "- missing required cat\n"
            "- cooked/final food shape/color/texture/serving does not match DISH VISUAL REQUIREMENTS\n"
            "- intermediate food/process state does not match the recipe step\n"
            "- wrong cat coat color or wrong cat identity versus CHARACTER DESCRIPTIONS\n"
            "- duplicate same-identity cat: two orange cats, two white cats, two black-and-white cats, mirrored cat copies, "
            "or unnamed extra cats with the same coat/identity as an expected cat\n"
            "- realistic, photorealistic, or live-action character style instead of cartoon\n"
            "- cooking pot/pan/cauldron is not on a visible stovetop burner/hob\n"
            "- unsupported visible ingredient, side, topping, sauce, or garnish not present in the recipe/request/brief\n"
            "- main dish or required food object is severely cropped, cut off by frame edge, or unreadable for dish verification\n"
            "- character style changed heavily\n"
            "- illustration style, palette, line quality, shading, face design, costume rendering, or cat design does not match references\n"
            "- candidate frame was not compared to attached reference images, or references were ignored\n"
            "- any prompt-mentioned character does not match its description in CHARACTER DESCRIPTIONS\n"
            "- abnormal character scale such as a giant cat, tiny grandma, or oversized secondary character\n"
            "- split-screen, collage, stitched image, duplicated panel, or visible horizontal/vertical seam\n"
            "- image does not match the scene\n\n"
            "Scoring rule: if you mention any significant deviation, mismatch, wrong real-world dish appearance, missing required character, wrong cat "
            "identity/color, extra character, or abnormal scale, you must put it in critical_issues, set status to "
            "REGENERATE_IMAGE, and keep overall_score below the approval threshold. Never return APPROVE for a negative "
            "finding.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('image', 'APPROVE | REGENERATE_IMAGE | HUMAN_REVIEW')}\n\n"
            f"RECIPE:\n{recipe_text or '-'}\n\n"
            f"DISH VISUAL REQUIREMENTS:\n{dish_visual_brief or '-'}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"SCENE DESCRIPTION:\n{sanitize_character_names_and_keys(scene_description) or '-'}\n\n"
            f"SCENE PROMPT:\n{sanitize_character_names_and_keys(scene_prompt) or '-'}\n"
            f"EXPECTED CHARACTERS: {expected_character_labels or '-'}\n"
            f"CHARACTER DESCRIPTIONS:\n{json.dumps(character_descriptions, ensure_ascii=False, indent=2)}\n"
            f"EXPECTED OBJECTS: {', '.join(expected_objects or []) or '-'}\n"
            f"STYLE REQUIREMENTS:\n{style_requirements or '-'}\n"
        )
        neutral_prompt = (
            "You are a visual quality checker for a short cartoon cooking video.\n"
            "Look at the attached frame and compare it to the expected brief. Use neutral wording. "
            "Return only one JSON object and no markdown.\n\n"
            "Check these items:\n"
            "1. The frame matches the expected scene and current recipe step.\n"
            "2. Required characters are present and keep the provided character identities.\n"
            "   If grandma is expected, her readable face must be visible; a frame with only her hand, arm, body, "
            "back, or cropped head is not production-ready.\n"
            "3. Food or ingredient state matches the recipe step and dish notes.\n"
            "4. The style is colorful cozy cartoon, not photographic.\n"
            "5. Anatomy, scale, faces, paws/hands, and composition look production-ready.\n"
            "6. The frame is one continuous image, not a split panel, collage, stitched image, or scene band with a seam.\n"
            "7. There is no readable text, caption, logo, or watermark.\n\n"
            "If the frame is production-ready, status must be APPROVE. "
            "If a new frame is needed, status must be REGENERATE_IMAGE. "
            "Hard fail as REGENERATE_IMAGE for missing readable grandma face when grandma is expected, "
            "including only-hand/only-arm/only-body grandma crops. "
            "Use HUMAN_REVIEW only when the image cannot be evaluated.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('image', 'APPROVE | REGENERATE_IMAGE | HUMAN_REVIEW')}\n\n"
            f"RECIPE SUMMARY:\n{_qa_safe_context_excerpt(recipe_text)}\n\n"
            f"DISH NOTES:\n{_qa_safe_context_excerpt(dish_visual_brief)}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"EXPECTED BRIEF:\n{_qa_safe_context_excerpt(sanitize_character_names_and_keys(scene_prompt), max_chars=900)}\n"
            f"EXPECTED CHARACTERS: {expected_character_labels or '-'}\n"
            f"CHARACTER NOTES:\n{json.dumps(character_descriptions, ensure_ascii=False, indent=2)}\n"
            f"EXPECTED OBJECTS: {', '.join(expected_objects or []) or '-'}\n"
        )
        images = [image_bytes]
        if _bool_env("AI_APPROVAL_INCLUDE_REFERENCE_IMAGES", True):
            images.extend(previous_reference_images or [])
        data = _call_ai_approval_model_compatible(
            prompt,
            images=images,
            fallback_prompts=[neutral_prompt],
            config=cfg,
        )
        return normalize_ai_approval_result(
            data,
            stage="image",
            raw_model_response=data.get("_raw_model_response"),
            model_name=data.get("_model_name") or cfg.model,
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
        "9. Whether the line sounds unfinished at the end.\n"
        "10. Wrong word stress or pronunciation mistakes. Do not rely only on recognized_text: recognized_text can be correct while stress is wrong.\n\n"
        "11. Whether intonation rises at the end like a question or uncertainty when EXPECTED SPEECH is a normal declarative cooking line. "
        "Final rising/question intonation, trailing uncertainty, or 'unfinished thought' cadence is a defect unless the expected text is explicitly a question.\n"
        "12. Whether any extra spoken words were added or any expected words were skipped. Extra filler, paraphrase, or omitted words are defects for this pipeline.\n\n"
        f"{RUSSIAN_PRONUNCIATION_QA_GUIDE}\n\n"
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
        '  "pronunciation_issues": [],\n'
        '  "wrong_stress_words": [],\n'
        '  "rising_intonation_detected": false,\n'
        '  "intonation_issues": [],\n'
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
    missing_words = _string_list(data.get("missing_words"))
    extra_words = _string_list(data.get("extra_words"))
    cut_off = _bool_value(data.get("cut_off_detected"), False)
    long_pauses = _bool_value(data.get("long_pauses_detected"), False)
    pronunciation_issues = _string_list(data.get("pronunciation_issues"))
    wrong_stress_words = _string_list(data.get("wrong_stress_words"))
    rising_intonation = _bool_value(data.get("rising_intonation_detected"), False)
    intonation_issues = _string_list(data.get("intonation_issues"))
    phrase_overflow = _bool_value(data.get("phrase_overflow_detected"), False)
    unfinished_line = _bool_value(data.get("unfinished_line_detected"), False)
    status = str(data.get("status") or "").strip().upper()
    if status not in ALLOWED_STATUSES:
        status = AI_APPROVAL_STATUS_HUMAN_REVIEW
    if (
        phrase_completed
        and match_score >= 0.85
        and not cut_off
        and not phrase_overflow
        and not unfinished_line
        and not missing_words
        and not extra_words
        and not pronunciation_issues
        and not wrong_stress_words
        and not rising_intonation
        and not intonation_issues
    ):
        status = AI_APPROVAL_STATUS_APPROVE
    elif (
        cut_off
        or phrase_overflow
        or unfinished_line
        or missing_words
        or extra_words
        or pronunciation_issues
        or wrong_stress_words
        or rising_intonation
        or intonation_issues
        or match_score < 0.70
    ):
        status = AI_APPROVAL_STATUS_REGENERATE_VIDEO
    else:
        status = AI_APPROVAL_STATUS_HUMAN_REVIEW
    return SpeechTranscriptionResult(
        phrase_completed=phrase_completed,
        match_score=match_score,
        missing_words=missing_words,
        extra_words=extra_words,
        cut_off_detected=cut_off,
        long_pauses_detected=long_pauses,
        speech_speed=str(data.get("speech_speed") or "unknown").strip() or "unknown",
        recognized_text=str(data.get("recognized_text") or "").strip(),
        pronunciation_issues=pronunciation_issues,
        wrong_stress_words=wrong_stress_words,
        rising_intonation_detected=rising_intonation,
        intonation_issues=intonation_issues,
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
    dish_visual_brief: str = "",
    scene_number: int | str = "",
    language: str | None = None,
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
            "- the real-world dish appearance in DISH VISUAL REQUIREMENTS\n"
            "- the previous character references if provided\n"
            "- the visual style requirements\n\n"
            "Canonical dish lookup:\n"
            "- Before scoring, identify from your own culinary/world knowledge what the named dish should look like "
            "in real life. Use the recipe and DISH VISUAL REQUIREMENTS to resolve the exact variant.\n"
            "- Compare sampled frames against that canonical target: food shape, size, color, surface texture, "
            "interior/cut view if relevant, sauce/broth state, ingredient forms, and serving/process state.\n"
            "- Add a short note about the canonical appearance you used to positive_notes or warnings. If the dish "
            "cannot be identified confidently, use HUMAN_REVIEW rather than APPROVE.\n\n"
            "Important rules:\n"
            "- No subtitles\n"
            "- No captions\n"
            "- No visible text\n"
            "- No typography\n"
            "- No watermarks\n"
            "- No photorealism\n"
            "- Any visible text, logo, watermark, caption, subtitles, UI text, label, or readable typography in the video = REGENERATE_VIDEO\n"
            "- Any photorealistic, live-action, or realistic-animal drift for recurring characters = REGENERATE_VIDEO\n"
            "- Grandma must stay consistent\n"
            "- Grandma's face must remain visible, readable, and consistent with the reference/start frame when grandma is expected\n"
            "- Do not approve back-only grandma shots, cropped/hidden/tiny/faceless grandma shots, or a different woman's face\n"
            "- Cat must stay consistent if present\n"
            "- Grandma, cat, and any other recurring characters must match the provided reference/start images and canonical identities\n"
            "- Art style must match the provided reference/start images and project style. Do not approve a clip that "
            "switches into a different storybook, Disney-like, Pixar-like, anime, watercolor, oil-painting, 3D-render, "
            "or generic cute-cartoon look, even if it is still non-photorealistic.\n"
            "- Character scale must remain normal: no giant cat, tiny grandma, or accidental oversized/undersized character\n"
            "- Cat identity count: multiple cats are acceptable only when they are distinct expected identities. "
            "Do not approve duplicate same-identity cats: two orange cats, two white cats, two black-and-white cats, "
            "mirrored copies, kittens, or unnamed extra cats with the same coat/identity as an expected cat\n"
            "- Do not approve duplicated white cats, two white cats, a white cat with long ears, rabbit-like ears, dog-like body, or wrong animal anatomy\n"
            "- Wrong cat color, wrong cat identity, or a cat that stops matching the canonical/start-frame character identity = REGENERATE_VIDEO\n"
            "- Do not approve a cat inside an oven, pot, saucepan, cauldron, frying pan, skillet, hot oil, boiling liquid, or on a stovetop burner/hob; the cat must stay safely outside cookware and heat sources\n"
            "- Every human or animal character must stay visibly cartoon/stylized, never photorealistic or live-action\n"
            "- The video must remain one continuous scene; reject split-screen, collage-like, stitched, duplicated-panel, "
            "or visible horizontal/vertical seam artifacts in any sampled frame\n"
            "- Any pot, saucepan, cauldron, frying pan, or skillet that is boiling, simmering, frying, sauteing, steaming, or cooking must be on a visible stovetop burner/hob\n"
            "- Do not approve unsupported visible ingredients, sides, toppings, sauces, or garnishes that are absent from the recipe/request/brief\n"
            "- Food must match the recipe step\n"
            "- Cooked/final food must still match DISH VISUAL REQUIREMENTS; intermediate recipe steps must match the current ingredient/process state and not drift into a generic or unrelated dish\n"
            "- Do not approve if the main dish or required food object is severely cropped, cut off by the frame edge, "
            "or unreadable for dish verification in the sampled frames where it is the focus\n"
            "- Major hand, face, mouth, or object deformation is a critical issue\n"
            "- Wrong art style or style mismatch versus reference/start images is a critical issue\n"
            "- Missing, unreadable, hidden, back-only, cropped, or wrong grandma face is a critical issue\n"
            "- Split-screen, collage, stitched-panel, duplicated scene band, or visible seam artifacts are critical issues\n"
            "- If the scene includes lipsync, grandmother's mouth should appear naturally active during speech frames\n"
            "- If the original video/audio is attached and speech is present, pronunciation must sound natural for the spoken language. "
            "Wrong stress, robotic intonation, swallowed syllables, clipped endings, rising/question intonation at the end of a declarative line, "
            "or unnatural emphasis are content defects.\n"
            "- Spoken words must match EXPECTED SPEECH exactly enough: no added filler words, no paraphrase, no missing expected words.\n"
            "- For Russian speech, wrong stress, distorted dish name, swallowed endings, extra words, missing words, or question-like rising intonation in a declarative phrase = REGENERATE_VIDEO\n"
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
            "- declarative cooking lines end with calm final intonation, not a question-like rising tone\n"
            "- speech has no unexpected extra words and does not omit expected words\n"
            "- the spoken phrase appears feasible within the expected duration\n\n"
            "Technical limitations are not content defects:\n"
            "- If speech transcription is disabled, do not mark it as a critical issue.\n"
            "- If the original MP4 is not attached, do not invent pronunciation defects; mention pronunciation as uncertain in warnings.\n"
            "- If local frame extraction failed but an MP4 video or reference/start frame is attached, review the attached media.\n\n"
            "Scoring rule: if you mention any significant deviation, mismatch, missing required character, wrong cat "
            "identity/color, extra character, abnormal scale, unnatural word stress/pronunciation, rising/question intonation, or missing/extra spoken words, you must put it "
            "in critical_issues, set status to REGENERATE_VIDEO, and keep overall_score below the approval threshold. "
            "Never return APPROVE for a negative finding.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('video', 'APPROVE | REGENERATE_VIDEO | HUMAN_REVIEW')}\n\n"
            f"MEDIA INPUT NOTE:\n{media_note}\n"
            f"SPEECH INPUT NOTE:\n{speech_note}\n\n"
            f"RECIPE:\n{recipe_text or '-'}\n\n"
            f"DISH VISUAL REQUIREMENTS:\n{dish_visual_brief or '-'}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"SCENE PROMPT:\n{sanitize_character_names_and_keys(scene_prompt) or '-'}\n"
            f"EXPECTED SPEECH TEXT:\n{expected_speech_text or '-'}\n"
            f"EXPECTED DURATION: {duration if duration is not None else '-'}\n"
            f"SPEECH QA RESULT:\n{json.dumps(speech.to_dict(), ensure_ascii=False)}\n"
            f"LOCAL WARNINGS:\n{json.dumps(warnings, ensure_ascii=False)}\n"
        )
        neutral_prompt = (
            "You are a neutral quality checker for sampled frames from a cozy cartoon cooking video.\n"
            "Use calm production-review wording. Compare the attached media to the expected brief and return only JSON.\n\n"
            "Check whether:\n"
            "1. The action matches the expected scene.\n"
            "2. The recurring characters stay consistent and cartoon-stylized.\n"
            "3. The art style matches the provided reference/start images, not a different generic cartoon or storybook style.\n"
            "4. The food/process state matches the recipe step and dish notes.\n"
            "5. There is no readable text, logo, watermark, or photographic/live-action drift.\n"
            "6. The clip has usable movement and no major visual artifacts.\n"
            "7. If speech evidence is available, it matches EXPECTED SPEECH closely enough for the duration.\n\n"
            "Hard fail as REGENERATE_VIDEO for visible text/logo/watermark/caption, wrong reference/project art style, storybook/Disney-like/generic cartoon style drift, photorealistic/live-action character drift, duplicate same-identity cats such as two orange cats, two white cats, or two black-and-white cats, white cats with long/rabbit-like ears, wrong cat identity/color, unsafe cat placement inside cookware/oven/hot pan/stove, Russian wrong stress, distorted dish name, extra/missing spoken words, or rising question-like intonation in a declarative phrase.\n\n"
            "Use status APPROVE when production-ready, REGENERATE_VIDEO when a new clip is needed, "
            "and HUMAN_REVIEW only when the media cannot be evaluated.\n\n"
            "Return only valid JSON:\n"
            f"{_json_schema_text('video', 'APPROVE | REGENERATE_VIDEO | HUMAN_REVIEW')}\n\n"
            f"MEDIA INPUT NOTE:\n{media_note}\n"
            f"SPEECH INPUT NOTE:\n{speech_note}\n\n"
            f"RECIPE SUMMARY:\n{_qa_safe_context_excerpt(recipe_text)}\n\n"
            f"DISH NOTES:\n{_qa_safe_context_excerpt(dish_visual_brief)}\n\n"
            f"SCENE NUMBER: {scene_number}\n"
            f"EXPECTED BRIEF:\n{_qa_safe_context_excerpt(sanitize_character_names_and_keys(scene_prompt), max_chars=900)}\n"
            f"EXPECTED SPEECH TEXT:\n{_qa_safe_context_excerpt(expected_speech_text or '-', max_chars=400)}\n"
            f"EXPECTED DURATION: {duration if duration is not None else '-'}\n"
            f"SPEECH QA RESULT:\n{json.dumps(speech.to_dict(), ensure_ascii=False)}\n"
            f"LOCAL WARNINGS:\n{json.dumps(warnings, ensure_ascii=False)}\n"
        )
        data = _call_ai_approval_model_compatible(
            prompt,
            images=frames,
            fallback_prompts=[neutral_prompt],
            config=cfg,
        )
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
            data["speech_recognized_text"] = speech.recognized_text
            data["speech_missing_words"] = list(speech.missing_words)
            data["speech_extra_words"] = list(speech.extra_words)
            data["speech_wrong_stress_words"] = list(speech.wrong_stress_words)
            if (
                str(language or "").strip().lower() == "ru"
                and expected_speech_text
                and not attached_original_video_for_speech
            ):
                data.setdefault("critical_issues", [])
                if isinstance(data["critical_issues"], list):
                    data["critical_issues"].append("RU speech QA did not receive original MP4/audio input")
            if (
                speech.cut_off_detected
                or speech.phrase_overflow_detected
                or speech.unfinished_line_detected
                or speech.speech_speed == "too_fast"
                or speech.missing_words
                or speech.extra_words
                or speech.pronunciation_issues
                or speech.wrong_stress_words
                or speech.rising_intonation_detected
                or speech.intonation_issues
                or speech.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO
            ):
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
                    if speech.missing_words:
                        issues.append("missing spoken words: " + ", ".join(speech.missing_words))
                    if speech.extra_words:
                        issues.append("extra spoken words: " + ", ".join(speech.extra_words))
                    if speech.wrong_stress_words:
                        issues.append("wrong word stress: " + ", ".join(speech.wrong_stress_words))
                    if speech.pronunciation_issues:
                        issues.extend(f"pronunciation issue: {issue}" for issue in speech.pronunciation_issues)
                    if speech.rising_intonation_detected:
                        issues.append("rising/question intonation at end of declarative line")
                    if speech.intonation_issues:
                        issues.extend(f"intonation issue: {issue}" for issue in speech.intonation_issues)
                    if speech.status == AI_APPROVAL_STATUS_REGENERATE_VIDEO and not issues:
                        issues.append("speech transcription does not match expected phrase")
                    data["critical_issues"].extend(issues)
        return normalize_ai_approval_result(
            data,
            stage="video",
            raw_model_response=data.get("_raw_model_response"),
            model_name=data.get("_model_name") or cfg.model,
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
        "positive_notes": list(result.positive_notes),
        "recommended_action": result.recommended_action,
        "raw_model_response": result.raw_model_response,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_name": result.model_name,
        "model": result.model_name,
        "prompt_variant": result.prompt_variant,
        "recognized_text": result.speech_recognized_text,
        "speech_recognized_text": result.speech_recognized_text,
        "wrong_stress_words": list(result.speech_wrong_stress_words),
        "speech_missing_words": list(result.speech_missing_words),
        "speech_extra_words": list(result.speech_extra_words),
    }


def format_ai_approval_summary(result: AIApprovalResult) -> str:
    reasons = result.critical_issues or result.warnings or result.positive_notes
    reason_text = ", ".join(reasons[:3]) if reasons else (result.recommended_action or "без дополнительных замечаний")
    stage_label = {
        "prompt": "prompt",
        "image": "image",
        "video": "video",
        "final": "final",
    }.get(str(result.stage or "").strip().lower(), str(result.stage or "unknown"))
    if result.status == AI_APPROVAL_STATUS_HUMAN_REVIEW:
        return (
            f"🤖 ИИ апрув decision: {stage_label} / HUMAN_REVIEW\n"
            f"Оценка: {result.overall_score:.2f}\n"
            f"Причина: {reason_text}."
        )
    return (
        f"🤖 ИИ апрув decision: {stage_label} / {result.status}\n"
        f"Оценка: {result.overall_score:.2f}\n"
        f"Причины: {reason_text}."
    )


def encode_video_bytes_for_debug(video_bytes: bytes, *, max_bytes: int = 256) -> str:
    return base64.b64encode(video_bytes[:max_bytes]).decode("ascii")
