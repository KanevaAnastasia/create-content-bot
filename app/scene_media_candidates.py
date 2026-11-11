"""Helpers for per-scene media candidate history, likes, and selection."""

from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime, UTC
from typing import Any

from .media_file_store import read_media_uri_bytes

DEFAULT_SCENE_VIDEO_SPEED_COEF = 1.0
DEFAULT_VEO_SCENE_VIDEO_SPEED_COEF = 1.1
SCENE_MEDIA_MESSAGE_INDEX_KEY = "scene_media_message_index"
CYRILLIC_CHAR_RE = re.compile(r"[А-Яа-яЁё]")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode_b64(data: str | None) -> bytes | None:
    raw = str(data or "").strip()
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except Exception:
        return None


def _new_candidate_id() -> str:
    return uuid.uuid4().hex


def default_scene_video_speed_coef(scene: dict[str, Any] | None) -> float:
    if not isinstance(scene, dict):
        return DEFAULT_SCENE_VIDEO_SPEED_COEF
    model = str(
        scene.get("model")
        or scene.get("video_model")
        or scene.get("video_provider")
        or scene.get("provider")
        or ""
    ).strip().lower()
    if model.startswith("veo"):
        return DEFAULT_VEO_SCENE_VIDEO_SPEED_COEF
    return DEFAULT_SCENE_VIDEO_SPEED_COEF


def _ensure_image_candidates(scene: dict[str, Any]) -> list[dict[str, Any]]:
    items = scene.get("image_candidates")
    if not isinstance(items, list):
        items = []
        scene["image_candidates"] = items
    return items


def _ensure_video_candidates(scene: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    mapping = scene.get("video_candidates_by_lang")
    if not isinstance(mapping, dict):
        mapping = {}
        scene["video_candidates_by_lang"] = mapping
    out: dict[str, list[dict[str, Any]]] = {}
    for key, value in mapping.items():
        if isinstance(value, list):
            out[str(key)] = value
        else:
            out[str(key)] = []
    scene["video_candidates_by_lang"] = out
    return out


def _legacy_start_image_candidate(scene: dict[str, Any]) -> dict[str, Any] | None:
    image_b64 = str(scene.get("start_frame_image_b64") or "").strip()
    if not image_b64:
        return None
    return {
        "candidate_id": "legacy-start",
        "kind": "start",
        "image_b64": image_b64,
        "created_at": "",
        "liked": False,
        "approved_for_video": True,
    }


def _legacy_end_image_candidate(scene: dict[str, Any]) -> dict[str, Any] | None:
    image_b64 = str(scene.get("end_frame_image_b64") or "").strip()
    if not image_b64:
        return None
    return {
        "candidate_id": "legacy-end",
        "kind": "end",
        "image_b64": image_b64,
        "created_at": "",
        "liked": False,
        "approved_for_video": True,
    }


def _legacy_video_candidate(scene: dict[str, Any], language: str) -> dict[str, Any] | None:
    results = scene.get("video_results_by_lang")
    if not isinstance(results, dict):
        return None
    item = results.get(language)
    if not isinstance(item, dict):
        return None
    video_b64 = str(item.get("video_b64") or "").strip()
    if not video_b64:
        return None
    approvals = scene.get("video_approvals_by_lang")
    approved = isinstance(approvals, dict) and bool(approvals.get(language))
    return {
        "candidate_id": f"legacy-video-{language}",
        "language": language,
        "video_b64": video_b64,
        "created_at": "",
        "liked": False,
        "approved_for_production": approved,
        "speed_coef": default_scene_video_speed_coef(scene),
        "source_image_candidate_id": None,
    }


def register_image_candidate(
    scene: dict[str, Any],
    *,
    kind: str,
    image_bytes: bytes,
) -> dict[str, Any]:
    candidate = {
        "candidate_id": _new_candidate_id(),
        "kind": str(kind or "start"),
        "image_b64": _encode_b64(image_bytes),
        "created_at": _utc_now_iso(),
        "liked": False,
        "approved_for_video": False,
    }
    _ensure_image_candidates(scene).append(candidate)
    return candidate


def register_video_candidate(
    scene: dict[str, Any],
    *,
    language: str,
    video_bytes: bytes,
    source_image_candidate_id: str | None = None,
    speed_coef: float | None = None,
) -> dict[str, Any]:
    lang = str(language or "ru").strip().lower() or "ru"
    candidate = {
        "candidate_id": _new_candidate_id(),
        "language": lang,
        "video_b64": _encode_b64(video_bytes),
        "created_at": _utc_now_iso(),
        "liked": False,
        "approved_for_production": False,
        "speed_coef": float(speed_coef if speed_coef is not None else default_scene_video_speed_coef(scene)),
        "source_image_candidate_id": source_image_candidate_id,
    }
    mapping = _ensure_video_candidates(scene)
    bucket = mapping.get(lang)
    if not isinstance(bucket, list):
        bucket = []
        mapping[lang] = bucket
    bucket.append(candidate)
    scene["video_candidates_by_lang"] = mapping
    return candidate


def image_candidates_for_kind(scene: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    out = [
        item
        for item in _ensure_image_candidates(scene)
        if isinstance(item, dict) and str(item.get("kind") or "start") == kind
    ]
    if out:
        return out
    if kind == "end":
        legacy = _legacy_end_image_candidate(scene)
    else:
        legacy = _legacy_start_image_candidate(scene)
    return [legacy] if legacy is not None else []


def resolve_scene_image_for_video(scene: dict[str, Any], *, kind: str = "start") -> dict[str, Any] | None:
    candidates = image_candidates_for_kind(scene, kind)
    if not candidates:
        return None
    liked = [item for item in candidates if bool(item.get("liked"))]
    if liked:
        return liked[-1]
    return candidates[-1]


def resolve_scene_video_for_production(scene: dict[str, Any], language: str) -> dict[str, Any] | None:
    lang = str(language or "ru").strip().lower() or "ru"
    mapping = _ensure_video_candidates(scene)
    bucket = mapping.get(lang)
    candidates = [item for item in bucket if isinstance(item, dict)] if isinstance(bucket, list) else []
    if not candidates:
        legacy = _legacy_video_candidate(scene, lang)
        return legacy
    approved = [item for item in candidates if bool(item.get("approved_for_production"))]
    if approved:
        liked_approved = [item for item in approved if bool(item.get("liked"))]
        return liked_approved[-1] if liked_approved else approved[-1]
    liked = [item for item in candidates if bool(item.get("liked"))]
    if liked:
        return liked[-1]
    return candidates[-1]


def resolve_latest_approved_scene_video_for_final_build(
    scene: dict[str, Any],
    language: str,
) -> dict[str, Any] | None:
    lang = str(language or "ru").strip().lower() or "ru"
    mapping = _ensure_video_candidates(scene)
    bucket = mapping.get(lang)
    candidates = [item for item in bucket if isinstance(item, dict)] if isinstance(bucket, list) else []
    approved = [
        item
        for item in candidates
        if bool(item.get("approved_for_production")) and _candidate_matches_final_build_language(item, lang)
    ]
    if approved:
        return approved[-1]
    # Story visuals are deliberately generated once as silent Russian clips.
    # Localized final builds add their own narration and subtitles, so an EN
    # track must reuse the approved visual instead of requiring a duplicate
    # paid video generation for every scene.
    if lang != "ru" and str(scene.get("generation_mode") or "").strip().lower() == "story":
        ru_candidates = [
            item
            for item in mapping.get("ru") or []
            if isinstance(item, dict)
            and bool(item.get("approved_for_production"))
            and _candidate_matches_final_build_language(item, "ru")
        ]
        if ru_candidates:
            return ru_candidates[-1]
    legacy = _legacy_video_candidate(scene, lang)
    if legacy is not None and bool(legacy.get("approved_for_production")):
        return legacy
    return None


def latest_scene_video_candidate(scene: dict[str, Any], language: str) -> dict[str, Any] | None:
    lang = str(language or "ru").strip().lower() or "ru"
    mapping = _ensure_video_candidates(scene)
    bucket = mapping.get(lang)
    if isinstance(bucket, list) and bucket:
        return bucket[-1]
    return _legacy_video_candidate(scene, lang)


def find_image_candidate(scene: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    for item in _ensure_image_candidates(scene):
        if isinstance(item, dict) and str(item.get("candidate_id") or "") == candidate_id:
            return item
    return None


def find_video_candidate(scene: dict[str, Any], language: str, candidate_id: str) -> dict[str, Any] | None:
    mapping = _ensure_video_candidates(scene)
    bucket = mapping.get(str(language or "ru").strip().lower() or "ru")
    if not isinstance(bucket, list):
        return None
    for item in bucket:
        if isinstance(item, dict) and str(item.get("candidate_id") or "") == candidate_id:
            return item
    return None


def _speech_text_from_prompt(prompt: Any) -> str:
    raw = str(prompt or "")
    terminator_re = re.compile(
        r"(?is)\s+(?="
        r"(?:voice_lock(?:_[a-z0-9]+)*|speech_timing_plan|veo3_framing|framing)\s*:"
        r"|\b(?:natural-fast spoken delivery|render this video|never use horizontal landscape framing)\b"
        r")"
    )
    match = re.search(r"(?is)\bCHARACTERS\s+SPEAKS:\s*", raw)
    direct = ""
    if match:
        tail = raw[match.end() :]
        terminator = terminator_re.search(tail)
        newline_at = tail.find("\n")
        stop_at = len(tail)
        if terminator is not None:
            stop_at = min(stop_at, terminator.start())
        if newline_at >= 0:
            stop_at = min(stop_at, newline_at)
        direct = " ".join(tail[:stop_at].split())
    if direct:
        return direct
    parts: list[str] = []
    collecting = False
    for line in raw.splitlines():
        if re.search(r"(?i)\bspeech_timing_plan\s*:", line):
            collecting = True
            tail = re.sub(r"(?is)^.*?\bspeech_timing_plan\s*:\s*", "", line).strip()
            terminator = terminator_re.search(tail)
            if terminator is not None:
                tail = tail[: terminator.start()].strip()
            if tail:
                parts.append(tail)
            continue
        if not collecting:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("-"):
            break
        text = re.sub(
            r"^\s*-\s*(?:\d+(?:[\.,]\d+)?\s*-\s*\d+(?:[\.,]\d+)?s?\s*:\s*)?",
            "",
            stripped,
        ).strip()
        terminator = terminator_re.search(text)
        if terminator is not None:
            text = text[: terminator.start()].strip()
        if text:
            parts.append(text)
    return " ".join(" ".join(parts).split())


def _candidate_language_snapshot_text(candidate: dict[str, Any], language: str) -> str:
    lang = str(language or "").strip().lower()
    parts: list[str] = []
    speech_map = candidate.get("speech_by_lang")
    if isinstance(speech_map, dict) and str(speech_map.get(lang) or "").strip():
        parts.append(str(speech_map.get(lang) or "").strip())
    prompt_map = candidate.get("video_prompt_by_lang")
    if isinstance(prompt_map, dict):
        from_prompt = _speech_text_from_prompt(prompt_map.get(lang))
        if from_prompt:
            parts.append(from_prompt)
    return " ".join(parts).strip()


def _candidate_matches_final_build_language(candidate: dict[str, Any], language: str) -> bool:
    lang = str(language or "").strip().lower()
    if lang != "ru":
        return True
    text = _candidate_language_snapshot_text(candidate, lang)
    return not text or bool(CYRILLIC_CHAR_RE.search(text))


def mark_image_candidate_approved(scene: dict[str, Any], candidate_id: str | None) -> dict[str, Any] | None:
    if not candidate_id:
        return None
    candidate = find_image_candidate(scene, candidate_id)
    if candidate is not None:
        candidate["approved_for_video"] = True
    return candidate


def mark_video_candidate_approved(
    scene: dict[str, Any],
    language: str,
    candidate_id: str | None,
    *,
    speed_coef: float | None = None,
) -> dict[str, Any] | None:
    if not candidate_id:
        return None
    candidate = find_video_candidate(scene, language, candidate_id)
    if candidate is None:
        return None
    candidate["approved_for_production"] = True
    if speed_coef is not None:
        candidate["speed_coef"] = float(speed_coef)
    else:
        candidate["speed_coef"] = float(candidate.get("speed_coef") or default_scene_video_speed_coef(scene))
    return candidate


def set_candidate_liked(
    scene: dict[str, Any],
    *,
    media_type: str,
    candidate_id: str,
    liked: bool,
    language: str | None = None,
) -> dict[str, Any] | None:
    if media_type == "video":
        candidate = find_video_candidate(scene, str(language or "ru"), candidate_id)
    else:
        candidate = find_image_candidate(scene, candidate_id)
    if candidate is not None:
        candidate["liked"] = bool(liked)
    return candidate


def register_media_message_candidate(
    session: dict[str, Any],
    *,
    message_id: int | None,
    scene_idx: int,
    media_type: str,
    candidate_id: str,
    language: str | None = None,
    kind: str | None = None,
) -> None:
    if message_id is None:
        return
    raw = session.get(SCENE_MEDIA_MESSAGE_INDEX_KEY)
    if not isinstance(raw, dict):
        raw = {}
    raw[str(message_id)] = {
        "scene_idx": int(scene_idx),
        "media_type": str(media_type),
        "candidate_id": str(candidate_id),
        "language": str(language).strip().lower() if language else None,
        "kind": str(kind).strip().lower() if kind else None,
    }
    session[SCENE_MEDIA_MESSAGE_INDEX_KEY] = raw


def get_media_message_candidate(session: dict[str, Any], message_id: int | None) -> dict[str, Any] | None:
    if message_id is None:
        return None
    raw = session.get(SCENE_MEDIA_MESSAGE_INDEX_KEY)
    if not isinstance(raw, dict):
        return None
    item = raw.get(str(message_id))
    return item if isinstance(item, dict) else None


def resolve_candidate_by_message(
    session: dict[str, Any],
    production_scenes: list[dict[str, Any]],
    *,
    message_id: int | None,
    liked: bool,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    meta = get_media_message_candidate(session, message_id)
    if meta is None:
        return None
    scene_idx = int(meta.get("scene_idx", -1))
    if scene_idx < 0 or scene_idx >= len(production_scenes):
        return None
    scene = production_scenes[scene_idx]
    candidate = set_candidate_liked(
        scene,
        media_type=str(meta.get("media_type") or "image"),
        candidate_id=str(meta.get("candidate_id") or ""),
        language=str(meta.get("language") or "") or None,
        liked=liked,
    )
    if candidate is None:
        return None
    return scene, candidate


def selected_image_bytes_for_video(scene: dict[str, Any], *, kind: str = "start") -> bytes | None:
    selected = resolve_scene_image_for_video(scene, kind=kind)
    if selected is None:
        return None
    return _decode_b64(str(selected.get("image_b64") or "")) or read_media_uri_bytes(str(selected.get("media_uri") or ""))


def selected_video_bytes_for_production(scene: dict[str, Any], language: str) -> bytes | None:
    selected = resolve_scene_video_for_production(scene, language)
    if selected is None:
        return None
    return _decode_b64(str(selected.get("video_b64") or "")) or read_media_uri_bytes(str(selected.get("media_uri") or ""))


def parse_speed_coef(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        coef = float(value)
    else:
        raw = str(value).strip().replace(",", ".")
        if not raw:
            return None
        try:
            coef = float(raw)
        except ValueError:
            return None
    if coef <= 0:
        return None
    return coef
