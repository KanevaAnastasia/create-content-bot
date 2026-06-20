"""Estimated media-side cost accounting for scene videos."""

from __future__ import annotations

from typing import Any


_GEMINI_TEXT_PRICING_PER_MTOK_USD: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.54, 4.50),
    "gemini-2.5-flash-lite": (0.18, 0.72),
    "gemini-2.5-pro": (2.25, 18.00),
}

# Closest currently published per-image Vertex price tiers.
_IMAGE_GENERATION_PRICE_USD: dict[str, float] = {
    "gemini-3-pro-image": 0.06,
    "gemini-3.1-flash-image": 0.04,
    "gemini-3.1-flash-image-preview": 0.04,
    "gemini-2.5-flash-image": 0.039,
    "imagen-4.0-fast-generate-001": 0.02,
    "imagen-4.0-generate-001": 0.04,
    "imagen-4.0-ultra-generate-001": 0.06,
    "imagen-3.0-fast-generate-001": 0.02,
    "imagen-3.0-generate-001": 0.04,
    "imagen-3.0-generate-002": 0.04,
}


def estimate_text_tokens(text: str) -> int:
    """Google pricing reference uses ~4 chars per text token."""
    chars = len(str(text or ""))
    return max(1, round(chars / 4)) if chars else 0


def estimate_gemini_text_cost_usd(*, model: str, prompt_text: str, response_text: str) -> dict[str, Any]:
    """Estimate Gemini text cost from prompt/response char lengths."""
    model_id = (model or "").strip()
    in_price, out_price = _GEMINI_TEXT_PRICING_PER_MTOK_USD.get(model_id, _GEMINI_TEXT_PRICING_PER_MTOK_USD["gemini-2.5-flash"])
    prompt_tokens = estimate_text_tokens(prompt_text)
    response_tokens = estimate_text_tokens(response_text)
    usd = (prompt_tokens / 1_000_000.0) * in_price + (response_tokens / 1_000_000.0) * out_price
    return {
        "kind": "gemini_text",
        "model": model_id or "gemini-2.5-flash",
        "prompt_tokens_est": prompt_tokens,
        "response_tokens_est": response_tokens,
        "cost_usd": round(usd, 6),
    }


def estimate_image_generation_cost_usd(model: str) -> dict[str, Any]:
    """Estimate one Vertex-generated image cost by model tier."""
    model_id = (model or "").strip()
    usd = _IMAGE_GENERATION_PRICE_USD.get(model_id, 0.04)
    return {
        "kind": "vertex_image",
        "model": model_id or "unknown",
        "cost_usd": round(float(usd), 6),
    }


def _scene_cost_bucket(scene: dict[str, Any]) -> dict[str, Any]:
    bucket = scene.get("media_costs")
    if not isinstance(bucket, dict):
        bucket = {
            "shared_cost_usd": 0.0,
            "per_language_cost_usd": {},
            "events": [],
        }
        scene["media_costs"] = bucket
    bucket.setdefault("shared_cost_usd", 0.0)
    bucket.setdefault("per_language_cost_usd", {})
    bucket.setdefault("events", [])
    return bucket


def add_scene_shared_cost(scene: dict[str, Any], event: dict[str, Any]) -> None:
    """Add a cost shared by all language videos of the scene."""
    bucket = _scene_cost_bucket(scene)
    usd = float(event.get("cost_usd") or 0.0)
    bucket["shared_cost_usd"] = round(float(bucket.get("shared_cost_usd") or 0.0) + usd, 6)
    events = bucket.get("events")
    if isinstance(events, list):
        events.append({**event, "scope": "shared"})


def add_scene_language_cost(scene: dict[str, Any], language: str, event: dict[str, Any]) -> None:
    """Add a cost that belongs only to one output language video."""
    bucket = _scene_cost_bucket(scene)
    per_lang = bucket.get("per_language_cost_usd")
    if not isinstance(per_lang, dict):
        per_lang = {}
        bucket["per_language_cost_usd"] = per_lang
    usd = float(event.get("cost_usd") or 0.0)
    lang = str(language or "ru")
    per_lang[lang] = round(float(per_lang.get(lang) or 0.0) + usd, 6)
    events = bucket.get("events")
    if isinstance(events, list):
        events.append({**event, "scope": "language", "language": lang})


def estimated_scene_video_cost(scene: dict[str, Any], language: str, total_languages: int) -> dict[str, Any]:
    """Return estimated cost for one concrete scene-language video."""
    bucket = _scene_cost_bucket(scene)
    langs = max(1, int(total_languages or 1))
    shared_total = float(bucket.get("shared_cost_usd") or 0.0)
    per_lang = bucket.get("per_language_cost_usd")
    language_total = 0.0
    if isinstance(per_lang, dict):
        language_total = float(per_lang.get(str(language or "ru")) or 0.0)
    total = shared_total / langs + language_total
    return {
        "language": str(language or "ru"),
        "shared_cost_usd_total": round(shared_total, 6),
        "shared_cost_usd_allocated": round(shared_total / langs, 6),
        "language_cost_usd": round(language_total, 6),
        "estimated_total_cost_usd": round(total, 6),
        "events": list(bucket.get("events") or []),
    }


def estimated_session_total_media_cost(session: dict[str, Any]) -> float:
    """Sum all shared and per-language media-side costs currently tracked in scenes."""
    total = 0.0
    for scene in session.get("scenes_plan") or []:
        if not isinstance(scene, dict):
            continue
        bucket = scene.get("media_costs")
        if not isinstance(bucket, dict):
            continue
        total += float(bucket.get("shared_cost_usd") or 0.0)
        per_lang = bucket.get("per_language_cost_usd")
        if isinstance(per_lang, dict):
            total += sum(float(v or 0.0) for v in per_lang.values())
    return round(total, 6)
