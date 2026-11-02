"""Guest cartoon helpers shared by the bot and final-build worker."""

from __future__ import annotations

from typing import Any


def approved_guest_cartoon_scenes(scenes: object) -> list[dict[str, Any]]:
    """Return only scenes containing at least one production-approved video."""
    if not isinstance(scenes, list):
        return []
    approved_scenes: list[dict[str, Any]] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        candidates_by_lang = scene.get("video_candidates_by_lang")
        if not isinstance(candidates_by_lang, dict):
            continue
        has_approved_video = any(
            bool(candidate.get("approved_for_production"))
            for candidates in candidates_by_lang.values()
            if isinstance(candidates, list)
            for candidate in candidates
            if isinstance(candidate, dict)
        )
        if has_approved_video:
            approved_scenes.append(scene)
    return approved_scenes
