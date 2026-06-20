"""Final production render helpers for merged vertical masters."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
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
BACKGROUND_MUSIC_ATTENUATION_DB = 3.0
SOURCE_AUDIO_VOLUME = 1.0
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
FFPROBE_TIMEOUT_SEC = 60
FFMPEG_SHORT_TIMEOUT_SEC = 300
FFMPEG_RENDER_TIMEOUT_SEC = 900
SUBTITLE_MAX_LINES = 2
SUBTITLE_ASS_INVISIBLE_TOKEN = "__ASS_INVISIBLE_DOT__"
SUBTITLE_MAX_WORDS_PER_SECOND = 2.6
SUBTITLE_MIN_PAGE_DURATION_SEC = 1.4
SUBTITLE_STYLE_NAME = "CleanPremium"
SUBTITLE_PRIMARY_FONT = "Inter SemiBold"
SUBTITLE_FALLBACK_FONT = "Noto Sans"
SUBTITLE_VERTICAL_FONT_SIZE = 86
SUBTITLE_VERTICAL_MARGIN_V = 420
SUBTITLE_VERTICAL_LINE_CHARS = 20
SUBTITLE_HORIZONTAL_FONT_SIZE = 67
SUBTITLE_HORIZONTAL_MARGIN_V = 145
SUBTITLE_HORIZONTAL_LINE_CHARS = 27
SUBTITLE_SQUARE_FONT_SIZE = 75
SUBTITLE_SQUARE_MARGIN_V = 145
SUBTITLE_SQUARE_LINE_CHARS = 23
SUBTITLE_OUTLINE = 2
SUBTITLE_SHADOW = 5
SUBTITLE_OUTLINE_COLOUR = "&H80000000"
SUBTITLE_SHADOW_COLOUR = "&H40000000"
SUBTITLE_FONTS_DIR_ENV_NAME = "SUBTITLE_FONTS_DIR"
COMBINING_ACCENT_MARKS_RE = re.compile(r"[\u0300-\u036f]")
CYRILLIC_WORD_RE = re.compile(r"[А-Яа-яЁё]+(?:[А-Яа-яЁё]+)?")
DEFAULT_BACKGROUND_MUSIC_FALLBACKS = (
    "/Users/anastasia/Downloads/Cooking-Show-chosic.com_.mp3",
)
POST_PRODUCTION_ARTIFACTS_DIR_ENV_NAME = "POST_PRODUCTION_ARTIFACTS_DIR"
DEFAULT_POST_PRODUCTION_ARTIFACTS_DIR = "artifacts/post_production"
FFMPEG_STREAM_RE = re.compile(r"Stream #\d+:\d+(?:\[[^\]]+\])?(?:\([^)]*\))?:\s*([^:]+):")
FFMPEG_VIDEO_SIZE_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


@dataclass(frozen=True, slots=True)
class SubtitleLayout:
    width: int
    height: int
    font_size: int
    margin_l: int
    margin_r: int
    margin_v: int
    line_chars: int


def _encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def subtitle_fonts_dir() -> Path:
    raw = (os.getenv(SUBTITLE_FONTS_DIR_ENV_NAME) or "").strip()
    fonts_dir = Path(raw) if raw else _project_root() / "fonts"
    if not fonts_dir.is_absolute():
        fonts_dir = (_project_root() / fonts_dir).resolve()
    return fonts_dir


def _subtitle_font_name(fonts_dir: Path | None = None) -> str:
    fonts_path = fonts_dir or subtitle_fonts_dir()
    font_files: list[str] = []
    if fonts_path.exists():
        font_files = [
            path.name.lower().replace(" ", "").replace("_", "").replace("-", "")
            for path in fonts_path.iterdir()
            if path.suffix.lower() in {".ttf", ".otf", ".ttc"}
        ]
    if any("inter" in name and "semibold" in name for name in font_files):
        return SUBTITLE_PRIMARY_FONT
    if any("notosans" in name for name in font_files):
        return SUBTITLE_FALLBACK_FONT
    return SUBTITLE_PRIMARY_FONT


def post_production_artifacts_dir() -> Path:
    raw = (os.getenv(POST_PRODUCTION_ARTIFACTS_DIR_ENV_NAME) or "").strip()
    base = Path(raw or DEFAULT_POST_PRODUCTION_ARTIFACTS_DIR)
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent / base
    return base


def _persist_render_artifact(source_path: Path, *, bundle_key: str, filename: str) -> str:
    target_dir = post_production_artifacts_dir() / bundle_key
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    shutil.copyfile(source_path, target_path)
    return str(target_path.resolve())


def _readable_slug(value: str | None, *, fallback: str = "video") -> str:
    raw = str(value or "").strip().lower()
    parts = re.findall(r"[a-z0-9]+", raw)
    slug = "_".join(parts[:6]).strip("_")
    return slug or fallback


def _scene_title_for_artifacts(scenes: list[dict[str, Any]]) -> str:
    for scene in scenes:
        for key in ("recipe_title", "dish_name", "title", "name", "short_description", "text"):
            value = str(scene.get(key) or "").strip()
            if value:
                return value
    return "video"


def _make_artifact_bundle_key(scenes: list[dict[str, Any]], *, project_slug: str | None) -> str:
    readable = _readable_slug(project_slug) if project_slug else _readable_slug(_scene_title_for_artifacts(scenes))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{readable}_{uuid.uuid4().hex[:8]}"


def _artifact_filename(*, project_slug: str | None, kind: str, language: str) -> str:
    prefix = _readable_slug(project_slug, fallback="final")
    lang = _readable_slug(language, fallback="ru")
    return f"{prefix}_{kind}_{lang}.mp4"


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


def _probe_media_with_ffmpeg(path: str | os.PathLike[str]) -> str:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        return ""
    cmd = [ffmpeg, "-hide_banner", "-i", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "post_production.ffmpeg_probe_timeout path=%s timeout=%ss",
            path,
            FFPROBE_TIMEOUT_SEC,
        )
        raise RuntimeError(f"post_production timeout at ffmpeg:probe after {FFPROBE_TIMEOUT_SEC}s") from exc
    except Exception:
        logger.exception("post_production.ffmpeg_probe_failed path=%s", path)
        return ""
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def _video_dimensions(path: str | os.PathLike[str]) -> tuple[int | None, int | None]:
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        probe = _probe_media_with_ffmpeg(path)
        for line in probe.splitlines():
            if " Video:" not in line:
                continue
            match = FFMPEG_VIDEO_SIZE_RE.search(line)
            if match:
                return int(match.group(1)), int(match.group(2))
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
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None, None
        item = streams[0]
        return int(item.get("width") or 0) or None, int(item.get("height") or 0) or None
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "post_production.ffprobe_timeout stage=video_dimensions path=%s timeout=%ss",
            path,
            FFPROBE_TIMEOUT_SEC,
        )
        raise RuntimeError(
            f"post_production timeout at ffprobe:video_dimensions after {FFPROBE_TIMEOUT_SEC}s"
        ) from exc
    except Exception:
        logger.exception("post_production.video_dimensions_failed path=%s", path)
        return None, None


def _has_audio_stream(path: str | os.PathLike[str]) -> bool:
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        probe = _probe_media_with_ffmpeg(path)
        for match in FFMPEG_STREAM_RE.finditer(probe):
            if match.group(1).strip().lower() == "audio":
                return True
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
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        return bool(streams)
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "post_production.ffprobe_timeout stage=audio_probe path=%s timeout=%ss",
            path,
            FFPROBE_TIMEOUT_SEC,
        )
        raise RuntimeError(
            f"post_production timeout at ffprobe:audio_probe after {FFPROBE_TIMEOUT_SEC}s"
        ) from exc
    except Exception:
        logger.exception("post_production.audio_probe_failed path=%s", path)
        return False


def _media_duration(path: str | os.PathLike[str]) -> float | None:
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        probe = _probe_media_with_ffmpeg(path)
        match = FFMPEG_DURATION_RE.search(probe)
        if not match:
            return None
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        duration = hours * 3600 + minutes * 60 + seconds
        return duration if duration > 0 else None
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
        data = json.loads(proc.stdout or "{}")
        duration = float((data.get("format") or {}).get("duration") or 0.0)
        return duration if duration > 0 else None
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "post_production.ffprobe_timeout stage=media_duration path=%s timeout=%ss",
            path,
            FFPROBE_TIMEOUT_SEC,
        )
        raise RuntimeError(f"post_production timeout at ffprobe:media_duration after {FFPROBE_TIMEOUT_SEC}s") from exc
    except Exception:
        logger.exception("post_production.media_duration_failed path=%s", path)
        return None


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
    raw = COMBINING_ACCENT_MARKS_RE.sub("", raw)
    raw = " ".join(raw.split())
    raw = CYRILLIC_WORD_RE.sub(lambda match: _normalize_cyrillic_subtitle_word(match.group(0)), raw)
    return raw.strip()


def _normalize_cyrillic_subtitle_word(word: str) -> str:
    lowered = str(word or "").lower()
    if not lowered:
        return ""
    if word[:1].isupper() and not word[1:].isupper():
        return lowered[:1].upper() + lowered[1:]
    return lowered


def _subtitle_layout_for_dimensions(width: int | None, height: int | None) -> SubtitleLayout:
    safe_width = int(width or TARGET_WIDTH)
    safe_height = int(height or TARGET_HEIGHT)
    ratio = safe_width / max(1, safe_height)
    if ratio < 0.85:
        base_width = 1080
        base_height = 1920
        base_font_size = SUBTITLE_VERTICAL_FONT_SIZE
        base_margin_v = SUBTITLE_VERTICAL_MARGIN_V
        base_line_chars = SUBTITLE_VERTICAL_LINE_CHARS
    elif ratio > 1.15:
        base_width = 1920
        base_height = 1080
        base_font_size = SUBTITLE_HORIZONTAL_FONT_SIZE
        base_margin_v = SUBTITLE_HORIZONTAL_MARGIN_V
        base_line_chars = SUBTITLE_HORIZONTAL_LINE_CHARS
    else:
        base_width = 1080
        base_height = 1080
        base_font_size = SUBTITLE_SQUARE_FONT_SIZE
        base_margin_v = SUBTITLE_SQUARE_MARGIN_V
        base_line_chars = SUBTITLE_SQUARE_LINE_CHARS
    scale = safe_height / float(base_height)
    width_scale = safe_width / float(base_width)
    font_size = max(24, int(round(base_font_size * scale)))
    margin_v = max(36, int(round(base_margin_v * scale)))
    margin_h = max(48, int(round(80 * width_scale)))
    line_chars = max(18, int(round(base_line_chars * width_scale)))
    return SubtitleLayout(
        width=safe_width,
        height=safe_height,
        font_size=font_size,
        margin_l=margin_h,
        margin_r=margin_h,
        margin_v=margin_v,
        line_chars=line_chars,
    )


def _balanced_subtitle_lines(words: list[str], *, max_line_chars: int) -> list[str]:
    visible_words = [str(word or "").strip() for word in words if str(word or "").strip()]
    if not visible_words:
        return []
    full_line = " ".join(visible_words)
    if len(full_line) <= max_line_chars or len(visible_words) == 1:
        return [full_line]

    best: tuple[int, int, int, list[str]] | None = None
    for split_at in range(1, len(visible_words)):
        first = " ".join(visible_words[:split_at])
        second = " ".join(visible_words[split_at:])
        overflow = max(0, len(first) - max_line_chars) + max(0, len(second) - max_line_chars)
        balance = abs(len(first) - len(second))
        score = (overflow, balance, abs(split_at - (len(visible_words) // 2)), [first, second])
        if best is None or score[:3] < best[:3]:
            best = score
    return best[3] if best else [full_line]


def _subtitle_page_fits(words: list[str], *, max_line_chars: int) -> bool:
    lines = _balanced_subtitle_lines(words, max_line_chars=max_line_chars)
    if len(lines) > SUBTITLE_MAX_LINES:
        return False
    return all(len(line) <= max_line_chars for line in lines)


def _format_burned_subtitle_block(words: list[str], *, max_line_chars: int | None = None) -> str:
    layout = _subtitle_layout_for_dimensions(TARGET_WIDTH, TARGET_HEIGHT)
    limit = int(max_line_chars or layout.line_chars)
    visible = _balanced_subtitle_lines(words, max_line_chars=limit)
    if not visible:
        visible = [SUBTITLE_ASS_INVISIBLE_TOKEN]
    return "\n".join(visible[:SUBTITLE_MAX_LINES])


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


def _write_burned_subtitle_ass(
    path: Path,
    cues: list[dict[str, Any]],
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> None:
    layout = _subtitle_layout_for_dimensions(video_width, video_height)
    font_name = _subtitle_font_name()
    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {layout.width}",
            f"PlayResY: {layout.height}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
            (
                f"Style: {SUBTITLE_STYLE_NAME},{font_name},{layout.font_size},"
                f"&H00FFFFFF,&H00FFFFFF,{SUBTITLE_OUTLINE_COLOUR},{SUBTITLE_SHADOW_COLOUR},"
                f"0,0,0,0,100,100,0,0,1,{SUBTITLE_OUTLINE},{SUBTITLE_SHADOW},2,"
                f"{layout.margin_l},{layout.margin_r},{layout.margin_v},1"
            ),
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        ]
    )
    lines = [header]
    for cue in cues:
        text = _escape_ass_text(str(cue.get("text") or " "))
        lines.append(
            "Dialogue: 0,{start},{end},{style},,0,0,0,,{text}".format(
                start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                style=SUBTITLE_STYLE_NAME,
                text=text,
            )
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _subtitle_pages(
    text: str,
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> list[list[str]]:
    words = _normalize_subtitle_text(text).split()
    if not words:
        return []
    layout = _subtitle_layout_for_dimensions(video_width, video_height)
    pages: list[list[str]] = []
    idx = 0
    while idx < len(words):
        page_words: list[str] = []
        while idx < len(words):
            candidate = page_words + [words[idx]]
            if page_words and not _subtitle_page_fits(candidate, max_line_chars=layout.line_chars):
                break
            page_words = candidate
            idx += 1
        if not page_words:
            page_words = [words[idx]]
            idx += 1
        pages.append(page_words)
    return pages


def _expand_dense_subtitle_entry(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one long subtitle entry into page-sized subentries when its time window is too dense."""
    text = _normalize_subtitle_text(str(item.get("text") or ""))
    start_sec = float(item.get("start_sec") or 0.0)
    end_sec = float(item.get("end_sec") or start_sec)
    duration = max(0.0, end_sec - start_sec)
    pages = _subtitle_pages(text)
    if len(pages) <= 1 or duration <= 0:
        return [dict(item, text=text, start_sec=start_sec, end_sec=end_sec)]
    total_words = sum(len(page) for page in pages)
    if total_words <= 0:
        return [dict(item, text=text, start_sec=start_sec, end_sec=end_sec)]
    words_per_sec = total_words / duration if duration > 0 else float("inf")
    page_duration = duration / len(pages)
    if words_per_sec <= SUBTITLE_MAX_WORDS_PER_SECOND and page_duration >= SUBTITLE_MIN_PAGE_DURATION_SEC:
        return [dict(item, text=text, start_sec=start_sec, end_sec=end_sec)]

    weights = [max(1, len(page)) for page in pages]
    total_weight = sum(weights)
    cursor = start_sec
    out: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        share = weights[index] / float(total_weight)
        next_cursor = end_sec if index == len(pages) - 1 else cursor + (duration * share)
        out.append(
            {
                **item,
                "text": " ".join(page).strip(),
                "start_sec": cursor,
                "end_sec": next_cursor,
            }
        )
        cursor = next_cursor
    return out


def _build_burned_subtitle_cues(
    item: dict[str, Any],
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> list[dict[str, Any]]:
    start_sec = float(item.get("start_sec") or 0.0)
    end_sec = float(item.get("end_sec") or start_sec)
    layout = _subtitle_layout_for_dimensions(video_width, video_height)
    pages = _subtitle_pages(str(item.get("text") or ""), video_width=video_width, video_height=video_height)
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
                    "text": _format_burned_subtitle_block(page[:size], max_line_chars=layout.line_chars),
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
    timing_scale: float = 1.0,
) -> list[dict[str, Any]]:
    cues_map = scene.get("speech_cues_by_lang")
    raw_cues = cues_map.get(language) if isinstance(cues_map, dict) else None
    entries: list[dict[str, Any]] = []
    safe_speed = max(0.01, float(speed_coef or 1.0))
    safe_scale = max(0.01, float(timing_scale or 1.0))
    safe_duration = max(0.0, float(fallback_duration or 0.0))
    if isinstance(raw_cues, list):
        for cue in raw_cues:
            if not isinstance(cue, dict):
                continue
            text = _normalize_subtitle_text(str(cue.get("text") or ""))
            start_sec = (float(cue.get("start_sec") or 0.0) / safe_speed) * safe_scale
            end_sec = (
                float(cue.get("end_sec") or float(cue.get("start_sec") or 0.0)) / safe_speed
            ) * safe_scale
            if safe_duration > 0:
                start_sec = min(max(0.0, start_sec), safe_duration)
                end_sec = min(max(start_sec, end_sec), safe_duration)
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
        expanded: list[dict[str, Any]] = []
        for entry in entries:
            expanded.extend(_expand_dense_subtitle_entry(entry))
        return expanded
    text = _normalize_subtitle_text(
        str((scene.get("speech_by_lang") or {}).get(language) or scene.get("text") or "").strip()
    )
    return _expand_dense_subtitle_entry(
        {
            "scene": int(scene.get("scene") or 0) or None,
            "language": language,
            "text": text,
            "start_sec": start_offset,
            "end_sec": start_offset + fallback_duration,
        }
    )


def _run_ffmpeg(cmd: list[str], *, timeout: int = FFMPEG_SHORT_TIMEOUT_SEC, stage: str = "unknown") -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "post_production.ffmpeg_timeout stage=%s timeout=%ss cmd=%s",
            stage,
            timeout,
            " ".join(shlex.quote(part) for part in cmd[:8]),
        )
        raise RuntimeError(f"post_production timeout at ffmpeg:{stage} after {timeout}s") from exc
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
    _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="normalize_segment")


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
    _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="concat_segments")


def _ffmpeg_filter_quote(value: str | os.PathLike[str]) -> str:
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace("'", r"\'")
    return f"'{escaped}'"


def _subtitle_filter(subtitles_path: Path, *, fonts_dir: Path | None = None) -> str:
    resolved_fonts_dir = fonts_dir or subtitle_fonts_dir()
    parts = [f"filename={_ffmpeg_filter_quote(subtitles_path)}"]
    if resolved_fonts_dir.exists():
        parts.append(f"fontsdir={_ffmpeg_filter_quote(resolved_fonts_dir)}")
    else:
        logger.warning("post_production.subtitle_fonts_dir_missing path=%s", resolved_fonts_dir)
    return "subtitles=" + ":".join(parts)


def _render_subtitled_music_master(
    *,
    ffmpeg: str,
    clean_path: Path,
    subtitles_path: Path,
    music_path: str | None,
    music_volume: float,
    dest_path: Path,
) -> bool:
    subtitle_filter = _subtitle_filter(subtitles_path)
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
                f"[0:v]{subtitle_filter}[v];"
                f"[0:a]volume={SOURCE_AUDIO_VOLUME:.4f}[voice];"
                f"[1:a]volume={effective_music_volume:.4f}[music];"
                "[voice][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
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
            _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="render_subtitled_music_master_mix")
            return True
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
        _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="render_subtitled_music_master_music_only")
        return False
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
    _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="render_subtitled_music_master_subtitles_only")
    return has_source_audio and _has_audio_stream(dest_path)


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
    artifact_bundle_key = _make_artifact_bundle_key(scenes, project_slug=project_slug)

    for language in languages:
        selected_segments: list[dict[str, Any]] = []
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
            planned_final_duration = source_duration / speed_coef if source_duration > 0 else 0.0
            selected_segments.append(
                {
                    "scene_index": scene_index,
                    "scene": scene,
                    "language": language,
                    "video_bytes": video_bytes,
                    "speed_coef": speed_coef,
                    "source_duration": source_duration,
                    "planned_final_duration": planned_final_duration,
                }
            )
        if not selected_segments:
            continue

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
                item["actual_duration"] = _media_duration(normalized_path) or float(
                    item["planned_final_duration"] or 0.0
                )
                normalized_paths.append(normalized_path)

            concat_list = tmp_path / "concat.txt"
            concat_list.write_text(
                "\n".join(f"file '{path.as_posix()}'" for path in normalized_paths),
                encoding="utf-8",
            )
            clean_path = tmp_path / f"final_clean_master_{language}.mp4"
            _concat_segments(ffmpeg=ffmpeg, list_path=concat_list, dest_path=clean_path)
            clean_w, clean_h = _video_dimensions(clean_path)

            subtitle_entries: list[dict[str, Any]] = []
            current_start = 0.0
            for item in selected_segments:
                actual_duration = float(item.get("actual_duration") or item.get("planned_final_duration") or 0.0)
                planned_duration = float(item.get("planned_final_duration") or 0.0)
                timing_scale = (actual_duration / planned_duration) if planned_duration > 0 else 1.0
                scene_entries = _scene_subtitle_entries(
                    item["scene"],
                    language,
                    start_offset=current_start,
                    fallback_duration=actual_duration,
                    speed_coef=float(item["speed_coef"]),
                    timing_scale=timing_scale,
                )
                for entry in scene_entries:
                    entry["scene"] = int(item["scene_index"]) + 1
                    entry["speed_coef"] = float(item["speed_coef"])
                    entry["timing_scale"] = timing_scale
                subtitle_entries.extend(scene_entries)
                current_start += actual_duration
            subtitle_entries_by_language[language] = subtitle_entries

            ass_path = tmp_path / f"subtitles_{language}.ass"
            subtitle_cues: list[dict[str, Any]] = []
            for item in subtitle_entries:
                subtitle_cues.extend(
                    _build_burned_subtitle_cues(item, video_width=clean_w, video_height=clean_h)
                )
            _write_burned_subtitle_ass(ass_path, subtitle_cues, video_width=clean_w, video_height=clean_h)

            subtitled_path = tmp_path / f"final_subtitled_music_master_{language}.mp4"
            render_preserved_source_audio = _render_subtitled_music_master(
                ffmpeg=ffmpeg,
                clean_path=clean_path,
                subtitles_path=ass_path,
                music_path=music_path,
                music_volume=music_volume,
                dest_path=subtitled_path,
            )

            clean_bytes = clean_path.read_bytes()
            subtitled_bytes = subtitled_path.read_bytes()
            sub_w, sub_h = _video_dimensions(subtitled_path)
            original_audio_preserved = (
                bool(render_preserved_source_audio)
                if render_preserved_source_audio is not None
                else _has_audio_stream(clean_path) and _has_audio_stream(subtitled_path)
            )
            clean_artifact_path = _persist_render_artifact(
                clean_path,
                bundle_key=artifact_bundle_key,
                filename=_artifact_filename(project_slug=project_slug, kind="clean_master", language=language),
            )
            subtitled_artifact_path = _persist_render_artifact(
                subtitled_path,
                bundle_key=artifact_bundle_key,
                filename=_artifact_filename(project_slug=project_slug, kind="subtitled_music_master", language=language),
            )
            original_audio_preserved_by_language[language] = original_audio_preserved
            renders_by_language[language] = {
                "clean": {
                    "video_b64": _encode_b64(clean_bytes),
                    "video_path": clean_artifact_path,
                    "width": clean_w,
                    "height": clean_h,
                    "source_audio_preserved": original_audio_preserved,
                },
                "subtitled_music": {
                    "video_b64": _encode_b64(subtitled_bytes),
                    "video_path": subtitled_artifact_path,
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
        "artifact_bundle_key": artifact_bundle_key,
    }
