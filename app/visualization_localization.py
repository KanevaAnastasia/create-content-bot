"""Localize an approved visualization master without changing its audio/edit."""

from __future__ import annotations

import math
import re
import shutil
import uuid
from pathlib import Path

from app import post_production as pp


VARIANTS = {"clean", "upscaled_4k", "subtitled_music", "titled_subtitled_music"}


def translated_cues(segments: list[dict], *, duration: float, language: str) -> list[dict]:
    """Keep source phrase boundaries; do not pretend translated words were spoken.

    A translation can contain a different number/order of words. Showing each
    whole translated phrase at its original interval avoids fabricated RU word
    alignment while preserving the original delivery and pauses.
    """
    if not segments or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Visualization translation needs timed source phrases.")
    cues = []
    previous_end = 0.0
    for segment in segments:
        start, end = float(segment["start_sec"]), float(segment["end_sec"])
        text = str(segment.get("text") or "").strip()
        if not all(math.isfinite(x) for x in (start, end)) or not (
            previous_end <= start < end <= duration + 0.001
        ):
            raise ValueError("Translated phrases must use ordered, non-overlapping source intervals.")
        if not text or (language == "ru" and not re.search("[А-Яа-яЁё]", text)):
            raise ValueError("Russian visualization subtitles need a Russian translation.")
        cues.append({"start_sec": start, "end_sec": end, "text": text, "preserve_case": True})
        previous_end = end
    return cues


def build_localized_visualization(
    *, clean_path: Path, upscaled_path: Path, language: str,
    segments: list[dict], duration: float, title: str,
    title_duration: float, variants: list[str], progress_callback=None,
) -> dict:
    if language != "ru":
        raise ValueError("This localization path is for Russian translated subtitles only.")
    if not variants or not set(variants) <= VARIANTS:
        raise ValueError("Unknown visualization render variant.")
    entries = translated_cues(segments, duration=duration, language=language)
    for path in (clean_path, upscaled_path):
        if not path.is_file():
            raise ValueError(f"Approved visualization master is missing: {path.name}")
        if abs(pp._media_duration(path) - duration) > 0.1:
            raise ValueError("Source master duration changed; review subtitle timing before rendering.")
    if "titled_subtitled_music" in variants and (
        not re.search("[А-Яа-яЁё]", title) or not 0 < title_duration <= duration
    ):
        raise ValueError("A Russian title and valid title duration are required.")
    ffmpeg = pp._resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    bundle = f"visualization_localized_{uuid.uuid4().hex}"
    output = pp.post_production_artifacts_dir() / bundle
    output.mkdir(parents=True, exist_ok=False)
    renders = {}
    for variant in variants:
        source = clean_path if variant == "clean" else upscaled_path
        dest = output / f"visualization_{variant}_{language}.mp4"
        width, height = pp._video_dimensions(source)
        if progress_callback:
            progress_callback({"language": language, "stage": "subtitles_building",
                               "label": "Original audio/edit + translated source-timed captions"})
        if variant in {"clean", "upscaled_4k"}:
            shutil.copyfile(source, dest)
        else:
            cues = []
            for entry in entries:
                cues.extend(pp._build_burned_subtitle_cues(entry, video_width=width, video_height=height))
            title_cues = []
            if variant == "titled_subtitled_music":
                title_cues = [pp._build_title_overlay_cue(
                    {language: title}, language=language, duration_sec=title_duration,
                )]
            ass = output / f"{variant}_{language}.ass"
            pp._write_burned_subtitle_ass(
                ass, cues, title_cues=title_cues, title_style={"title_color": "#fff7e8"},
                video_width=width, video_height=height,
            )
            pp._render_subtitled_music_master(
                ffmpeg=ffmpeg, clean_path=source, subtitles_path=ass,
                music_path=None, music_volume=0.0, dest_path=dest,
            )
        if abs(pp._media_duration(dest) - duration) > 0.1:
            raise RuntimeError("Localized visualization duration does not match its source.")
        renders[variant] = {
            "video_path": str(dest), "width": width, "height": height,
            "source_variant": "clean" if variant == "clean" else "upscaled_4k",
            "source_video_path": str(source), "source_audio_preserved": True,
            "background_music_included": False,
            "subtitle_fingerprint": pp.subtitle_entries_fingerprint(entries),
        }
    return {
        "languages": [language], "renders_by_language": {language: renders},
        "artifact_bundle_key": bundle, "subtitle_entries_by_language": {language: entries},
        "original_audio_preserved_by_language": {language: True},
    }
