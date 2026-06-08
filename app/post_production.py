"""Final production render helpers for merged vertical masters."""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .ai_approval_service import _resolve_ffmpeg
from .db import SessionLocal
from .models import Project
from .scene_media_candidates import (
    DEFAULT_SCENE_VIDEO_SPEED_COEF,
    parse_speed_coef,
    resolve_scene_video_for_production,
)

logger = logging.getLogger("bot.video")

DEFAULT_BACKGROUND_MUSIC_VOLUME = 0.18
BACKGROUND_MUSIC_ATTENUATION_DB = 15.0
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
SUBTITLE_MAX_LINES = 2
SUBTITLE_ASS_INVISIBLE_TOKEN = "__ASS_INVISIBLE_DOT__"
SUBTITLE_SHORT_FIRST_WORD_LEN = 2
SUBTITLE_SHORT_WORD_LEN = 3
DEFAULT_BACKGROUND_MUSIC_FALLBACKS = (
    "/Users/anastasia/Downloads/Cooking-Show-chosic.com_.mp3",
)


def _encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _apply_db_attenuation(volume: float, db: float) -> float:
    safe = max(0.0, float(volume))
    return safe * (10 ** (-float(db) / 20.0))


def _resolve_ffprobe() -> str | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        return None
    candidate = str(Path(ffmpeg).with_name("ffprobe"))
    return candidate if os.path.exists(candidate) else None


def _video_dimensions(path: str | os.PathLike[str]) -> tuple[int | None, int | None]:
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        return None, None
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None, None
        item = streams[0]
        return int(item.get("width") or 0) or None, int(item.get("height") or 0) or None
    except Exception:
        logger.exception("post_production.video_dimensions_failed path=%s", path)
        return None, None


def _has_audio_stream(path: str | os.PathLike[str]) -> bool:
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        return False
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        return bool(streams)
    except Exception:
        logger.exception("post_production.audio_probe_failed path=%s", path)
        return False


def probe_video_bytes_dimensions(video_bytes: bytes) -> tuple[int | None, int | None]:
    with tempfile.TemporaryDirectory(prefix="post_production_probe_") as tmp:
        path = Path(tmp) / "probe.mp4"
        path.write_bytes(video_bytes)
        return _video_dimensions(path)


def _subtitle_timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(millis, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, rem = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{rem:03d}"


def _normalize_subtitle_text(text: str) -> str:
    raw = str(text or "").replace("[пауза]", " ").replace("[ПАУЗА]", " ")
    raw = " ".join(raw.split())
    return raw.strip()


def _subtitle_first_line_capacity(words: list[str]) -> int:
    if not words:
        return 0
    if len(words) >= 2 and len(words[0]) <= SUBTITLE_SHORT_FIRST_WORD_LEN:
        return 2
    return 1


def _subtitle_second_line_capacity(words: list[str]) -> int:
    if not words:
        return 0
    if len(words) <= 3:
        return len(words)
    preview = words[:3]
    if any(len(word) <= SUBTITLE_SHORT_WORD_LEN for word in preview):
        return 3
    return 3


def _format_burned_subtitle_lines(words: list[str]) -> list[str]:
    visible_words = [str(word or "").strip() for word in words if str(word or "").strip()]
    if not visible_words:
        return [SUBTITLE_ASS_INVISIBLE_TOKEN, SUBTITLE_ASS_INVISIBLE_TOKEN]

    first_capacity = _subtitle_first_line_capacity(visible_words)
    first_line_words = visible_words[:first_capacity]
    second_line_words = visible_words[first_capacity:]
    lines = [" ".join(first_line_words)]
    lines.append(" ".join(second_line_words) if second_line_words else SUBTITLE_ASS_INVISIBLE_TOKEN)
    return lines[:SUBTITLE_MAX_LINES]


def _format_burned_subtitle_block(words: list[str]) -> str:
    visible = _format_burned_subtitle_lines(words)
    while len(visible) < SUBTITLE_MAX_LINES:
        visible.append(SUBTITLE_ASS_INVISIBLE_TOKEN)
    return "\n".join(visible)


def _ass_timestamp(seconds: float) -> str:
    centis = max(0, int(round(seconds * 100)))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, rem = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{rem:02d}"


def _escape_ass_text(text: str) -> str:
    escaped = str(text or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    escaped = escaped.replace(SUBTITLE_ASS_INVISIBLE_TOKEN, "{\\alpha&HFF&}.")
    return escaped.replace("\n", r"\N")


def _write_burned_subtitle_ass(path: Path, cues: list[dict[str, Any]]) -> None:
    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {TARGET_WIDTH}",
            f"PlayResY: {TARGET_HEIGHT}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
            "Style: Codex,Comic Sans MS,90,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0.1,0,1,2.6,0,7,72,180,1180,1",
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        ]
    )
    lines = [header]
    for cue in cues:
        text = _escape_ass_text(str(cue.get("text") or " "))
        lines.append(
            "Dialogue: 0,{start},{end},Codex,,0,0,0,,{text}".format(
                start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                text=text,
            )
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _subtitle_pages(text: str) -> list[list[str]]:
    words = _normalize_subtitle_text(text).split()
    if not words:
        return []
    pages: list[list[str]] = []
    idx = 0
    while idx < len(words):
        first_capacity = _subtitle_first_line_capacity(words[idx:])
        second_capacity = _subtitle_second_line_capacity(words[idx + first_capacity :])
        page_size = max(1, first_capacity + second_capacity)
        pages.append(words[idx : idx + page_size])
        idx += page_size
    return pages


def _build_burned_subtitle_cues(item: dict[str, Any]) -> list[dict[str, Any]]:
    start_sec = float(item.get("start_sec") or 0.0)
    end_sec = float(item.get("end_sec") or start_sec)
    pages = _subtitle_pages(str(item.get("text") or ""))
    total_steps = sum(len(page) for page in pages)
    if total_steps <= 0 or end_sec <= start_sec:
        return [{"start_sec": start_sec, "end_sec": end_sec, "text": " "}]

    duration = end_sec - start_sec
    step_duration = duration / float(total_steps)
    cues: list[dict[str, Any]] = []
    step_index = 0
    for page in pages:
        for size in range(1, len(page) + 1):
            cue_start = start_sec + (step_index * step_duration)
            cue_end = end_sec if step_index == total_steps - 1 else start_sec + ((step_index + 1) * step_duration)
            cues.append(
                {
                    "start_sec": cue_start,
                    "end_sec": cue_end,
                    "text": _format_burned_subtitle_block(page[:size]),
                }
            )
            step_index += 1
    return cues


def _scene_subtitle_entries(
    scene: dict[str, Any],
    language: str,
    *,
    start_offset: float,
    fallback_duration: float,
    speed_coef: float,
) -> list[dict[str, Any]]:
    cues_map = scene.get("speech_cues_by_lang")
    raw_cues = cues_map.get(language) if isinstance(cues_map, dict) else None
    entries: list[dict[str, Any]] = []
    safe_speed = max(0.01, float(speed_coef or 1.0))
    if isinstance(raw_cues, list):
        for cue in raw_cues:
            if not isinstance(cue, dict):
                continue
            text = _normalize_subtitle_text(str(cue.get("text") or ""))
            start_sec = float(cue.get("start_sec") or 0.0) / safe_speed
            end_sec = float(cue.get("end_sec") or float(cue.get("start_sec") or 0.0)) / safe_speed
            entries.append(
                {
                    "scene": int(scene.get("scene") or 0) or None,
                    "language": language,
                    "text": text,
                    "start_sec": start_offset + start_sec,
                    "end_sec": start_offset + end_sec,
                }
            )
    if entries:
        return entries
    text = _normalize_subtitle_text(
        str((scene.get("speech_by_lang") or {}).get(language) or scene.get("text") or "").strip()
    )
    return [
        {
            "scene": int(scene.get("scene") or 0) or None,
            "language": language,
            "text": text,
            "start_sec": start_offset,
            "end_sec": start_offset + fallback_duration,
        }
    ]


def _run_ffmpeg(cmd: list[str], *, timeout: int = 180) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if isinstance(exc.stderr, bytes) else str(exc.stderr)
        raise RuntimeError(f"ffmpeg failed: {stderr[:1200]}") from exc


def _atempo_filter(speed_coef: float) -> str:
    remaining = max(0.01, float(speed_coef))
    factors: list[float] = []
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.5f}" for factor in factors)


def _normalize_segment(
    *,
    ffmpeg: str,
    src_path: Path,
    dest_path: Path,
    speed_coef: float,
) -> None:
    vf = (
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_WIDTH}:{TARGET_HEIGHT},setpts=PTS/{speed_coef:.6f}"
    )
    af = _atempo_filter(speed_coef)
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src_path),
        "-vf",
        vf,
        "-af",
        af,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(dest_path),
    ]
    _run_ffmpeg(cmd, timeout=300)


def _concat_segments(*, ffmpeg: str, list_path: Path, dest_path: Path) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(dest_path),
    ]
    _run_ffmpeg(cmd, timeout=300)


def _render_subtitled_music_master(
    *,
    ffmpeg: str,
    clean_path: Path,
    subtitles_path: Path,
    music_path: str | None,
    music_volume: float,
    dest_path: Path,
) -> None:
    subtitle_filter = f"subtitles={shlex.quote(str(subtitles_path))}"
    effective_music_volume = _apply_db_attenuation(music_volume, BACKGROUND_MUSIC_ATTENUATION_DB)
    has_source_audio = _has_audio_stream(clean_path)
    if music_path:
        if has_source_audio:
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(clean_path),
                "-stream_loop",
                "-1",
                "-i",
                str(music_path),
                "-filter_complex",
                f"[0:v]{subtitle_filter}[v];[1:a]volume={effective_music_volume:.4f}[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(dest_path),
            ]
            _run_ffmpeg(cmd, timeout=300)
            return
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(clean_path),
            "-stream_loop",
            "-1",
                "-i",
                str(music_path),
                "-filter_complex",
                f"[0:v]{subtitle_filter}[v];[1:a]volume={effective_music_volume:.4f}[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(dest_path),
        ]
        _run_ffmpeg(cmd, timeout=300)
        return
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(clean_path),
        "-vf",
        subtitle_filter,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dest_path),
    ]
    _run_ffmpeg(cmd, timeout=300)


def load_project_media_config(project_slug: str | None) -> dict[str, Any]:
    slug = str(project_slug or "").strip()
    if not slug:
        return {}
    try:
        with SessionLocal() as session:
            project = session.scalar(select(Project).where(Project.slug == slug))
            if project is None:
                return {}
            return {
                "background_music_asset_uri": project.background_music_asset_uri,
                "background_music_volume": project.background_music_volume,
            }
    except Exception:
        logger.exception("post_production.load_project_media_config_failed slug=%s", slug)
        return {}


def _resolve_music_path(project_slug: str | None) -> tuple[str | None, float]:
    cfg = load_project_media_config(project_slug)
    music_uri = str(cfg.get("background_music_asset_uri") or "").strip() or None
    volume = parse_speed_coef(cfg.get("background_music_volume"))
    if volume is None:
        volume = DEFAULT_BACKGROUND_MUSIC_VOLUME
    if music_uri and not os.path.isabs(music_uri):
        music_uri = str((Path(__file__).resolve().parent.parent / music_uri).resolve())
    if music_uri and not os.path.exists(music_uri):
        logger.warning("post_production.music_file_missing path=%s", music_uri)
        music_uri = None
    if music_uri is None:
        for candidate in DEFAULT_BACKGROUND_MUSIC_FALLBACKS:
            if os.path.exists(candidate):
                music_uri = candidate
                break
    return music_uri, float(volume)


def _production_languages(scene: dict[str, Any]) -> list[str]:
    raw = scene.get("video_candidates_by_lang")
    out: list[str] = []
    if isinstance(raw, dict):
        for key in raw:
            code = str(key).strip().lower()
            if code and code not in out:
                out.append(code)
    legacy = scene.get("video_results_by_lang")
    if isinstance(legacy, dict):
        for key in legacy:
            code = str(key).strip().lower()
            if code and code not in out:
                out.append(code)
    return out or ["ru"]


def production_languages_for_scenes(scenes: list[dict[str, Any]]) -> list[str]:
    union: list[str] = []
    for scene in scenes:
        for language in _production_languages(scene):
            if language not in union:
                union.append(language)
    ready: list[str] = []
    for language in union:
        if all(resolve_scene_video_for_production(scene, language) is not None for scene in scenes):
            ready.append(language)
    return ready


def build_post_production(
    scenes: list[dict[str, Any]],
    *,
    project_slug: str | None = None,
) -> dict[str, Any]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    languages = production_languages_for_scenes(scenes)
    if not languages:
        raise RuntimeError("No complete language tracks are available for final production.")

    music_path, music_volume = _resolve_music_path(project_slug)
    renders_by_language: dict[str, dict[str, Any]] = {}
    subtitle_entries_by_language: dict[str, list[dict[str, Any]]] = {}
    original_audio_preserved_by_language: dict[str, bool] = {}

    for language in languages:
        selected_segments: list[dict[str, Any]] = []
        subtitle_entries: list[dict[str, Any]] = []
        current_start = 0.0
        for scene_index, scene in enumerate(scenes):
            selected = resolve_scene_video_for_production(scene, language)
            if selected is None:
                continue
            video_b64 = str(selected.get("video_b64") or "").strip()
            if not video_b64:
                continue
            try:
                video_bytes = base64.b64decode(video_b64)
            except Exception:
                continue
            speed_coef = parse_speed_coef(selected.get("speed_coef")) or DEFAULT_SCENE_VIDEO_SPEED_COEF
            source_duration = float(scene.get("duration_sec") or 0) or 0.0
            final_duration = source_duration / speed_coef if source_duration > 0 else 0.0
            scene_entries = _scene_subtitle_entries(
                scene,
                language,
                start_offset=current_start,
                fallback_duration=final_duration,
                speed_coef=speed_coef,
            )
            for entry in scene_entries:
                entry["scene"] = scene_index + 1
                entry["speed_coef"] = speed_coef
            subtitle_entries.extend(scene_entries)
            current_start += final_duration
            selected_segments.append(
                {
                    "scene_index": scene_index,
                    "language": language,
                    "video_bytes": video_bytes,
                    "speed_coef": speed_coef,
                }
            )
        if not selected_segments:
            continue
        subtitle_entries_by_language[language] = subtitle_entries

        with tempfile.TemporaryDirectory(prefix=f"post_production_{language}_") as tmp:
            tmp_path = Path(tmp)
            normalized_paths: list[Path] = []
            for item in selected_segments:
                src_path = tmp_path / f"scene_{item['scene_index'] + 1}_{item['language']}.mp4"
                src_path.write_bytes(item["video_bytes"])
                width, height = _video_dimensions(src_path)
                logger.info(
                    "post_production.input_dimensions scene=%s lang=%s width=%s height=%s",
                    item["scene_index"] + 1,
                    item["language"],
                    width,
                    height,
                )
                normalized_path = tmp_path / f"scene_{item['scene_index'] + 1}_{item['language']}_norm.mp4"
                _normalize_segment(
                    ffmpeg=ffmpeg,
                    src_path=src_path,
                    dest_path=normalized_path,
                    speed_coef=float(item["speed_coef"]),
                )
                normalized_paths.append(normalized_path)

            concat_list = tmp_path / "concat.txt"
            concat_list.write_text(
                "\n".join(f"file '{path.as_posix()}'" for path in normalized_paths),
                encoding="utf-8",
            )
            clean_path = tmp_path / f"final_clean_master_{language}.mp4"
            _concat_segments(ffmpeg=ffmpeg, list_path=concat_list, dest_path=clean_path)

            ass_path = tmp_path / f"subtitles_{language}.ass"
            subtitle_cues: list[dict[str, Any]] = []
            for item in subtitle_entries:
                subtitle_cues.extend(_build_burned_subtitle_cues(item))
            _write_burned_subtitle_ass(ass_path, subtitle_cues)

            subtitled_path = tmp_path / f"final_subtitled_music_master_{language}.mp4"
            _render_subtitled_music_master(
                ffmpeg=ffmpeg,
                clean_path=clean_path,
                subtitles_path=ass_path,
                music_path=music_path,
                music_volume=music_volume,
                dest_path=subtitled_path,
            )

            clean_bytes = clean_path.read_bytes()
            subtitled_bytes = subtitled_path.read_bytes()
            clean_w, clean_h = _video_dimensions(clean_path)
            sub_w, sub_h = _video_dimensions(subtitled_path)
            original_audio_preserved = _has_audio_stream(clean_path) and _has_audio_stream(subtitled_path)
            original_audio_preserved_by_language[language] = original_audio_preserved
            renders_by_language[language] = {
                "clean": {
                    "video_b64": _encode_b64(clean_bytes),
                    "width": clean_w,
                    "height": clean_h,
                    "source_audio_preserved": original_audio_preserved,
                },
                "subtitled_music": {
                    "video_b64": _encode_b64(subtitled_bytes),
                    "width": sub_w,
                    "height": sub_h,
                    "music_asset_uri": music_path,
                    "music_volume": music_volume,
                    "source_audio_preserved": original_audio_preserved,
                },
            }

    if not renders_by_language:
        raise RuntimeError("No final renders were produced for any language.")

    first_lang = next(iter(renders_by_language))
    speech_budget_by_language: dict[str, list[dict[str, Any]]] = {}
    for language in renders_by_language:
        speech_budget_by_language[language] = []
        for scene in scenes:
            meta_map = scene.get("speech_budget_by_lang")
            if isinstance(meta_map, dict) and isinstance(meta_map.get(language), dict):
                speech_budget_by_language[language].append(dict(meta_map[language]))
    return {
        "final_clean_video": renders_by_language[first_lang]["clean"],
        "final_subtitled_music_video": renders_by_language[first_lang]["subtitled_music"],
        "renders_by_language": renders_by_language,
        "subtitle_entries": subtitle_entries_by_language.get(first_lang, []),
        "subtitle_entries_by_language": subtitle_entries_by_language,
        "speech_budget_by_language": speech_budget_by_language,
        "original_audio_preserved_by_language": original_audio_preserved_by_language,
        "music_asset_uri": music_path,
        "languages": list(renders_by_language.keys()),
    }
