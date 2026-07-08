"""Final production render helpers for merged vertical masters."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
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

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from .ai_approval_service import _resolve_ffmpeg
from .db import SessionLocal
from .models import Project
from .scene_media_candidates import (
    DEFAULT_SCENE_VIDEO_SPEED_COEF,
    parse_speed_coef,
    resolve_latest_approved_scene_video_for_final_build,
)

logger = logging.getLogger("bot.video")

DEFAULT_BACKGROUND_MUSIC_VOLUME = 0.18
BACKGROUND_MUSIC_ATTENUATION_DB = 3.0
SOURCE_AUDIO_VOLUME = 1.0
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
UPSCALED_4K_WIDTH = 2160
UPSCALED_4K_HEIGHT = 3840
FFPROBE_TIMEOUT_SEC = 60
FFMPEG_SHORT_TIMEOUT_SEC = 300
FFMPEG_RENDER_TIMEOUT_SEC = 900
FFMPEG_UPSCALE_TIMEOUT_SEC = 7200
SUBTITLE_MAX_LINES = 2
SUBTITLE_ASS_INVISIBLE_TOKEN = "__ASS_INVISIBLE_DOT__"
SUBTITLE_MAX_WORDS_PER_SECOND = 2.6
SUBTITLE_MIN_PAGE_DURATION_SEC = 1.4
SUBTITLE_STYLE_NAME = "CleanPremium"
SUBTITLE_PRIMARY_FONT = "DIN Condensed"
SUBTITLE_SECONDARY_FONT = "Arial Narrow"
SUBTITLE_FALLBACK_FONT = "TD Afisha"
SUBTITLE_VERTICAL_FONT_SIZE = 88
SUBTITLE_VERTICAL_MARGIN_H = 150
SUBTITLE_VERTICAL_MARGIN_V = 575
SUBTITLE_VERTICAL_LINE_CHARS = 18
SUBTITLE_HORIZONTAL_FONT_SIZE = 78
SUBTITLE_HORIZONTAL_MARGIN_H = 150
SUBTITLE_HORIZONTAL_MARGIN_V = 190
SUBTITLE_HORIZONTAL_LINE_CHARS = 22
SUBTITLE_SQUARE_FONT_SIZE = 82
SUBTITLE_SQUARE_MARGIN_H = 115
SUBTITLE_SQUARE_MARGIN_V = 230
SUBTITLE_SQUARE_LINE_CHARS = 18
SUBTITLE_OUTLINE = 6
SUBTITLE_SHADOW = 3
SUBTITLE_OUTLINE_COLOUR = "&H00000000"
SUBTITLE_SHADOW_COLOUR = "&H70000000"
SUBTITLE_BOLD = -1
SUBTITLE_ALIGNMENT = 1
SUBTITLE_UPPERCASE = True
SUBTITLE_LINE_ADVANCE_RATIO = 1.00
SUBTITLE_FONTS_DIR_ENV_NAME = "SUBTITLE_FONTS_DIR"
TITLE_OVERLAY_STYLE_PREFIX = "TitleOverlay"
TITLE_OVERLAY_FONT_BY_LANGUAGE = {
    "ru": "TD Afisha",
    "en": "Taller",
}
TITLE_OVERLAY_FONT_FILE_BY_LANGUAGE = {
    "ru": "TD Afisha.otf",
    "en": "Taller.otf",
}
TITLE_OVERLAY_DEFAULT_FONT = "TD Afisha"
TITLE_OVERLAY_DEFAULT_FONT_FILE = "TD Afisha.otf"
TITLE_OVERLAY_VERTICAL_FONT_SIZE = 118
TITLE_OVERLAY_HORIZONTAL_FONT_SIZE = 80
TITLE_OVERLAY_SQUARE_FONT_SIZE = 92
TITLE_OVERLAY_MARGIN_RATIO = 0.10
TITLE_OVERLAY_TOP_MARGIN_RATIO = 0.05
TITLE_OVERLAY_MAX_HEIGHT_RATIO = 0.08
TITLE_OVERLAY_OUTLINE = 5
TITLE_OVERLAY_SHADOW = 12
TITLE_OVERLAY_SPACING = 0
TITLE_OVERLAY_ASS_SAFE_WIDTH_RATIO = 1.0
TITLE_OVERLAY_ASS_SAFE_WIDTH_RATIO_BY_LANGUAGE = {
    "ru": 0.88,
}
TITLE_OVERLAY_RU_SCALE_X = 100
TITLE_SERIES_WIDTH_RATIO = 0.50
TITLE_SERIES_WIDTH_RATIO_BY_LANGUAGE = {
    "en": 0.50,
    "ru": 0.45,
}
TITLE_SERIES_DAY_WIDTH_RATIO = 0.30
TITLE_SERIES_DAY_START_RATIO_BY_LANGUAGE = {
    "en": 0.60,
    "ru": 0.60,
}
TITLE_SERIES_DAY_BASELINE_Y_SHIFT_BY_LANGUAGE = {
    "en": 0.30,
    "ru": 0.42,
}
TITLE_SERIES_MAIN_TITLE_OFFSET_FONT_SCALE_BY_LANGUAGE = {
    "en": 0.48,
    "ru": 0.52,
}
TITLE_SERIES_MAIN_TITLE_OFFSET_MAX_FONT_SCALE = 0.95
TITLE_SERIES_DAY_GAP_RATIO = 0.003
TITLE_SERIES_ROW_GAP_RATIO = -0.004
TITLE_SERIES_WORD_SEPARATOR = r"\h"
TITLE_OVERLAY_OUTLINE_COLOUR = "&H00000000"
TITLE_OVERLAY_SHADOW_COLOUR = "&H00000000"
TITLE_SERIES_STYLE_PREFIX = "TitleSeries"
TITLE_SERIES_FONT_BY_LANGUAGE = {
    "ru": "Berezka",
    "en": "Ravsten Demo",
}
TITLE_SERIES_FONT_FILE_BY_LANGUAGE = {
    "ru": "Berezka.ttf",
    "en": "Ravsten Demo.otf",
}
TITLE_SERIES_DEFAULT_FONT = "Berezka"
TITLE_SERIES_DEFAULT_FONT_FILE = "Berezka.ttf"
TITLE_SERIES_DAY_FONT = "Marianna"
TITLE_SERIES_DAY_FONT_FILE = "Marianna.ttf"
TITLE_SERIES_VERTICAL_FONT_SIZE = 82
TITLE_SERIES_HORIZONTAL_FONT_SIZE = 54
TITLE_SERIES_SQUARE_FONT_SIZE = 64
TITLE_SERIES_DAY_VERTICAL_FONT_SIZE = 150
TITLE_SERIES_DAY_HORIZONTAL_FONT_SIZE = 76
TITLE_SERIES_DAY_SQUARE_FONT_SIZE = 88
TITLE_SERIES_RU_DAY_NUMBER_FONT_SCALE = 1.0
TITLE_SERIES_FONT_SIZE_SCALE_BY_LANGUAGE = {}
TITLE_SERIES_ASS_WIDTH_CORRECTION_BY_LANGUAGE = {
    "ru": 0.56,
}
TITLE_SERIES_DAY_ASS_WIDTH_CORRECTION_BY_LANGUAGE = {
    "en": 0.60,
    "ru": 0.46,
}
TITLE_SERIES_ASS_SCALE_X_BY_LANGUAGE = {
    "en": 100,
    "ru": 100,
}
TITLE_SERIES_DAY_ASS_SCALE_X_BY_LANGUAGE = {
    "en": 100,
    "ru": 100,
}
TITLE_SERIES_OUTLINE = 5
TITLE_SERIES_SHADOW = 10
TITLE_SERIES_PRIMARY_COLOUR = "&H00FFFFFF"
TITLE_SERIES_OUTLINE_COLOUR = "&H00000000"
TITLE_SERIES_SHADOW_COLOUR = "&H00000000"
COMBINING_ACCENT_MARKS_RE = re.compile(r"[\u0300-\u036f]")
CYRILLIC_WORD_RE = re.compile(r"[А-Яа-яЁё]+(?:[А-Яа-яЁё]+)?")
SUBTITLE_PAUSE_MARKER_RE = re.compile(r"\[\s*(?:pause|пауза)\s*\]", re.IGNORECASE)
CYRILLIC_CHAR_RE = re.compile(r"[А-Яа-яЁё]")
CYRILLIC_TO_LATIN_TITLE_MAP = str.maketrans(
    {
        "А": "A",
        "Б": "B",
        "В": "V",
        "Г": "G",
        "Д": "D",
        "Е": "E",
        "Ё": "E",
        "Ж": "Zh",
        "З": "Z",
        "И": "I",
        "Й": "Y",
        "К": "K",
        "Л": "L",
        "М": "M",
        "Н": "N",
        "О": "O",
        "П": "P",
        "Р": "R",
        "С": "S",
        "Т": "T",
        "У": "U",
        "Ф": "F",
        "Х": "Kh",
        "Ц": "Ts",
        "Ч": "Ch",
        "Ш": "Sh",
        "Щ": "Shch",
        "Ъ": "",
        "Ы": "Y",
        "Ь": "",
        "Э": "E",
        "Ю": "Yu",
        "Я": "Ya",
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)
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


@dataclass(frozen=True, slots=True)
class TitleOverlayFit:
    text: str
    font_size: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class TitleSeriesFit:
    text: str
    counter_text: str
    series_font_size: int
    counter_font_size: int
    counter_number_font_size: int
    counter_scale_x: int
    series_width: int
    series_height: int
    counter_width: int
    counter_height: int
    width: int
    height: int
    scale_x: int = 100
    series_actual_width: int = 0
    series_actual_height: int = 0
    counter_actual_width: int = 0


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
    if any("dincondensed" in name for name in font_files):
        return SUBTITLE_PRIMARY_FONT
    if any("arialnarrow" in name for name in font_files):
        return SUBTITLE_SECONDARY_FONT
    system_font_candidates = (
        (Path("/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"), SUBTITLE_PRIMARY_FONT),
        (Path("/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf"), SUBTITLE_SECONDARY_FONT),
    )
    for path, name in system_font_candidates:
        if path.exists():
            return name
    if any("tdafisha" in name for name in font_files):
        return SUBTITLE_FALLBACK_FONT
    if any("inter" in name and "semibold" in name for name in font_files):
        return "Inter SemiBold"
    if any("notosans" in name for name in font_files):
        return "Noto Sans"
    return SUBTITLE_FALLBACK_FONT


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
    raw = SUBTITLE_PAUSE_MARKER_RE.sub(" ", str(text or ""))
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
        base_margin_h = SUBTITLE_VERTICAL_MARGIN_H
        base_margin_v = SUBTITLE_VERTICAL_MARGIN_V
        base_line_chars = SUBTITLE_VERTICAL_LINE_CHARS
    elif ratio > 1.15:
        base_width = 1920
        base_height = 1080
        base_font_size = SUBTITLE_HORIZONTAL_FONT_SIZE
        base_margin_h = SUBTITLE_HORIZONTAL_MARGIN_H
        base_margin_v = SUBTITLE_HORIZONTAL_MARGIN_V
        base_line_chars = SUBTITLE_HORIZONTAL_LINE_CHARS
    else:
        base_width = 1080
        base_height = 1080
        base_font_size = SUBTITLE_SQUARE_FONT_SIZE
        base_margin_h = SUBTITLE_SQUARE_MARGIN_H
        base_margin_v = SUBTITLE_SQUARE_MARGIN_V
        base_line_chars = SUBTITLE_SQUARE_LINE_CHARS
    scale = safe_height / float(base_height)
    width_scale = safe_width / float(base_width)
    font_size = max(24, int(round(base_font_size * scale)))
    margin_v = max(36, int(round(base_margin_v * scale)))
    margin_h = max(48, int(round(base_margin_h * width_scale)))
    font_scale = max(0.01, scale)
    line_chars = max(18, int(round(base_line_chars * (width_scale / font_scale))))
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
        longer_top_penalty = max(0, len(first) - len(second))
        balance = abs(len(first) - len(second))
        score = (overflow, longer_top_penalty, balance, [first, second])
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
    return "\n".join(_subtitle_display_text(line) for line in visible[:SUBTITLE_MAX_LINES])


def _subtitle_display_text(text: str) -> str:
    display = str(text or "")
    return display.upper() if SUBTITLE_UPPERCASE else display


def _subtitle_line_advance(layout: SubtitleLayout) -> int:
    return max(1, int(round(layout.font_size * SUBTITLE_LINE_ADVANCE_RATIO)))


def _subtitle_line_anchor_y(layout: SubtitleLayout, *, line_count: int, index: int) -> int:
    virtual_margin_v = layout.margin_v + ((line_count - index - 1) * _subtitle_line_advance(layout))
    return max(0, layout.height - virtual_margin_v)


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


def _title_overlay_style_name(language: str) -> str:
    code = str(language or "").strip().lower()
    return f"{TITLE_OVERLAY_STYLE_PREFIX}{code.upper() if code else 'RU'}"


def _title_overlay_font_name(language: str) -> str:
    code = str(language or "").strip().lower()
    return TITLE_OVERLAY_FONT_BY_LANGUAGE.get(code) or TITLE_OVERLAY_DEFAULT_FONT


def _title_overlay_font_path(language: str) -> Path | None:
    code = str(language or "").strip().lower()
    filename = TITLE_OVERLAY_FONT_FILE_BY_LANGUAGE.get(code) or TITLE_OVERLAY_DEFAULT_FONT_FILE
    path = subtitle_fonts_dir() / filename
    return path if path.exists() else None


def _title_series_style_name(language: str) -> str:
    code = str(language or "").strip().lower()
    return f"{TITLE_SERIES_STYLE_PREFIX}{code.upper() if code else 'RU'}"


def _title_overlay_ass_safe_width_ratio(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_OVERLAY_ASS_SAFE_WIDTH_RATIO_BY_LANGUAGE.get(code) or TITLE_OVERLAY_ASS_SAFE_WIDTH_RATIO)


def _title_series_font_name(language: str) -> str:
    code = str(language or "").strip().lower()
    return TITLE_SERIES_FONT_BY_LANGUAGE.get(code) or TITLE_SERIES_DEFAULT_FONT


def _title_series_font_path(language: str) -> Path | None:
    code = str(language or "").strip().lower()
    filename = TITLE_SERIES_FONT_FILE_BY_LANGUAGE.get(code) or TITLE_SERIES_DEFAULT_FONT_FILE
    path = subtitle_fonts_dir() / filename
    return path if path.exists() else None


def _title_series_day_font_path() -> Path | None:
    path = subtitle_fonts_dir() / TITLE_SERIES_DAY_FONT_FILE
    return path if path.exists() else None


def _title_series_ass_scale_x(language: str) -> int:
    code = str(language or "").strip().lower()
    return int(TITLE_SERIES_ASS_SCALE_X_BY_LANGUAGE.get(code) or 100)


def _title_series_width_ratio(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_SERIES_WIDTH_RATIO_BY_LANGUAGE.get(code) or TITLE_SERIES_WIDTH_RATIO)


def _title_series_day_start_ratio(language: str, *, side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO) -> float:
    code = str(language or "").strip().lower()
    default = side_margin_ratio + _title_series_width_ratio(language)
    return float(TITLE_SERIES_DAY_START_RATIO_BY_LANGUAGE.get(code) or default)


def _title_series_font_size_scale(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_SERIES_FONT_SIZE_SCALE_BY_LANGUAGE.get(code) or 1.0)


def _title_series_ass_width_correction(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_SERIES_ASS_WIDTH_CORRECTION_BY_LANGUAGE.get(code) or 1.0)


def _title_series_day_ass_width_correction(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_SERIES_DAY_ASS_WIDTH_CORRECTION_BY_LANGUAGE.get(code) or 1.0)


def _title_series_day_ass_scale_x(language: str) -> int:
    code = str(language or "").strip().lower()
    return int(TITLE_SERIES_DAY_ASS_SCALE_X_BY_LANGUAGE.get(code) or 100)


def _title_series_day_y_shift_ratio(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_SERIES_DAY_BASELINE_Y_SHIFT_BY_LANGUAGE.get(code) or 0.0)


def _title_series_main_title_offset_scale(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_SERIES_MAIN_TITLE_OFFSET_FONT_SCALE_BY_LANGUAGE.get(code) or 0.74)


def _title_series_day_gap(layout: SubtitleLayout) -> int:
    return max(6, int(round(layout.width * TITLE_SERIES_DAY_GAP_RATIO)))


def _title_series_main_row_offset(layout: SubtitleLayout, fit: TitleSeriesFit) -> int:
    visual_height = min(
        int(fit.series_actual_height or fit.height),
        int(round(fit.series_font_size * TITLE_SERIES_MAIN_TITLE_OFFSET_MAX_FONT_SCALE)),
    )
    return max(0, int(round(visual_height + (layout.height * TITLE_SERIES_ROW_GAP_RATIO))))


def _title_series_day_x(
    *,
    layout: SubtitleLayout,
    fit: TitleSeriesFit | None,
    series_left: int,
    title_margin_h: int,
    language: str,
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
) -> int:
    if fit is None:
        return int(round(layout.width * _title_series_day_start_ratio(language, side_margin_ratio=side_margin_ratio)))
    counter_width = int(fit.counter_actual_width or fit.counter_width)
    preferred_x = int(round(layout.width * _title_series_day_start_ratio(language, side_margin_ratio=side_margin_ratio)))
    max_x = layout.width - title_margin_h - counter_width
    return max(series_left, min(preferred_x, max_x))


def _coerce_overlay_ratio(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return default
    if ratio > 1:
        ratio = ratio / 100.0
    return min(max(ratio, minimum), maximum)


def _ass_colour_from_hex(value: Any, *, default: str = "&H00FFFFFF") -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", raw)
    if not match:
        return default
    rgb = match.group(1)
    rr, gg, bb = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H00{bb.upper()}{gg.upper()}{rr.upper()}"


def _title_overlay_font_size(layout: SubtitleLayout, text: str) -> int:
    if layout.height > layout.width:
        base = TITLE_OVERLAY_VERTICAL_FONT_SIZE
    elif layout.width > layout.height:
        base = TITLE_OVERLAY_HORIZONTAL_FONT_SIZE
    else:
        base = TITLE_OVERLAY_SQUARE_FONT_SIZE
    length = len(" ".join(str(text or "").split()))
    if length <= 0:
        return base
    if 0 < length <= 4:
        return int(base * 2.05)
    if length <= 8:
        return int(base * 1.75)
    if length <= 18:
        return int(base * 1.32)
    if length > 78:
        return max(48, int(base * 0.58))
    if length > 52:
        return max(56, int(base * 0.72))
    return base


def _title_series_font_size(layout: SubtitleLayout) -> int:
    scale = layout.height / 1920.0 if layout.height > layout.width else layout.width / 1080.0
    if layout.height > layout.width:
        return int(round(TITLE_SERIES_VERTICAL_FONT_SIZE * scale))
    if layout.width > layout.height:
        return int(round(TITLE_SERIES_HORIZONTAL_FONT_SIZE * scale))
    return int(round(TITLE_SERIES_SQUARE_FONT_SIZE * scale))


def _title_series_day_font_size(layout: SubtitleLayout, language: str = "") -> int:
    scale = layout.height / 1920.0 if layout.height > layout.width else layout.width / 1080.0
    if layout.height > layout.width:
        base = TITLE_SERIES_DAY_VERTICAL_FONT_SIZE
    elif layout.width > layout.height:
        base = TITLE_SERIES_DAY_HORIZONTAL_FONT_SIZE
    else:
        base = TITLE_SERIES_DAY_SQUARE_FONT_SIZE
    return int(round(base * scale))


def _title_series_ru_day_number_font_size(layout: SubtitleLayout, language: str = "") -> int:
    base = _title_series_day_font_size(layout, language)
    if str(language or "").strip().lower() == "ru":
        return int(round(base * TITLE_SERIES_RU_DAY_NUMBER_FONT_SCALE))
    return base


def _title_series_row_offset(layout: SubtitleLayout, language: str = "") -> int:
    return int(round(_title_series_ru_day_number_font_size(layout, language) * 0.62))


def _wrap_title_overlay_text(text: str, *, max_chars: int) -> str:
    words = " ".join(str(text or "").split()).split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
            continue
        current = candidate
    if current:
        lines.append(current)
    if len(lines) <= 3:
        return "\n".join(lines)
    merged = lines[:2] + [" ".join(lines[2:])]
    return "\n".join(merged)


def _measure_title_line(draw: ImageDraw.ImageDraw, line: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), line or " ", font=font, stroke_width=TITLE_OVERLAY_OUTLINE)
    return max(0, int(round(bbox[2] - bbox[0])))


def _wrap_title_overlay_lines_by_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    max_width: int,
) -> list[str]:
    words = " ".join(str(text or "").split()).split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}".strip()
        if _measure_title_line(draw, candidate, font) <= max_width:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


def _measure_title_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    *,
    spacing: int,
) -> tuple[int, int]:
    if not lines:
        return 0, 0
    bbox = draw.multiline_textbbox(
        (0, 0),
        "\n".join(lines),
        font=font,
        spacing=spacing,
        stroke_width=TITLE_OVERLAY_OUTLINE,
    )
    width = max(_measure_title_line(draw, line, font) for line in lines)
    height = max(0, int(round(bbox[3] - bbox[1])))
    return width, height


def _measure_title_series_counter(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    layout: SubtitleLayout,
    language: str,
    base_size: int | None = None,
    number_size: int | None = None,
) -> tuple[int, int]:
    day_font_path = _title_series_day_font_path()
    if day_font_path is None:
        fallback = int(layout.width * 0.22)
        return fallback, int(base_size or _title_series_day_font_size(layout, language))
    resolved_base_size = int(base_size or _title_series_day_font_size(layout, language))
    base_font = ImageFont.truetype(str(day_font_path), size=resolved_base_size)
    clean = " ".join(str(text or "").split())
    if str(language or "").strip().lower() != "ru":
        return _measure_title_block(draw, [clean], base_font, spacing=0)
    match = re.search(r"\d+", clean)
    if match is None:
        return _measure_title_block(draw, [clean], base_font, spacing=0)
    resolved_number_size = int(number_size or _title_series_ru_day_number_font_size(layout, language))
    number_font = ImageFont.truetype(
        str(day_font_path),
        size=resolved_number_size,
    )
    width = 0
    height = 0
    before = clean[: match.start()]
    number = clean[match.start() : match.end()]
    after = clean[match.end() :]
    if before:
        before_width, before_height = _measure_title_block(draw, [before], base_font, spacing=0)
        width += before_width
        height = max(height, before_height)
    number_width, number_height = _measure_title_block(draw, [number], number_font, spacing=0)
    width += number_width
    height = max(height, number_height)
    if after:
        after_width, after_height = _measure_title_block(draw, [after], base_font, spacing=0)
        width += after_width
        height = max(height, after_height)
    return width, height


def _fit_title_overlay_ass_text(
    text: str,
    *,
    layout: SubtitleLayout,
    language: str,
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
    has_series_cue: bool = False,
    top_margin_ratio: float = TITLE_OVERLAY_TOP_MARGIN_RATIO,
) -> TitleOverlayFit | None:
    clean = " ".join(str(text or "").split())
    font_path = _title_overlay_font_path(language)
    if not clean or font_path is None:
        return None

    margin_h = int(layout.width * side_margin_ratio)
    max_width = max(
        1,
        int(
            (
                layout.width
                - (margin_h * 2)
                - TITLE_OVERLAY_SHADOW
                - (TITLE_OVERLAY_OUTLINE * 2)
            )
            * _title_overlay_ass_safe_width_ratio(language)
        ),
    )
    _ = (top_margin_ratio, has_series_cue)
    max_height = max(1, int(layout.height * TITLE_OVERLAY_MAX_HEIGHT_RATIO))
    base_size = _title_overlay_font_size(layout, clean)
    max_size = max(base_size, int(layout.height * 0.42))
    min_size = max(32, int(round(base_size * 0.45)))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    for size in range(max_size, min_size - 1, -2):
        font = ImageFont.truetype(str(font_path), size=size)
        lines = [clean]
        spacing = max(8, int(size * 0.10))
        width, height = _measure_title_block(draw, lines, font, spacing=spacing)
        if width <= max_width and height <= max_height:
            return TitleOverlayFit("\n".join(lines), size, width, height)
    font = ImageFont.truetype(str(font_path), size=min_size)
    width, height = _measure_title_block(draw, [clean], font, spacing=max(8, int(min_size * 0.10)))
    return TitleOverlayFit(clean, min_size, width, height)


def _fit_title_series_ass_text(
    series_text: str,
    counter_text: str,
    *,
    layout: SubtitleLayout,
    language: str,
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
    max_width: int | None = None,
) -> TitleSeriesFit | None:
    clean = " ".join(str(series_text or "").split())
    if str(language or "").strip().lower() == "ru":
        clean = clean.lower()
    measure_text = _title_series_visual_text(clean)
    font_path = _title_series_font_path(language)
    if not clean or font_path is None:
        return None

    margin_h = int(layout.width * side_margin_ratio)
    max_row_width = max(1, int(max_width or (layout.width - (margin_h * 2))))
    clean_counter = " ".join(str(counter_text or "").split())
    gap_width = _title_series_day_gap(layout) if clean_counter else 0
    counter_target_width = min(
        max_row_width,
        max(1, int(round(layout.width * TITLE_SERIES_DAY_WIDTH_RATIO))),
    )
    series_target_width = min(
        max_row_width,
        max(1, int(round(layout.width * _title_series_width_ratio(language)))),
    )
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    base_size = _title_series_font_size(layout)
    max_series_size = max(base_size, int(round(layout.height * 0.16)))
    min_series_size = max(24, int(round(base_size * 0.45)))
    display_series_scale = _title_series_font_size_scale(language)
    display_series_scale_x = _title_series_ass_scale_x(language)
    series_width_correction = _title_series_ass_width_correction(language)
    size = min_series_size
    series_width = 0
    series_height = 0
    for candidate_size in range(max_series_size, min_series_size - 1, -2):
        display_size = int(round(candidate_size * display_series_scale))
        font = ImageFont.truetype(str(font_path), size=display_size)
        raw_width, height = _measure_title_block(draw, [measure_text], font, spacing=0)
        width = int(round(raw_width * (display_series_scale_x / 100.0) * series_width_correction))
        if width <= series_target_width:
            size = candidate_size
            series_width = width
            series_height = height
            break
    if series_width <= 0:
        display_size = int(round(size * display_series_scale))
        font = ImageFont.truetype(str(font_path), size=display_size)
        raw_width, series_height = _measure_title_block(draw, [measure_text], font, spacing=0)
        series_width = int(round(raw_width * (display_series_scale_x / 100.0) * series_width_correction))

    counter_width = 0
    counter_height = 0
    counter_scale_x = 100
    counter_size = size
    number_size = size
    min_counter_size = max(24, int(round(_title_series_day_font_size(layout, language) * 0.45)))
    if clean_counter:
        base_counter_size = _title_series_day_font_size(layout, language)
        lang = str(language or "").strip().lower()
        preferred_counter_size = max(
            int(round(base_counter_size * 2.60)),
            int(round(size * (1.70 if lang == "ru" else 1.15))),
        )
        max_counter_size = preferred_counter_size
        min_counter_size = max(24, int(round(base_counter_size * 0.35)))
        for candidate_size in range(max_counter_size, min_counter_size - 1, -2):
            candidate_width, candidate_height = _measure_title_series_counter(
                draw,
                clean_counter,
                layout=layout,
                language=language,
                base_size=candidate_size,
                number_size=candidate_size,
            )
            candidate_width = int(round(candidate_width * _title_series_day_ass_width_correction(language)))
            if candidate_width <= counter_target_width:
                counter_size = candidate_size
                number_size = candidate_size
                counter_width = candidate_width
                counter_height = candidate_height
                counter_scale_x = 100
                break
        if counter_width <= 0:
            counter_size = min_counter_size
            number_size = min_counter_size
            counter_width, counter_height = _measure_title_series_counter(
                draw,
                clean_counter,
                layout=layout,
                language=language,
                base_size=counter_size,
                number_size=number_size,
            )
            counter_width = int(round(counter_width * _title_series_day_ass_width_correction(language)))
            counter_scale_x = 100
    scaled_counter_width = counter_width
    fitted_width = series_width + gap_width + scaled_counter_width
    while fitted_width > max_row_width and (size > min_series_size or (clean_counter and counter_size > min_counter_size)):
        reduce_counter = bool(
            clean_counter
            and counter_size > min_counter_size
            and (scaled_counter_width >= counter_target_width or size <= min_series_size)
        )
        if reduce_counter:
            counter_size = max(min_counter_size, counter_size - 1)
            number_size = counter_size
            counter_width, counter_height = _measure_title_series_counter(
                draw,
                clean_counter,
                layout=layout,
                language=language,
                base_size=counter_size,
                number_size=number_size,
            )
            counter_width = int(round(counter_width * _title_series_day_ass_width_correction(language)))
            counter_scale_x = 100
        elif size > min_series_size:
            size = max(min_series_size, size - 1)
            display_size = int(round(size * display_series_scale))
            font = ImageFont.truetype(str(font_path), size=display_size)
            raw_width, series_height = _measure_title_block(draw, [measure_text], font, spacing=0)
            series_width = int(round(raw_width * (display_series_scale_x / 100.0) * series_width_correction))
        elif clean_counter and counter_size > min_counter_size:
            counter_size = max(min_counter_size, counter_size - 1)
            number_size = counter_size
            counter_width, counter_height = _measure_title_series_counter(
                draw,
                clean_counter,
                layout=layout,
                language=language,
                base_size=counter_size,
                number_size=number_size,
            )
            counter_width = int(round(counter_width * _title_series_day_ass_width_correction(language)))
            counter_scale_x = 100
        scaled_counter_width = counter_width
        fitted_width = series_width + gap_width + scaled_counter_width
    display_series_size = int(round(size * display_series_scale))
    series_slot_width = series_target_width
    counter_slot_width = counter_target_width if clean_counter else 0
    counter_ass_scale_x = counter_scale_x if clean_counter else 100
    counter_actual_width = counter_width
    return TitleSeriesFit(
        clean,
        clean_counter,
        display_series_size,
        counter_size,
        number_size,
        counter_ass_scale_x,
        series_slot_width,
        series_height,
        counter_slot_width,
        counter_height,
        series_width + gap_width + counter_actual_width,
        max(series_height, counter_height),
        display_series_scale_x,
        series_width,
        series_height,
        counter_actual_width,
    )


def _format_title_overlay_ass_text(
    text: str,
    *,
    layout: SubtitleLayout,
    language: str = "ru",
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
    top_margin_ratio: float = TITLE_OVERLAY_TOP_MARGIN_RATIO,
    has_series_cue: bool = False,
) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        return ""
    try:
        fit = _fit_title_overlay_ass_text(
            clean,
            layout=layout,
            language=language,
            side_margin_ratio=side_margin_ratio,
            top_margin_ratio=top_margin_ratio,
            has_series_cue=has_series_cue,
        )
    except Exception:
        logger.exception("post_production.title_overlay_fit_failed language=%s", language)
        fit = None
    scale_x = TITLE_OVERLAY_RU_SCALE_X if str(language or "").strip().lower() == "ru" else 100
    if fit is not None:
        return f"{{\\fscx{scale_x}\\fs{fit.font_size}}}" + _escape_ass_no_wrap_spaces(fit.text)

    font_size = _title_overlay_font_size(layout, clean)
    return f"{{\\fscx{scale_x}\\fs{font_size}}}" + _escape_ass_no_wrap_spaces(clean)


def _escape_ass_no_wrap_spaces(text: str) -> str:
    return _escape_ass_text(text).replace(" ", r"\h")


def latinize_cyrillic_title(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        return ""
    latinized = clean.translate(CYRILLIC_TO_LATIN_TITLE_MAP)
    latinized = re.sub(r"[^0-9A-Za-z .,'&!?:;()/\\\\-]+", "", latinized)
    latinized = " ".join(latinized.split())
    return latinized.title() if clean.isupper() else latinized[:1].upper() + latinized[1:]


def _title_series_visual_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _escape_title_series_ass_text(text: str) -> str:
    return _escape_ass_no_wrap_spaces(text)


def _format_title_series_counter_ass_text(
    counter_text: str,
    *,
    layout: SubtitleLayout,
    language: str,
    base_size: int | None = None,
    number_size: int | None = None,
    scale_x: int = 100,
) -> str:
    clean = " ".join(str(counter_text or "").split())
    if not clean:
        return ""
    resolved_base_size = int(base_size or _title_series_day_font_size(layout, language))
    resolved_scale_x = max(45, min(100, int(scale_x or 100)))
    prefix = f"{{\\fn{TITLE_SERIES_DAY_FONT}\\fscx{resolved_scale_x}\\fs{resolved_base_size}}}"
    if str(language or "").strip().lower() != "ru":
        return prefix + _escape_ass_no_wrap_spaces(clean)
    match = re.search(r"\d+", clean)
    if match is None:
        return prefix + _escape_ass_no_wrap_spaces(clean)
    resolved_number_size = int(number_size or _title_series_ru_day_number_font_size(layout, language))
    before = _escape_ass_no_wrap_spaces(clean[: match.start()])
    number = _escape_ass_no_wrap_spaces(clean[match.start() : match.end()])
    after = _escape_ass_no_wrap_spaces(clean[match.end() :])
    return f"{prefix}{before}{{\\fs{resolved_number_size}}}{number}{{\\fs{resolved_base_size}}}{after}"


def _format_title_series_ass_text(
    series_text: str,
    counter_text: str,
    *,
    layout: SubtitleLayout,
    language: str = "ru",
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
    max_width: int | None = None,
) -> str:
    clean_series = " ".join(str(series_text or "").split())
    if str(language or "").strip().lower() == "ru":
        clean_series = clean_series.lower()
    clean_counter = " ".join(str(counter_text or "").split())
    if not clean_series and not clean_counter:
        return ""
    fit: TitleSeriesFit | None = None
    if clean_series:
        try:
            fit = _fit_title_series_ass_text(
                clean_series,
                clean_counter,
                layout=layout,
                language=language,
                side_margin_ratio=side_margin_ratio,
                max_width=max_width,
            )
        except Exception:
            logger.exception("post_production.title_series_fit_failed language=%s", language)
    formatted_series = ""
    if clean_series:
        series_font_size = fit.series_font_size if fit is not None else _title_series_font_size(layout)
        scale_x = fit.scale_x if fit is not None else 100
        formatted_series = f"{{\\q2\\fscx{scale_x}\\fs{series_font_size}}}" + _escape_title_series_ass_text(clean_series)
    if not clean_counter:
        return formatted_series
    counter = _format_title_series_counter_ass_text(
        clean_counter,
        layout=layout,
        language=language,
        base_size=fit.counter_font_size if fit is not None else None,
        number_size=fit.counter_number_font_size if fit is not None else None,
        scale_x=fit.counter_scale_x if fit is not None else 100,
    )
    if not clean_series:
        return "{\\q2}" + counter
    return f"{formatted_series}\\h\\h{counter}"


def _format_title_series_title_only_ass_text(
    series_text: str,
    *,
    layout: SubtitleLayout,
    language: str = "ru",
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
    max_width: int | None = None,
    fit: TitleSeriesFit | None = None,
) -> str:
    clean_series = " ".join(str(series_text or "").split())
    if str(language or "").strip().lower() == "ru":
        clean_series = clean_series.lower()
    if not clean_series:
        return ""
    if fit is None:
        try:
            fit = _fit_title_series_ass_text(
                clean_series,
                "",
                layout=layout,
                language=language,
                side_margin_ratio=side_margin_ratio,
                max_width=max_width,
            )
        except Exception:
            logger.exception("post_production.title_series_fit_failed language=%s", language)
            fit = None
    series_font_size = fit.series_font_size if fit is not None else _title_series_font_size(layout)
    scale_x = fit.scale_x if fit is not None else 100
    return f"{{\\q2\\fscx{scale_x}\\fs{series_font_size}}}" + _escape_title_series_ass_text(clean_series)


def _overlay_title_for_language(overlay_title_by_lang: dict[str, Any] | None, language: str) -> str:
    if not isinstance(overlay_title_by_lang, dict):
        return ""
    lang = str(language or "").strip().lower()
    direct = str(overlay_title_by_lang.get(lang) or "").strip()
    if direct:
        return direct
    if lang != "ru":
        fallback = str(overlay_title_by_lang.get("en") or "").strip()
        if fallback:
            return fallback
        ru_fallback = str(overlay_title_by_lang.get("ru") or "").strip()
        return latinize_cyrillic_title(ru_fallback) if CYRILLIC_CHAR_RE.search(ru_fallback) else ru_fallback
    else:
        fallback = str(overlay_title_by_lang.get("ru") or overlay_title_by_lang.get("en") or "").strip()
    return fallback


def _overlay_value_for_language(value_by_lang: dict[str, Any] | None, language: str) -> str:
    if not isinstance(value_by_lang, dict):
        return ""
    lang = str(language or "").strip().lower()
    direct = str(value_by_lang.get(lang) or "").strip()
    if direct:
        return direct
    fallback = str(value_by_lang.get("ru") or value_by_lang.get("en") or "").strip()
    return fallback


def _build_title_overlay_cue(
    overlay_title_by_lang: dict[str, Any] | None,
    *,
    language: str,
    duration_sec: float,
) -> dict[str, Any] | None:
    title = _overlay_title_for_language(overlay_title_by_lang, language)
    duration = max(0.0, float(duration_sec or 0.0))
    if not title or duration <= 0:
        return None
    return {
        "text": title,
        "start_sec": 0.0,
        "end_sec": duration,
        "style": _title_overlay_style_name(language),
        "language": str(language or "ru").strip().lower() or "ru",
    }


def _build_title_series_overlay_cue(
    overlay_series_title_by_lang: dict[str, Any] | None,
    overlay_series_counter_by_lang: dict[str, Any] | None,
    *,
    language: str,
    duration_sec: float,
) -> dict[str, Any] | None:
    series_title = _overlay_value_for_language(overlay_series_title_by_lang, language)
    counter = _overlay_value_for_language(overlay_series_counter_by_lang, language)
    duration = max(0.0, float(duration_sec or 0.0))
    if (not series_title and not counter) or duration <= 0:
        return None
    lang = str(language or "ru").strip().lower() or "ru"
    return {
        "kind": "series",
        "text": series_title,
        "counter_text": counter,
        "start_sec": 0.0,
        "end_sec": duration,
        "style": _title_series_style_name(lang),
        "language": lang,
    }


def _write_burned_subtitle_ass(
    path: Path,
    cues: list[dict[str, Any]],
    *,
    title_cues: list[dict[str, Any]] | None = None,
    title_style: dict[str, Any] | None = None,
    video_width: int | None = None,
    video_height: int | None = None,
) -> None:
    layout = _subtitle_layout_for_dimensions(video_width, video_height)
    font_name = _subtitle_font_name()
    title_style = title_style if isinstance(title_style, dict) else {}
    title_margin_ratio = _coerce_overlay_ratio(
        title_style.get("side_margin_ratio"),
        default=TITLE_OVERLAY_MARGIN_RATIO,
        minimum=0.06,
        maximum=0.22,
    )
    title_top_margin_ratio = _coerce_overlay_ratio(
        title_style.get("top_margin_ratio"),
        default=TITLE_OVERLAY_TOP_MARGIN_RATIO,
        minimum=0.04,
        maximum=0.22,
    )
    title_primary_colour = _ass_colour_from_hex(title_style.get("title_color"))
    title_margin_h = max(0, int(layout.width * title_margin_ratio))
    base_title_margin_v = max(0, int(layout.height * title_top_margin_ratio))
    has_series_cue = any(str(cue.get("kind") or "") == "series" for cue in title_cues or [])
    series_row_offsets: list[int] = []
    for cue in title_cues or []:
        if str(cue.get("kind") or "") != "series":
            continue
        language = str(cue.get("language") or "ru").strip().lower() or "ru"
        fit = _fit_title_series_ass_text(
            str(cue.get("text") or ""),
            str(cue.get("counter_text") or ""),
            layout=layout,
            language=language,
            side_margin_ratio=title_margin_ratio,
            max_width=layout.width - (title_margin_h * 2),
        )
        if fit is not None:
            series_row_offsets.append(
                _title_series_main_row_offset(layout, fit)
            )
        else:
            series_row_offsets.append(_title_series_row_offset(layout, language))
    series_row_offset = max(series_row_offsets, default=0)
    main_title_margin_v = base_title_margin_v + (series_row_offset if has_series_cue else 0)
    series_margin_l = title_margin_h
    title_styles: list[str] = []
    if title_cues:
        style_specs: list[dict[str, Any]] = []
        seen_styles: set[str] = set()
        for cue in title_cues:
            language = str(cue.get("language") or "ru").strip().lower() or "ru"
            kind = str(cue.get("kind") or "title")
            style_name = str(
                cue.get("style") or (_title_series_style_name(language) if kind == "series" else _title_overlay_style_name(language))
            )
            if style_name in seen_styles:
                continue
            seen_styles.add(style_name)
            if kind == "series":
                style_specs.append(
                    {
                        "style_name": style_name,
                        "font": _title_series_font_name(language),
                        "font_size": _title_series_font_size(layout),
                        "primary_colour": TITLE_SERIES_PRIMARY_COLOUR,
                        "outline_colour": TITLE_SERIES_OUTLINE_COLOUR,
                        "shadow_colour": TITLE_SERIES_SHADOW_COLOUR,
                        "spacing": 0,
                        "outline": TITLE_SERIES_OUTLINE,
                        "shadow": TITLE_SERIES_SHADOW,
                        "margin_v": base_title_margin_v,
                        "margin_l": series_margin_l,
                        "margin_r": title_margin_h,
                        "alignment": 7,
                    }
                )
            else:
                style_specs.append(
                    {
                        "style_name": style_name,
                        "font": _title_overlay_font_name(language),
                        "font_size": _title_overlay_font_size(layout, ""),
                        "primary_colour": title_primary_colour,
                        "outline_colour": TITLE_OVERLAY_OUTLINE_COLOUR,
                        "shadow_colour": TITLE_OVERLAY_SHADOW_COLOUR,
                        "spacing": TITLE_OVERLAY_SPACING,
                        "outline": TITLE_OVERLAY_OUTLINE,
                        "shadow": TITLE_OVERLAY_SHADOW,
                        "margin_v": main_title_margin_v,
                        "margin_l": title_margin_h,
                        "margin_r": title_margin_h,
                        "alignment": 7,
                    }
                )
        for spec in style_specs:
            title_styles.append(
                (
                    f"Style: {spec['style_name']},{spec['font']},{spec['font_size']},"
                    f"{spec['primary_colour']},&H00FFFFFF,{spec['outline_colour']},{spec['shadow_colour']},"
                    f"0,0,0,0,100,100,{spec['spacing']},0,1,{spec['outline']},{spec['shadow']},{spec['alignment']},"
                    f"{spec['margin_l']},{spec['margin_r']},{spec['margin_v']},1"
                )
            )
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
                f"{SUBTITLE_BOLD},0,0,0,100,100,0,0,1,{SUBTITLE_OUTLINE},{SUBTITLE_SHADOW},{SUBTITLE_ALIGNMENT},"
                f"{layout.margin_l},{layout.margin_r},{layout.margin_v},1"
            ),
            *title_styles,
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        ]
    )
    lines = [header]
    for cue in title_cues or []:
        kind = str(cue.get("kind") or "title")
        if kind == "series":
            language = str(cue.get("language") or "ru")
            max_series_row_width = layout.width - series_margin_l - title_margin_h
            fit = _fit_title_series_ass_text(
                str(cue.get("text") or ""),
                str(cue.get("counter_text") or ""),
                layout=layout,
                language=language,
                side_margin_ratio=title_margin_ratio,
                max_width=max_series_row_width,
            )
            series_text = _format_title_series_title_only_ass_text(
                str(cue.get("text") or ""),
                layout=layout,
                language=language,
                side_margin_ratio=title_margin_ratio,
                max_width=max_series_row_width,
                fit=fit,
            )
            counter_text = _format_title_series_counter_ass_text(
                str(cue.get("counter_text") or ""),
                layout=layout,
                language=language,
                base_size=fit.counter_font_size if fit is not None else None,
                number_size=fit.counter_number_font_size if fit is not None else None,
                scale_x=fit.counter_scale_x if fit is not None else 100,
            )
            style_name = str(cue.get("style") or _title_series_style_name(language))
            if series_text:
                lines.append(
                    "Dialogue: 2,{start},{end},{style},,0,0,0,,{text}".format(
                        start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                        end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                        style=style_name,
                        text=series_text,
                    )
                )
            if counter_text:
                day_pos_x = _title_series_day_x(
                    layout=layout,
                    fit=fit,
                    series_left=series_margin_l,
                    title_margin_h=title_margin_h,
                    language=language,
                    side_margin_ratio=title_margin_ratio,
                )
                day_font_size = fit.counter_font_size if fit is not None else _title_series_day_font_size(layout, language)
                day_pos_y = max(
                    0,
                    int(round(base_title_margin_v - (day_font_size * _title_series_day_y_shift_ratio(language)))),
                )
                lines.append(
                    "Dialogue: 2,{start},{end},{style},,{margin_l},{margin_r},{margin_v},,{text}".format(
                        start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                        end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                        style=style_name,
                        margin_l=0,
                        margin_r=0,
                        margin_v=0,
                        text=f"{{\\q2\\pos({day_pos_x},{day_pos_y})}}" + counter_text,
                    )
                )
            continue
        else:
            text = _format_title_overlay_ass_text(
                str(cue.get("text") or " "),
                layout=layout,
                language=str(cue.get("language") or "ru"),
                side_margin_ratio=title_margin_ratio,
                top_margin_ratio=title_top_margin_ratio,
                has_series_cue=has_series_cue,
            )
            layer = 1
        if not text:
            continue
        lines.append(
            "Dialogue: {layer},{start},{end},{style},,0,0,0,,{text}".format(
                layer=layer,
                start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                style=str(cue.get("style") or _title_overlay_style_name(str(cue.get("language") or "ru"))),
                text=text,
            )
        )
    for cue in cues:
        cue_lines = str(cue.get("text") or " ").splitlines() or [" "]
        for index, line in enumerate(cue_lines):
            anchor_y = _subtitle_line_anchor_y(layout, line_count=len(cue_lines), index=index)
            text = f"{{\\an1\\pos({layout.margin_l},{anchor_y})}}" + _escape_ass_text(line)
            lines.append(
                "Dialogue: 0,{start},{end},{style},,{margin_l},{margin_r},{margin_v},,{text}".format(
                    start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                    end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                    style=SUBTITLE_STYLE_NAME,
                    margin_l=0,
                    margin_r=0,
                    margin_v=0,
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
    page_lines = [
        _balanced_subtitle_lines(page, max_line_chars=layout.line_chars)[:SUBTITLE_MAX_LINES]
        for page in pages
    ]
    if not page_lines or end_sec <= start_sec:
        return [{"start_sec": start_sec, "end_sec": end_sec, "text": " "}]

    duration = end_sec - start_sec
    cues: list[dict[str, Any]] = []
    weights = [max(1, sum(len(line.split()) for line in lines)) for lines in page_lines]
    total_weight = sum(weights)
    cursor = start_sec
    for index, lines in enumerate(page_lines):
        cue_start = cursor
        if index == len(page_lines) - 1:
            cue_end = end_sec
        else:
            cue_end = start_sec + (duration * (sum(weights[: index + 1]) / float(total_weight)))
        cues.append(
            {
                "start_sec": cue_start,
                "end_sec": cue_end,
                "text": "\n".join(_subtitle_display_text(line) for line in lines),
            }
        )
        cursor = cue_end
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
            if not text or end_sec <= start_sec:
                continue
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
    raw_text = _subtitle_source_text_for_scene(scene, language)
    pause_entries = _subtitle_entries_from_pause_markers(
        raw_text,
        scene=int(scene.get("scene") or 0) or None,
        language=language,
        start_sec=start_offset,
        end_sec=start_offset + fallback_duration,
    )
    if pause_entries:
        expanded: list[dict[str, Any]] = []
        for entry in pause_entries:
            expanded.extend(_expand_dense_subtitle_entry(entry))
        return expanded
    text = _normalize_subtitle_text(raw_text)
    if not text and str(language or "").strip().lower() != "ru":
        scene_no = int(scene.get("scene") or 0) or None
        raise RuntimeError(
            f"Missing language-specific subtitle text for scene {scene_no or '?'} ({language})."
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


def _subtitle_entries_from_pause_markers(
    text: str,
    *,
    scene: int | None,
    language: str,
    start_sec: float,
    end_sec: float,
) -> list[dict[str, Any]]:
    raw = str(text or "")
    if not SUBTITLE_PAUSE_MARKER_RE.search(raw):
        return []
    parts = [_normalize_subtitle_text(part) for part in SUBTITLE_PAUSE_MARKER_RE.split(raw)]
    parts = [part for part in parts if part]
    if len(parts) <= 1 or end_sec <= start_sec:
        return []
    weights = [max(1, len(part.split())) for part in parts]
    total_weight = sum(weights)
    duration = end_sec - start_sec
    cursor = start_sec
    out: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if index == len(parts) - 1:
            next_cursor = end_sec
        else:
            next_cursor = start_sec + (duration * (sum(weights[: index + 1]) / float(total_weight)))
        out.append(
            {
                "scene": scene,
                "language": language,
                "text": part,
                "start_sec": cursor,
                "end_sec": next_cursor,
            }
        )
        cursor = next_cursor
    return out


def _scene_with_candidate_subtitle_snapshot(
    scene: dict[str, Any],
    selected: dict[str, Any] | None,
    language: str,
    *,
    use_candidate_subtitle_snapshot: bool = True,
) -> dict[str, Any]:
    if not use_candidate_subtitle_snapshot or not isinstance(selected, dict):
        return scene
    lang = str(language or "").strip().lower()
    out = dict(scene)
    for key in ("speech_by_lang", "speech_cues_by_lang", "speech_plan_by_lang", "video_prompt_by_lang"):
        snapshot = selected.get(key)
        if not isinstance(snapshot, dict) or not snapshot.get(lang):
            continue
        merged = dict(out.get(key) or {}) if isinstance(out.get(key), dict) else {}
        merged[lang] = snapshot.get(lang)
        out[key] = merged
    return out


def _speech_text_from_prompt(prompt: Any) -> str:
    raw = str(prompt or "")
    match = re.search(r"(?is)\bCHARACTERS\s+SPEAKS:\s*(.+?)(?:\n|$)", raw)
    return " ".join(str(match.group(1) if match else "").split())


def _subtitle_source_text_for_scene(scene: dict[str, Any], language: str) -> str:
    lang = str(language or "").strip().lower()
    speech_map = scene.get("speech_by_lang")
    if isinstance(speech_map, dict):
        direct = str(speech_map.get(lang) or "").strip()
        if direct:
            return direct
    prompt_map = scene.get("video_prompt_by_lang")
    if isinstance(prompt_map, dict):
        from_prompt = _speech_text_from_prompt(prompt_map.get(lang))
        if from_prompt:
            return from_prompt
    if lang == "ru":
        return str(scene.get("text") or _speech_text_from_prompt(scene.get("video_prompt")) or "").strip()
    return ""


def _selected_video_source_duration(selected: dict[str, Any] | None) -> float | None:
    if not isinstance(selected, dict):
        return None
    raw_b64 = str(selected.get("video_b64") or "").strip()
    if not raw_b64:
        return None
    try:
        video_bytes = base64.b64decode(raw_b64)
    except Exception:
        return None
    if not video_bytes:
        return None
    with tempfile.TemporaryDirectory(prefix="post_production_subtitle_probe_") as tmp:
        path = Path(tmp) / "scene_video.mp4"
        path.write_bytes(video_bytes)
        return _media_duration(path)


def _is_speechless_final_scene(scene: dict[str, Any]) -> bool:
    mode = str(scene.get("generation_mode") or scene.get("mode") or "").strip().lower()
    model = str(scene.get("model") or "").strip().lower()
    return mode == "silence" or model.startswith("kling")


def _final_scene_speed_coef(scene: dict[str, Any], selected: dict[str, Any] | None) -> float:
    if _is_speechless_final_scene(scene):
        return 1.0
    value = parse_speed_coef(selected.get("speed_coef")) if isinstance(selected, dict) else None
    return value or DEFAULT_SCENE_VIDEO_SPEED_COEF


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


def _upscale_master_to_4k(*, ffmpeg: str, src_path: Path, dest_path: Path) -> None:
    vf = (
        f"scale={UPSCALED_4K_WIDTH}:{UPSCALED_4K_HEIGHT}:flags=lanczos,"
        "unsharp=5:5:0.45:3:3:0.0"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src_path),
        "-vf",
        vf,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
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
    _run_ffmpeg(cmd, timeout=FFMPEG_UPSCALE_TIMEOUT_SEC, stage="upscale_master_to_4k")


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
            _run_ffmpeg(cmd, timeout=FFMPEG_UPSCALE_TIMEOUT_SEC, stage="render_subtitled_music_master_mix")
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
        _run_ffmpeg(cmd, timeout=FFMPEG_UPSCALE_TIMEOUT_SEC, stage="render_subtitled_music_master_music_only")
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
    _run_ffmpeg(cmd, timeout=FFMPEG_UPSCALE_TIMEOUT_SEC, stage="render_subtitled_music_master_subtitles_only")
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
        if all(resolve_latest_approved_scene_video_for_final_build(scene, language) is not None for scene in scenes):
            ready.append(language)
    return ready


def build_post_production(
    scenes: list[dict[str, Any]],
    *,
    project_slug: str | None = None,
    overlay_title_by_lang: dict[str, Any] | None = None,
    overlay_title_style: dict[str, Any] | None = None,
    overlay_series_title_by_lang: dict[str, Any] | None = None,
    overlay_series_counter_by_lang: dict[str, Any] | None = None,
    target_languages: list[str] | tuple[str, ...] | set[str] | None = None,
    target_variants: list[str] | tuple[str, ...] | set[str] | None = None,
    progress_callback: Any | None = None,
    use_candidate_subtitle_snapshot: bool = True,
) -> dict[str, Any]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    languages = production_languages_for_scenes(scenes)
    if target_languages:
        wanted_languages = {str(item).strip().lower() for item in target_languages if str(item).strip()}
        languages = [language for language in languages if language in wanted_languages]
    if not languages:
        raise RuntimeError("No complete language tracks are available for final production.")
    allowed_variants = {"clean", "upscaled_4k", "subtitled_music", "titled_subtitled_music"}
    requested_variants = (
        {str(item).strip() for item in target_variants if str(item).strip()}
        if target_variants
        else set(allowed_variants)
    )
    requested_variants &= allowed_variants
    if not requested_variants:
        raise RuntimeError("No final render variants were requested.")

    def emit_progress(language: str, stage: str, label: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                {
                    "language": language,
                    "stage": stage,
                    "label": label,
                    "requested_variants": sorted(requested_variants),
                }
            )
        except Exception:
            logger.exception("post_production.progress_callback_failed language=%s stage=%s", language, stage)

    music_path, music_volume = _resolve_music_path(project_slug)
    renders_by_language: dict[str, dict[str, Any]] = {}
    subtitle_entries_by_language: dict[str, list[dict[str, Any]]] = {}
    subtitle_fingerprint_by_language: dict[str, str] = {}
    original_audio_preserved_by_language: dict[str, bool] = {}
    title_overlay_by_language: dict[str, dict[str, Any]] = {}
    series_overlay_by_language: dict[str, dict[str, Any]] = {}
    artifact_bundle_key = _make_artifact_bundle_key(scenes, project_slug=project_slug)

    for language in languages:
        selected_segments: list[dict[str, Any]] = []
        for scene_index, scene in enumerate(scenes):
            selected = resolve_latest_approved_scene_video_for_final_build(scene, language)
            if selected is None:
                continue
            video_b64 = str(selected.get("video_b64") or "").strip()
            if not video_b64:
                continue
            try:
                video_bytes = base64.b64decode(video_b64)
            except Exception:
                continue
            speed_coef = _final_scene_speed_coef(scene, selected)
            source_duration = float(scene.get("duration_sec") or 0) or 0.0
            planned_final_duration = source_duration / speed_coef if source_duration > 0 else 0.0
            selected_segments.append(
                {
                    "scene_index": scene_index,
                    "scene": scene,
                    "selected": selected,
                    "language": language,
                    "video_bytes": video_bytes,
                    "candidate_id": selected.get("candidate_id"),
                    "speed_coef": speed_coef,
                    "source_duration": source_duration,
                    "planned_final_duration": planned_final_duration,
                }
            )
        if not selected_segments:
            continue

        with tempfile.TemporaryDirectory(prefix=f"post_production_{language}_") as tmp:
            emit_progress(language, "normalizing", "Preparing scene clips")
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
            emit_progress(language, "clean_building", "Building clean master")
            _concat_segments(ffmpeg=ffmpeg, list_path=concat_list, dest_path=clean_path)
            clean_w, clean_h = _video_dimensions(clean_path)
            emit_progress(language, "clean_ready", "Clean master ready")
            needs_upscaled_4k = bool(
                requested_variants & {"upscaled_4k", "subtitled_music", "titled_subtitled_music"}
            )
            upscaled_path: Path | None = None
            upscaled_w: int | None = None
            upscaled_h: int | None = None
            if needs_upscaled_4k:
                upscaled_path = tmp_path / f"final_upscaled_4k_master_{language}.mp4"
                emit_progress(language, "upscale_building", "Upscaling to 4K")
                _upscale_master_to_4k(ffmpeg=ffmpeg, src_path=clean_path, dest_path=upscaled_path)
                upscaled_w, upscaled_h = _video_dimensions(upscaled_path)
                emit_progress(language, "upscale_ready", "4K master ready")
            render_base_path = upscaled_path or clean_path
            render_base_w = upscaled_w or clean_w
            render_base_h = upscaled_h or clean_h

            subtitle_entries: list[dict[str, Any]] = []
            needs_subtitled = bool(requested_variants & {"subtitled_music", "titled_subtitled_music"})
            if needs_subtitled:
                current_start = 0.0
                for item in selected_segments:
                    actual_duration = float(item.get("actual_duration") or item.get("planned_final_duration") or 0.0)
                    planned_duration = float(item.get("planned_final_duration") or 0.0)
                    timing_scale = (actual_duration / planned_duration) if planned_duration > 0 else 1.0
                    scene_entries = _scene_subtitle_entries(
                        _scene_with_candidate_subtitle_snapshot(
                            item["scene"],
                            item.get("selected"),
                            language,
                            use_candidate_subtitle_snapshot=use_candidate_subtitle_snapshot,
                        ),
                        language,
                        start_offset=current_start,
                        fallback_duration=actual_duration,
                        speed_coef=float(item["speed_coef"]),
                        timing_scale=timing_scale,
                    )
                    for entry in scene_entries:
                        entry["scene"] = int(item["scene_index"]) + 1
                        entry["candidate_id"] = str(item.get("candidate_id") or "")
                        entry["speed_coef"] = float(item["speed_coef"])
                        entry["timing_scale"] = timing_scale
                    subtitle_entries.extend(scene_entries)
                    current_start += actual_duration
                subtitle_entries_by_language[language] = subtitle_entries
                subtitle_fingerprint_by_language[language] = subtitle_entries_fingerprint(subtitle_entries)

            subtitle_cues: list[dict[str, Any]] = []
            if needs_subtitled:
                for item in subtitle_entries:
                    subtitle_cues.extend(
                        _build_burned_subtitle_cues(item, video_width=render_base_w, video_height=render_base_h)
                    )
            first_segment_duration = float(selected_segments[0].get("actual_duration") or 0.0)
            title_cue = _build_title_overlay_cue(
                overlay_title_by_lang,
                language=language,
                duration_sec=first_segment_duration,
            )
            series_cue = _build_title_series_overlay_cue(
                overlay_series_title_by_lang,
                overlay_series_counter_by_lang,
                language=language,
                duration_sec=first_segment_duration,
            )
            if title_cue:
                title_overlay_by_language[language] = {
                    "title": str(title_cue.get("text") or ""),
                    "start_sec": float(title_cue.get("start_sec") or 0.0),
                    "end_sec": float(title_cue.get("end_sec") or 0.0),
                }
            if series_cue:
                series_overlay_by_language[language] = {
                    "series_title": str(series_cue.get("text") or ""),
                    "counter": str(series_cue.get("counter_text") or ""),
                    "start_sec": float(series_cue.get("start_sec") or 0.0),
                    "end_sec": float(series_cue.get("end_sec") or 0.0),
                }
            subtitled_path: Path | None = None
            render_preserved_source_audio: bool | None = None
            if "subtitled_music" in requested_variants:
                ass_path = tmp_path / f"subtitles_{language}.ass"
                _write_burned_subtitle_ass(
                    ass_path,
                    subtitle_cues,
                    video_width=render_base_w,
                    video_height=render_base_h,
                )

                subtitled_path = tmp_path / f"final_subtitled_music_master_{language}.mp4"
                emit_progress(language, "subtitles_building", "Rendering subtitles and music")
                render_preserved_source_audio = _render_subtitled_music_master(
                    ffmpeg=ffmpeg,
                    clean_path=render_base_path,
                    subtitles_path=ass_path,
                    music_path=music_path,
                    music_volume=music_volume,
                    dest_path=subtitled_path,
                )
                emit_progress(language, "subtitles_ready", "Subtitled/music master ready")
            titled_subtitled_path: Path | None = None
            titled_render_preserved_source_audio: bool | None = None
            title_cues = [cue for cue in (series_cue, title_cue) if cue]
            if "titled_subtitled_music" in requested_variants and title_cues:
                if subtitled_path is None:
                    ass_path = tmp_path / f"subtitles_{language}.ass"
                    _write_burned_subtitle_ass(
                        ass_path,
                        subtitle_cues,
                        video_width=render_base_w,
                        video_height=render_base_h,
                    )
                    subtitled_path = tmp_path / f"final_subtitled_music_master_{language}.mp4"
                    emit_progress(language, "subtitles_building", "Rendering subtitles and music")
                    render_preserved_source_audio = _render_subtitled_music_master(
                        ffmpeg=ffmpeg,
                        clean_path=render_base_path,
                        subtitles_path=ass_path,
                        music_path=music_path,
                        music_volume=music_volume,
                        dest_path=subtitled_path,
                    )
                    emit_progress(language, "subtitles_ready", "Subtitled/music master ready")
                titled_ass_path = tmp_path / f"title_overlay_{language}.ass"
                _write_burned_subtitle_ass(
                    titled_ass_path,
                    [],
                    title_cues=title_cues,
                    title_style=overlay_title_style,
                    video_width=render_base_w,
                    video_height=render_base_h,
                )
                titled_subtitled_path = tmp_path / f"final_titled_subtitled_music_master_{language}.mp4"
                emit_progress(language, "title_building", "Burning title overlay")
                titled_render_preserved_source_audio = _render_subtitled_music_master(
                    ffmpeg=ffmpeg,
                    clean_path=subtitled_path,
                    subtitles_path=titled_ass_path,
                    music_path=None,
                    music_volume=0.0,
                    dest_path=titled_subtitled_path,
                )
                emit_progress(language, "title_ready", "Title overlay master ready")

            renders_by_language[language] = {}
            emit_progress(language, "persisting", "Saving final artifacts")
            if "clean" in requested_variants:
                clean_bytes = clean_path.read_bytes()
                clean_artifact_path = _persist_render_artifact(
                    clean_path,
                    bundle_key=artifact_bundle_key,
                    filename=_artifact_filename(project_slug=project_slug, kind="clean_master", language=language),
                )
                renders_by_language[language]["clean"] = {
                    "video_b64": _encode_b64(clean_bytes),
                    "video_path": clean_artifact_path,
                    "width": clean_w,
                    "height": clean_h,
                    "source_audio_preserved": _has_audio_stream(clean_path),
                }
            if "upscaled_4k" in requested_variants and upscaled_path:
                upscaled_bytes = upscaled_path.read_bytes()
                upscaled_artifact_path = _persist_render_artifact(
                    upscaled_path,
                    bundle_key=artifact_bundle_key,
                    filename=_artifact_filename(
                        project_slug=project_slug,
                        kind="upscaled_4k_master",
                        language=language,
                    ),
                )
                renders_by_language[language]["upscaled_4k"] = {
                    "video_b64": _encode_b64(upscaled_bytes),
                    "video_path": upscaled_artifact_path,
                    "width": upscaled_w,
                    "height": upscaled_h,
                    "source_audio_preserved": _has_audio_stream(upscaled_path),
                    "source_variant": "clean",
                }
            if "subtitled_music" in requested_variants and subtitled_path:
                subtitled_bytes = subtitled_path.read_bytes()
                sub_w, sub_h = _video_dimensions(subtitled_path)
                original_audio_preserved = (
                    bool(render_preserved_source_audio)
                    if render_preserved_source_audio is not None
                    else _has_audio_stream(render_base_path) and _has_audio_stream(subtitled_path)
                )
                subtitled_artifact_path = _persist_render_artifact(
                    subtitled_path,
                    bundle_key=artifact_bundle_key,
                    filename=_artifact_filename(
                        project_slug=project_slug,
                        kind="subtitled_music_master",
                        language=language,
                    ),
                )
                original_audio_preserved_by_language[language] = original_audio_preserved
                renders_by_language[language]["subtitled_music"] = {
                    "video_b64": _encode_b64(subtitled_bytes),
                    "video_path": subtitled_artifact_path,
                    "width": sub_w,
                    "height": sub_h,
                    "music_asset_uri": music_path,
                    "music_volume": music_volume,
                    "source_audio_preserved": original_audio_preserved,
                    "subtitle_fingerprint": subtitle_fingerprint_by_language.get(language),
                }
            if titled_subtitled_path:
                titled_bytes = titled_subtitled_path.read_bytes()
                titled_w, titled_h = _video_dimensions(titled_subtitled_path)
                titled_original_audio_preserved = (
                    bool(titled_render_preserved_source_audio)
                    if titled_render_preserved_source_audio is not None
                    else _has_audio_stream(subtitled_path) and _has_audio_stream(titled_subtitled_path)
                )
                titled_artifact_path = _persist_render_artifact(
                    titled_subtitled_path,
                    bundle_key=artifact_bundle_key,
                    filename=_artifact_filename(
                        project_slug=project_slug,
                        kind="titled_subtitled_music_master",
                        language=language,
                    ),
                )
                renders_by_language[language]["titled_subtitled_music"] = {
                    "video_b64": _encode_b64(titled_bytes),
                    "video_path": titled_artifact_path,
                    "width": titled_w,
                    "height": titled_h,
                    "music_asset_uri": music_path,
                    "music_volume": music_volume,
                    "source_audio_preserved": titled_original_audio_preserved,
                    "subtitle_fingerprint": subtitle_fingerprint_by_language.get(language),
                }
            if not renders_by_language[language]:
                renders_by_language.pop(language, None)

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
        "final_clean_video": renders_by_language[first_lang].get("clean"),
        "final_upscaled_4k_video": renders_by_language[first_lang].get("upscaled_4k"),
        "final_subtitled_music_video": renders_by_language[first_lang].get("subtitled_music"),
        "final_titled_subtitled_music_video": renders_by_language[first_lang].get("titled_subtitled_music"),
        "renders_by_language": renders_by_language,
        "subtitle_entries": subtitle_entries_by_language.get(first_lang, []),
        "subtitle_entries_by_language": subtitle_entries_by_language,
        "subtitle_fingerprint_by_language": subtitle_fingerprint_by_language,
        "title_overlay_by_language": title_overlay_by_language,
        "series_overlay_by_language": series_overlay_by_language,
        "title_overlay_style": dict(overlay_title_style or {}),
        "speech_budget_by_language": speech_budget_by_language,
        "original_audio_preserved_by_language": original_audio_preserved_by_language,
        "music_asset_uri": music_path,
        "languages": list(renders_by_language.keys()),
        "artifact_bundle_key": artifact_bundle_key,
    }


def build_upscaled_post_production_from_clean_master(
    *,
    clean_video_path: str | Path,
    language: str,
    project_slug: str | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    source_path = Path(clean_video_path)
    if not source_path.exists():
        raise RuntimeError(f"Clean master is missing: {source_path}")
    lang = str(language or "ru").strip().lower() or "ru"

    def emit_progress(stage: str, label: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                {
                    "language": lang,
                    "stage": stage,
                    "label": label,
                    "requested_variants": ["upscaled_4k"],
                    "source_variant": "clean",
                }
            )
        except Exception:
            logger.exception("post_production.progress_callback_failed language=%s stage=%s", lang, stage)

    artifact_bundle_key = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_readable_slug(project_slug, fallback='final')}_{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix=f"post_production_upscale_{lang}_") as tmp:
        tmp_path = Path(tmp)
        emit_progress("reuse_clean", "Using existing clean master")
        upscaled_path = tmp_path / f"final_upscaled_4k_master_{lang}.mp4"
        emit_progress("upscale_building", "Upscaling to 4K")
        _upscale_master_to_4k(ffmpeg=ffmpeg, src_path=source_path, dest_path=upscaled_path)
        upscaled_w, upscaled_h = _video_dimensions(upscaled_path)
        emit_progress("upscale_ready", "4K master ready")
        emit_progress("persisting", "Saving 4K artifact")
        upscaled_artifact_path = _persist_render_artifact(
            upscaled_path,
            bundle_key=artifact_bundle_key,
            filename=_artifact_filename(project_slug=project_slug, kind="upscaled_4k_master", language=lang),
        )
        payload = {
            "video_b64": _encode_b64(upscaled_path.read_bytes()),
            "video_path": upscaled_artifact_path,
            "width": upscaled_w,
            "height": upscaled_h,
            "source_audio_preserved": _has_audio_stream(upscaled_path),
            "source_variant": "clean",
            "source_video_path": str(source_path),
        }
    return {
        "final_upscaled_4k_video": payload,
        "renders_by_language": {lang: {"upscaled_4k": payload}},
        "subtitle_entries": [],
        "subtitle_entries_by_language": {},
        "title_overlay_by_language": {},
        "series_overlay_by_language": {},
        "speech_budget_by_language": {},
        "original_audio_preserved_by_language": {},
        "languages": [lang],
        "artifact_bundle_key": artifact_bundle_key,
    }


def _subtitle_entries_for_language_from_plan(
    scenes: list[dict[str, Any]],
    language: str,
    *,
    use_candidate_subtitle_snapshot: bool = True,
) -> list[dict[str, Any]]:
    current_start = 0.0
    subtitle_entries: list[dict[str, Any]] = []
    for scene_index, scene in enumerate(scenes):
        selected = resolve_latest_approved_scene_video_for_final_build(scene, language)
        speed_coef = _final_scene_speed_coef(scene, selected)
        source_duration = _selected_video_source_duration(selected)
        if source_duration is None:
            try:
                source_duration = float(scene.get("duration_sec") or 0.0)
            except (TypeError, ValueError):
                source_duration = 0.0
        planned_duration = source_duration / float(speed_coef or DEFAULT_SCENE_VIDEO_SPEED_COEF) if source_duration > 0 else 0.0
        scene_entries = _scene_subtitle_entries(
            _scene_with_candidate_subtitle_snapshot(
                scene,
                selected,
                language,
                use_candidate_subtitle_snapshot=use_candidate_subtitle_snapshot,
            ),
            language,
            start_offset=current_start,
            fallback_duration=planned_duration,
            speed_coef=float(speed_coef),
            timing_scale=1.0,
        )
        for entry in scene_entries:
            entry["scene"] = int(scene_index) + 1
            entry["candidate_id"] = str(selected.get("candidate_id") or "") if isinstance(selected, dict) else ""
            entry["speed_coef"] = float(speed_coef)
            entry["timing_scale"] = 1.0
        subtitle_entries.extend(scene_entries)
        current_start += planned_duration
    return subtitle_entries


def subtitle_entries_fingerprint(entries: list[dict[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        normalized.append(
            {
                "scene": entry.get("scene"),
                "language": str(entry.get("language") or "").strip().lower(),
                "candidate_id": str(entry.get("candidate_id") or ""),
                "text": str(entry.get("text") or ""),
                "speed_coef": round(float(entry.get("speed_coef") or 0.0), 4),
            }
        )
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def subtitle_fingerprint_for_language(
    scenes: list[dict[str, Any]],
    language: str,
    *,
    use_candidate_subtitle_snapshot: bool = True,
) -> str:
    return subtitle_entries_fingerprint(
        _subtitle_entries_for_language_from_plan(
            scenes,
            language,
            use_candidate_subtitle_snapshot=use_candidate_subtitle_snapshot,
        )
    )


def build_subtitled_post_production_from_upscaled_master(
    scenes: list[dict[str, Any]],
    *,
    upscaled_video_path: str | Path,
    language: str,
    project_slug: str | None = None,
    progress_callback: Any | None = None,
    use_candidate_subtitle_snapshot: bool = True,
) -> dict[str, Any]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    source_path = Path(upscaled_video_path)
    if not source_path.exists():
        raise RuntimeError(f"4K master is missing: {source_path}")
    lang = str(language or "ru").strip().lower() or "ru"

    def emit_progress(stage: str, label: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                {
                    "language": lang,
                    "stage": stage,
                    "label": label,
                    "requested_variants": ["subtitled_music"],
                    "source_variant": "upscaled_4k",
                }
            )
        except Exception:
            logger.exception("post_production.progress_callback_failed language=%s stage=%s", lang, stage)

    source_w, source_h = _video_dimensions(source_path)
    music_path, music_volume = _resolve_music_path(project_slug)
    subtitle_entries = _subtitle_entries_for_language_from_plan(
        scenes,
        lang,
        use_candidate_subtitle_snapshot=use_candidate_subtitle_snapshot,
    )
    subtitle_fingerprint = subtitle_entries_fingerprint(subtitle_entries)
    subtitle_cues: list[dict[str, Any]] = []
    for item in subtitle_entries:
        subtitle_cues.extend(_build_burned_subtitle_cues(item, video_width=source_w, video_height=source_h))

    artifact_bundle_key = _make_artifact_bundle_key(scenes, project_slug=project_slug)
    with tempfile.TemporaryDirectory(prefix=f"post_production_subtitles_{lang}_") as tmp:
        tmp_path = Path(tmp)
        emit_progress("reuse_upscaled", "Using existing 4K master")
        ass_path = tmp_path / f"subtitles_{lang}.ass"
        _write_burned_subtitle_ass(
            ass_path,
            subtitle_cues,
            video_width=source_w,
            video_height=source_h,
        )
        subtitled_path = tmp_path / f"final_subtitled_music_master_{lang}.mp4"
        emit_progress("subtitles_building", "Rendering subtitles and music")
        source_audio_preserved = _render_subtitled_music_master(
            ffmpeg=ffmpeg,
            clean_path=source_path,
            subtitles_path=ass_path,
            music_path=music_path,
            music_volume=music_volume,
            dest_path=subtitled_path,
        )
        emit_progress("subtitles_ready", "Subtitled/music master ready")
        subtitled_w, subtitled_h = _video_dimensions(subtitled_path)
        emit_progress("persisting", "Saving subtitled/music artifact")
        subtitled_artifact_path = _persist_render_artifact(
            subtitled_path,
            bundle_key=artifact_bundle_key,
            filename=_artifact_filename(project_slug=project_slug, kind="subtitled_music_master", language=lang),
        )
        payload = {
            "video_b64": _encode_b64(subtitled_path.read_bytes()),
            "video_path": subtitled_artifact_path,
            "width": subtitled_w,
            "height": subtitled_h,
            "music_asset_uri": music_path,
            "music_volume": music_volume,
            "source_audio_preserved": bool(source_audio_preserved),
            "source_variant": "upscaled_4k",
            "source_video_path": str(source_path),
            "subtitle_fingerprint": subtitle_fingerprint,
        }
    return {
        "final_subtitled_music_video": payload,
        "renders_by_language": {lang: {"subtitled_music": payload}},
        "subtitle_entries": subtitle_entries,
        "subtitle_entries_by_language": {lang: subtitle_entries},
        "subtitle_fingerprint_by_language": {lang: subtitle_fingerprint},
        "title_overlay_by_language": {},
        "series_overlay_by_language": {},
        "speech_budget_by_language": {},
        "original_audio_preserved_by_language": {lang: bool(source_audio_preserved)},
        "music_asset_uri": music_path,
        "languages": [lang],
        "artifact_bundle_key": artifact_bundle_key,
    }


def _estimated_title_duration_for_language(scenes: list[dict[str, Any]], language: str, fallback_duration: float) -> float:
    if not scenes:
        return max(0.0, min(float(fallback_duration or 0.0), 6.0))
    first_scene = scenes[0]
    selected = resolve_latest_approved_scene_video_for_final_build(first_scene, language)
    speed_coef = _final_scene_speed_coef(first_scene, selected)
    try:
        source_duration = float(first_scene.get("duration_sec") or 0.0)
    except (TypeError, ValueError):
        source_duration = 0.0
    estimated = source_duration / float(speed_coef or DEFAULT_SCENE_VIDEO_SPEED_COEF) if source_duration > 0 else 0.0
    if estimated > 0:
        return min(estimated, float(fallback_duration or estimated))
    return max(0.0, min(float(fallback_duration or 0.0), 6.0))


def build_titled_post_production_from_subtitled_master(
    scenes: list[dict[str, Any]],
    *,
    subtitled_video_path: str | Path,
    language: str,
    project_slug: str | None = None,
    overlay_title_by_lang: dict[str, Any] | None = None,
    overlay_title_style: dict[str, Any] | None = None,
    overlay_series_title_by_lang: dict[str, Any] | None = None,
    overlay_series_counter_by_lang: dict[str, Any] | None = None,
    progress_callback: Any | None = None,
    use_candidate_subtitle_snapshot: bool = True,
) -> dict[str, Any]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    source_path = Path(subtitled_video_path)
    if not source_path.exists():
        raise RuntimeError(f"Subtitled/music master is missing: {source_path}")

    lang = str(language or "ru").strip().lower() or "ru"
    source_w, source_h = _video_dimensions(source_path)
    source_duration = _media_duration(source_path) or 0.0
    subtitle_fingerprint = subtitle_fingerprint_for_language(
        scenes,
        lang,
        use_candidate_subtitle_snapshot=use_candidate_subtitle_snapshot,
    )
    title_duration = _estimated_title_duration_for_language(scenes, lang, source_duration)
    title_cue = _build_title_overlay_cue(
        overlay_title_by_lang,
        language=lang,
        duration_sec=title_duration,
    )
    series_cue = _build_title_series_overlay_cue(
        overlay_series_title_by_lang,
        overlay_series_counter_by_lang,
        language=lang,
        duration_sec=title_duration,
    )
    title_cues = [cue for cue in (series_cue, title_cue) if cue]
    if not title_cues:
        raise RuntimeError("No title overlay text is available for titled final render.")

    def emit_progress(stage: str, label: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                {
                    "language": lang,
                    "stage": stage,
                    "label": label,
                    "requested_variants": ["titled_subtitled_music"],
                    "source_variant": "subtitled_music",
                }
            )
        except Exception:
            logger.exception("post_production.progress_callback_failed language=%s stage=%s", lang, stage)

    artifact_bundle_key = _make_artifact_bundle_key(scenes, project_slug=project_slug)
    with tempfile.TemporaryDirectory(prefix=f"post_production_title_{lang}_") as tmp:
        tmp_path = Path(tmp)
        emit_progress("reuse_subtitled", "Using existing subtitled/music master")
        titled_ass_path = tmp_path / f"title_overlay_{lang}.ass"
        _write_burned_subtitle_ass(
            titled_ass_path,
            [],
            title_cues=title_cues,
            title_style=overlay_title_style,
            video_width=source_w,
            video_height=source_h,
        )
        titled_path = tmp_path / f"final_titled_subtitled_music_master_{lang}.mp4"
        emit_progress("title_building", "Burning title overlay")
        source_audio_preserved = _render_subtitled_music_master(
            ffmpeg=ffmpeg,
            clean_path=source_path,
            subtitles_path=titled_ass_path,
            music_path=None,
            music_volume=0.0,
            dest_path=titled_path,
        )
        emit_progress("title_ready", "Title overlay master ready")
        titled_w, titled_h = _video_dimensions(titled_path)
        emit_progress("persisting", "Saving title/subtitled/music artifact")
        titled_artifact_path = _persist_render_artifact(
            titled_path,
            bundle_key=artifact_bundle_key,
            filename=_artifact_filename(
                project_slug=project_slug,
                kind="titled_subtitled_music_master",
                language=lang,
            ),
        )
        payload = {
            "video_b64": _encode_b64(titled_path.read_bytes()),
            "video_path": titled_artifact_path,
            "width": titled_w,
            "height": titled_h,
            "music_asset_uri": None,
            "music_volume": 0.0,
            "source_audio_preserved": bool(source_audio_preserved),
            "source_variant": "subtitled_music",
            "source_video_path": str(source_path),
            "subtitle_fingerprint": subtitle_fingerprint,
        }

    title_overlay_by_language = {}
    if title_cue:
        title_overlay_by_language[lang] = {
            "title": str(title_cue.get("text") or ""),
            "start_sec": float(title_cue.get("start_sec") or 0.0),
            "end_sec": float(title_cue.get("end_sec") or 0.0),
        }
    series_overlay_by_language = {}
    if series_cue:
        series_overlay_by_language[lang] = {
            "series_title": str(series_cue.get("text") or ""),
            "counter": str(series_cue.get("counter_text") or ""),
            "start_sec": float(series_cue.get("start_sec") or 0.0),
            "end_sec": float(series_cue.get("end_sec") or 0.0),
        }
    return {
        "final_titled_subtitled_music_video": payload,
        "renders_by_language": {lang: {"titled_subtitled_music": payload}},
        "subtitle_entries": [],
        "subtitle_entries_by_language": {},
        "subtitle_fingerprint_by_language": {lang: subtitle_fingerprint},
        "title_overlay_by_language": title_overlay_by_language,
        "series_overlay_by_language": series_overlay_by_language,
        "title_overlay_style": dict(overlay_title_style or {}),
        "speech_budget_by_language": {},
        "original_audio_preserved_by_language": {lang: bool(source_audio_preserved)},
        "music_asset_uri": None,
        "languages": [lang],
        "artifact_bundle_key": artifact_bundle_key,
    }
