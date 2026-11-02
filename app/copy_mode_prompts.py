"""Prompt rules for the video-copy generation mode."""

from __future__ import annotations

import re
from typing import Any

COPY_MODE_TIME_RANGE_EXAMPLE = "1.00-2.30 sec"

COPY_FAMILY_ROLES = ("granddaughter", "grandson", "grandfather", "daughter")


def copy_mode_family_roles(timeline: list[dict[str, Any]]) -> list[str]:
    roles = set()
    for entry in timeline:
        text = " ".join(str(entry.get(key) or "") for key in ("action", "hands_or_people", "must_preserve"))
        patterns = {
            "granddaughter": r"\bgranddaughter\b|\bвнучк\w*",
            "grandson": r"\bgrandson\b|\bвнук\w*",
            "grandfather": r"\bgrandfather\b|\bgrandpa\b|\bдедушк\w*",
            "daughter": r"\bdaughter\b|\bдочь\b|\bдоч(?:к|ер)\w*",
        }
        for role, pattern in patterns.items():
            if re.search(pattern, text, re.I):
                roles.add(role)
        for role in entry.get("character_roles") or []:
            if role in COPY_FAMILY_ROLES:
                roles.add(role)
    return [role for role in COPY_FAMILY_ROLES if role in roles]


COPY_CHARACTER_RULE = (
    "Replace source granddaughter, grandson, grandfather and daughter with the matching project characters "
    "from their attached references, preserving their actions and hands. "
    "Replace other people and unidentified hands with the grandmother."
)

COPY_MODE_PROMPT_RULES = (
    "Output only the content prompt for the video generator. "
    "Do not include model, aspect ratio, resolution, duration, quality, or provider names; those are set in UI params. "
    "Do not mention or attach the original source video as a reference; use the source analysis only as text timing instructions. "
    "Attach a video reference only for continuation chunks after the first chunk, and only the latest approved generated chunk. "
    "Use explicit second ranges formatted like `0.00-1.00 sec` or `1.00-2.30 sec`, not minutes, timestamps, or milliseconds. "
    "Do not shorten, summarize, or merge distinct cooking actions; preserve the chunk's meaningful source timing ranges. "
    "Collapse only consecutive source text/logo/watermark/end-screen replacement moments into one clean continuation range. "
    "Each timing line must describe the visible action, ingredients, cookware/props, camera movement, grandmother replacement, and cat placement. "
    "Preserve visible weather, season, time of day, and indoor lighting atmosphere. "
    "Every scene must include the orange cat. " + COPY_CHARACTER_RULE + " "
    "Never recreate Instagram marks, readable text, captions, usernames, watermarks, logos, social media end screens, or black logo screens. "
    "If text/logo/watermark is only an overlay on a cooking action, preserve the cooking action and remove only the overlay. "
    "Replace the whole moment with a clean continuation only when the source moment is a logo/end-screen/black-screen shot."
)


_MMSS_RANGE_RE = re.compile(
    r"^\s*(?P<sm>\d{1,2}):(?P<ss>\d{2})(?:\.(?P<sms>\d+))?\s*-\s*"
    r"(?P<em>\d{1,2}):(?P<es>\d{2})(?:\.(?P<ems>\d+))?\s*$"
)


def format_copy_mode_seconds_range(start_sec: float, end_sec: float) -> str:
    """Return a Seedance prompt timing range in explicit seconds."""

    start = max(0.0, float(start_sec))
    end = max(start, float(end_sec))
    return f"{start:.2f}-{end:.2f} sec"


def build_copy_mode_seedance_prompt(
    timeline: list[dict[str, Any]],
    *,
    chunk_start_sec: float = 0.0,
    continuation: bool = False,
    weather_atmosphere: str = "",
    user_feedback: str = "",
) -> str:
    """Build a ready-to-send content prompt from Gemini's variable-length timing ranges."""

    weather = _clean_text(weather_atmosphere)
    feedback = _clean_text(user_feedback)
    lines = [
        "Follow this timing plan exactly. The original source video is not attached as a reference.",
        "Time format: seconds, for example 1.00-2.30 sec.",
        "Never show Instagram marks, readable text, captions, usernames, watermarks, logos, social media end screens, or black logo screens.",
    ]
    if weather:
        lines.append(f"Preserve weather and atmosphere: {weather}.")
    if feedback:
        lines.append(f"User corrections to apply in every timing range: {feedback}.")
    if continuation:
        lines.append("This segment continues the previously generated video; keep motion, characters, kitchen, lighting, and food state continuous.")
    lines.append(COPY_CHARACTER_RULE)
    roles = copy_mode_family_roles(timeline)
    if roles:
        lines.append("Family character references for this segment: " + ", ".join(roles) + ".")
    lines.append("")

    for entry in _coalesced_prompt_entries(timeline):
        start = float(entry["start_sec"])
        end = float(entry["end_sec"])
        label = format_copy_mode_seconds_range(start - chunk_start_sec, end - chunk_start_sec)
        lines.append(f"{label}: {entry['text']}")

    lines.append("")
    lines.append(
        "Orange cat must be visible in every shot. "
        "Preserve ingredient order, cookware, warm wooden kitchen, autumn decor, smooth camera movement, and cozy cartoon style. "
        "Remove all readable text, captions, usernames, watermarks, logos, social media end screens, and black logo screens."
    )
    return "\n".join(line.rstrip() for line in lines).strip()


def _entry_seconds_range(item: dict[str, Any]) -> tuple[float, float]:
    if "start_sec" in item and "end_sec" in item:
        return float(item["start_sec"]), float(item["end_sec"])
    parsed = _parse_mmss_range(item.get("time_range"))
    if parsed is not None:
        return parsed
    start = float(item.get("second") or 0)
    duration = float(item.get("duration_sec") or 1)
    return start, start + max(0.01, duration)


def _coalesced_prompt_entries(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    pending_clean: dict[str, Any] | None = None
    previous_visible_context: dict[str, str] = {}

    for item in timeline:
        start, end = _entry_seconds_range(item)
        forbidden_marker = _entry_is_forbidden_replacement_moment(item)
        if forbidden_marker:
            if pending_clean is None:
                pending_clean = {
                    "start_sec": start,
                    "end_sec": end,
                    "context": dict(previous_visible_context),
                }
            else:
                pending_clean["end_sec"] = end
            continue

        if pending_clean is not None:
            entries.append(_clean_continuation_entry(pending_clean))
            pending_clean = None

        text, visible_context = _normal_prompt_entry_text(item)
        entries.append({"start_sec": start, "end_sec": end, "text": text})
        if visible_context:
            previous_visible_context = visible_context

    if pending_clean is not None:
        entries.append(_clean_continuation_entry(pending_clean))
    return entries


def _normal_prompt_entry_text(item: dict[str, Any]) -> tuple[str, dict[str, str]]:
    action = _clean_text(item.get("action"))
    ingredients = _join_values(item.get("visible_ingredients"))
    props = _join_values(item.get("cookware_props"))
    camera = _clean_text(item.get("camera"))
    weather = _entry_weather_text(item)
    grandma = _clean_text(item.get("replace_with_grandma"))
    cat = _clean_text(item.get("cat_placement_hint"))
    details = [action]
    if ingredients:
        details.append(f"Ingredients: {ingredients}.")
    if props:
        details.append(f"Cookware/props: {props}.")
    if camera:
        details.append(f"Camera: {camera}.")
    if weather:
        details.append(f"Weather/atmosphere: {weather}.")
    roles = copy_mode_family_roles([item])
    if roles:
        details.append("Use matching project characters and their hands: " + ", ".join(roles) + ".")
    elif grandma and grandma.lower() not in {"false", "no", "none"}:
        details.append("Replace visible hands or people with the grandmother.")
    if cat:
        details.append(f"Cat: {cat}.")
    return " ".join(details).strip(), {
        "ingredients": ingredients,
        "props": props,
        "camera": camera,
        "weather": weather,
    }


def _clean_continuation_entry(entry: dict[str, Any]) -> dict[str, Any]:
    context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
    details = [
        "Clean continuation of the previous cooking visual; remove the source text/logo/watermark/end-screen interval "
        "and do not recreate any readable text, username, social media mark, watermark, logo, or black logo screen."
    ]
    ingredients = _clean_text(context.get("ingredients"))
    props = _clean_text(context.get("props"))
    camera = _clean_text(context.get("camera"))
    weather = _clean_text(context.get("weather"))
    if ingredients:
        details.append(f"Keep ingredients visible from the previous beat: {ingredients}.")
    if props:
        details.append(f"Keep cookware/props visible from the previous beat: {props}.")
    if camera:
        details.append(f"Camera continues from the previous beat: {camera}.")
    if weather:
        details.append(f"Keep weather/atmosphere from the previous beat: {weather}.")
    details.append(COPY_CHARACTER_RULE)
    details.append("Cat: orange cat remains clearly visible in the same cozy kitchen continuity.")
    return {
        "start_sec": float(entry["start_sec"]),
        "end_sec": float(entry["end_sec"]),
        "text": " ".join(details).strip(),
    }


def _parse_mmss_range(value: Any) -> tuple[float, float] | None:
    match = _MMSS_RANGE_RE.match(str(value or ""))
    if not match:
        return None
    start = int(match.group("sm")) * 60 + int(match.group("ss"))
    end = int(match.group("em")) * 60 + int(match.group("es"))
    if match.group("sms"):
        start += float(f"0.{match.group('sms')}")
    if match.group("ems"):
        end += float(f"0.{match.group('ems')}")
    return float(start), float(end)


def _join_values(value: Any) -> str:
    if not isinstance(value, list):
        return _clean_text(value)
    return ", ".join(_clean_text(item) for item in value if _clean_text(item))


def _entry_weather_text(item: dict[str, Any]) -> str:
    for key in ("weather_atmosphere", "weather", "atmosphere", "lighting_mood"):
        text = _clean_text(item.get(key))
        if text:
            return text
    return ""


def _entry_is_forbidden_replacement_moment(item: dict[str, Any]) -> bool:
    text = " ".join(
        _clean_text(value)
        for value in (
            item.get("action"),
            item.get("camera"),
            item.get("hands_or_people"),
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "end screen",
            "black screen",
            "logo screen",
            "social media screen",
            "social media end",
            "instagram logo",
            "logo appears",
            "username appears",
            "watermark appears",
        )
    )


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
