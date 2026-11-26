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

from . import inner_boss_mix
from .discipline_modes import FLOW, MODE_KEY
from .ai_approval_service import _resolve_ffmpeg
from .db import SessionLocal
from .series_theme import holiday_dish_color, holiday_font, holiday_music, holiday_music_filter, themed_fonts
from .models import Project
from .scene_media_candidates import (
    DEFAULT_SCENE_VIDEO_SPEED_COEF,
    default_scene_video_speed_coef,
    parse_speed_coef,
    resolve_latest_approved_scene_video_for_final_build,
    video_candidate_bytes,
)

logger = logging.getLogger("bot.video")

DEFAULT_BACKGROUND_MUSIC_VOLUME = 0.18
BACKGROUND_MUSIC_ATTENUATION_DB = 3.0
SOURCE_AUDIO_VOLUME = 1.0
DISCIPLINE_ANASTASIA_NARRATION_VOLUME = 2.0
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
UPSCALED_4K_WIDTH = 2160
UPSCALED_4K_HEIGHT = 3840
HORIZONTAL_TARGET_WIDTH = 1920
HORIZONTAL_TARGET_HEIGHT = 1080
HORIZONTAL_UPSCALED_4K_WIDTH = 3840
HORIZONTAL_UPSCALED_4K_HEIGHT = 2160
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
STORY_WORD_SYNC_SUBTITLE_COLOUR = "&H00D8EBF3&"
STORY_WORD_SYNC_PROJECT_SLUGS = {"discipline", "grandma_kitchen", "kitchen"}
STORY_WORD_SYNC_MAX_WORDS = 4
STORY_WORD_SYNC_FONT_NAME = "ZyruaCitrus"
STORY_WORD_SYNC_FONT_PATH_ENV_NAME = "STORY_SUBTITLE_FONT_PATH"
STORY_WORD_SYNC_FONT_SIZE_RATIO = 0.75
STORY_WORD_SYNC_ANCHOR_Y_RATIO = 0.81
STORY_WORD_SYNC_STYLE_VERSION = "metel_fixed_blocks_v2"
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
TITLE_OVERLAY_BOLD = -1
# The reference has solid, heavy letters and a hard offset shadow, no contour.
TITLE_OVERLAY_OUTLINE = 0
TITLE_OVERLAY_EN_OUTLINE = 5
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
STORY_SERIES_COUNTER_FONT_SCALE = 1.35
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
TITLE_SUBTITLE_STYLE_PREFIX = "TitleSubtitle"
TITLE_SUBTITLE_FONT_BY_LANGUAGE = dict(TITLE_SERIES_FONT_BY_LANGUAGE)
TITLE_SUBTITLE_FONT_FILE_BY_LANGUAGE = dict(TITLE_SERIES_FONT_FILE_BY_LANGUAGE)
TITLE_SUBTITLE_DEFAULT_FONT = TITLE_SERIES_DEFAULT_FONT
TITLE_SUBTITLE_DEFAULT_FONT_FILE = TITLE_SERIES_DEFAULT_FONT_FILE
TITLE_SUBTITLE_FALLBACK_FONT = "Arial Narrow"
TITLE_SUBTITLE_FALLBACK_FONT_FILE = "Arial Narrow Bold.ttf"
TITLE_SUBTITLE_VERTICAL_FONT_SIZE = 40
TITLE_SUBTITLE_HORIZONTAL_FONT_SIZE = 30
TITLE_SUBTITLE_SQUARE_FONT_SIZE = 34
TITLE_SUBTITLE_PRIMARY_COLOUR = TITLE_SERIES_PRIMARY_COLOUR
TITLE_SUBTITLE_OUTLINE_COLOUR = TITLE_SERIES_OUTLINE_COLOUR
TITLE_SUBTITLE_SHADOW_COLOUR = TITLE_SERIES_SHADOW_COLOUR
TITLE_SUBTITLE_OUTLINE = TITLE_SERIES_OUTLINE
TITLE_SUBTITLE_SHADOW = TITLE_SERIES_SHADOW
TITLE_SUBTITLE_GAP_RATIO = 0.010
COMBINING_ACCENT_MARKS_RE = re.compile(r"[\u0300-\u036f]")
CYRILLIC_WORD_RE = re.compile(r"[А-Яа-яЁё]+(?:[А-Яа-яЁё]+)?")
SUBTITLE_PAUSE_MARKER_RE = re.compile(r"\[\s*(?:pause|пауза)\s*\]", re.IGNORECASE)
PROMPT_SPEECH_TERMINATOR_RE = re.compile(
    r"(?is)\s+(?="
    r"(?:voice_lock(?:_[a-z0-9]+)*|speech_timing_plan|veo3_framing|framing)\s*:"
    r"|\b(?:natural-fast spoken delivery|render this video|never use horizontal landscape framing)\b"
    r")"
)
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


def _render_dimensions_for_scenes(scenes: list[dict[str, Any]]) -> tuple[int, int]:
    if any(str(scene.get("aspect_ratio") or "").strip() == "16:9" for scene in scenes if isinstance(scene, dict)):
        return HORIZONTAL_TARGET_WIDTH, HORIZONTAL_TARGET_HEIGHT
    return TARGET_WIDTH, TARGET_HEIGHT


def _upscaled_dimensions_for_render(width: int, height: int) -> tuple[int, int]:
    if int(width or 0) > int(height or 0):
        return HORIZONTAL_UPSCALED_4K_WIDTH, HORIZONTAL_UPSCALED_4K_HEIGHT
    return UPSCALED_4K_WIDTH, UPSCALED_4K_HEIGHT
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


def _story_subtitle_font_path() -> Path | None:
    raw = (os.getenv(STORY_WORD_SYNC_FONT_PATH_ENV_NAME) or "").strip()
    candidates = [Path(raw)] if raw else []
    candidates.extend(
        [
            _project_root() / "fonts" / "ZYRUA Citrus.ttf",
            Path.home()
            / "Movies"
            / "CapCut"
            / "User Data"
            / "Cache"
            / "effect"
            / "7013643779252949505"
            / "bc22ab40d294bba5c9eb3e5ffa954a98"
            / "ZYRUA Citrus.ttf",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


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


def selected_scene_video_fingerprint_for_language(scenes: list[dict[str, Any]], language: str) -> str:
    lang = str(language or "ru").strip().lower() or "ru"
    selected: list[dict[str, Any]] = []
    for scene_index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        candidate = resolve_latest_approved_scene_video_for_final_build(scene, lang)
        if candidate is None:
            continue
        selected.append(
            {
                "scene_index": scene_index,
                "scene": scene.get("scene"),
                "candidate_id": candidate.get("candidate_id"),
                "created_at": candidate.get("created_at"),
                "speed_coef": parse_speed_coef(candidate.get("speed_coef")) or default_scene_video_speed_coef(scene),
                "byte_size": candidate.get("byte_size"),
                "media_uri": candidate.get("media_uri"),
                "source_image_candidate_id": candidate.get("source_image_candidate_id"),
                "duration_sec": scene.get("duration_sec"),
                "timeline_duration_sec": scene.get("timeline_duration_sec"),
                "timeline_duration_by_lang": scene.get("timeline_duration_by_lang"),
                **({MODE_KEY: FLOW} if scene.get(MODE_KEY) == FLOW else {}),
            }
        )
    raw = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def _subtitle_display_text(text: str, *, preserve_case: bool = False) -> str:
    display = str(text or "")
    return display.upper() if SUBTITLE_UPPERCASE and not preserve_case else display


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
    custom = holiday_font("dish", code)
    if custom:
        return custom[0]
    return TITLE_OVERLAY_FONT_BY_LANGUAGE.get(code) or TITLE_OVERLAY_DEFAULT_FONT


def _title_overlay_font_path(language: str) -> Path | None:
    code = str(language or "").strip().lower()
    custom = holiday_font("dish", code)
    if custom:
        return subtitle_fonts_dir() / custom[1]
    filename = TITLE_OVERLAY_FONT_FILE_BY_LANGUAGE.get(code) or TITLE_OVERLAY_DEFAULT_FONT_FILE
    path = subtitle_fonts_dir() / filename
    return path if path.exists() else None


def _title_series_style_name(language: str) -> str:
    code = str(language or "").strip().lower()
    return f"{TITLE_SERIES_STYLE_PREFIX}{code.upper() if code else 'RU'}"


def _title_subtitle_style_name(language: str) -> str:
    code = str(language or "").strip().lower()
    return f"{TITLE_SUBTITLE_STYLE_PREFIX}{code.upper() if code else 'RU'}"


def _title_overlay_ass_safe_width_ratio(language: str) -> float:
    code = str(language or "").strip().lower()
    return float(TITLE_OVERLAY_ASS_SAFE_WIDTH_RATIO_BY_LANGUAGE.get(code) or TITLE_OVERLAY_ASS_SAFE_WIDTH_RATIO)


def _title_series_font_name(language: str) -> str:
    code = str(language or "").strip().lower()
    custom = holiday_font("series", code)
    if custom:
        return custom[0]
    return TITLE_SERIES_FONT_BY_LANGUAGE.get(code) or TITLE_SERIES_DEFAULT_FONT


def _title_series_font_path(language: str) -> Path | None:
    code = str(language or "").strip().lower()
    custom = holiday_font("series", code)
    if custom:
        return subtitle_fonts_dir() / custom[1]
    filename = TITLE_SERIES_FONT_FILE_BY_LANGUAGE.get(code) or TITLE_SERIES_DEFAULT_FONT_FILE
    path = subtitle_fonts_dir() / filename
    return path if path.exists() else None


def _title_series_day_font_path() -> Path | None:
    path = subtitle_fonts_dir() / TITLE_SERIES_DAY_FONT_FILE
    return path if path.exists() else None


def _title_subtitle_font_path(language: str = "") -> Path | None:
    code = str(language or "").strip().lower()
    filename = TITLE_SUBTITLE_FONT_FILE_BY_LANGUAGE.get(code) or TITLE_SUBTITLE_DEFAULT_FONT_FILE
    local_path = subtitle_fonts_dir() / filename
    if local_path.exists():
        return local_path
    default_path = subtitle_fonts_dir() / TITLE_SUBTITLE_DEFAULT_FONT_FILE
    if default_path.exists():
        return default_path
    fallback_path = subtitle_fonts_dir() / TITLE_SUBTITLE_FALLBACK_FONT_FILE
    if fallback_path.exists():
        return fallback_path
    system_path = Path("/System/Library/Fonts/Supplemental") / TITLE_SUBTITLE_FALLBACK_FONT_FILE
    if system_path.exists():
        return system_path
    return fallback_path if fallback_path.exists() else None


def _title_subtitle_font_name(language: str = "") -> str:
    code = str(language or "").strip().lower()
    filename = TITLE_SUBTITLE_FONT_FILE_BY_LANGUAGE.get(code) or TITLE_SUBTITLE_DEFAULT_FONT_FILE
    if (subtitle_fonts_dir() / filename).exists():
        return TITLE_SUBTITLE_FONT_BY_LANGUAGE.get(code) or TITLE_SUBTITLE_DEFAULT_FONT
    if (subtitle_fonts_dir() / TITLE_SUBTITLE_DEFAULT_FONT_FILE).exists():
        return TITLE_SUBTITLE_DEFAULT_FONT
    if (
        (subtitle_fonts_dir() / TITLE_SUBTITLE_FALLBACK_FONT_FILE).exists()
        or (Path("/System/Library/Fonts/Supplemental") / TITLE_SUBTITLE_FALLBACK_FONT_FILE).exists()
    ):
        return TITLE_SUBTITLE_FALLBACK_FONT
    return TITLE_SUBTITLE_FALLBACK_FONT


def _title_series_ass_scale_x(language: str) -> int:
    code = str(language or "").strip().lower()
    return int(TITLE_SERIES_ASS_SCALE_X_BY_LANGUAGE.get(code) or 100)


def _title_series_width_ratio(language: str) -> float:
    code = str(language or "").strip().lower()
    if holiday_font("series", code):
        return 0.80
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
    if holiday_font("series", code):
        # Pudelinka renders at ~70% of Pillow metrics in libass.
        return 0.70
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


def _title_day_ink_left_offset(text: str, font_size: int) -> int:
    """Compensate Marianna's overhanging strokes in libass coordinates."""
    path = _title_series_day_font_path()
    if path is None or not text.strip():
        return 0
    font = ImageFont.truetype(str(path), size=1000)
    mask, offset = font.getmask2(text.strip())
    bounds = mask.getbbox()
    if bounds is None:
        return 0
    ascent, descent = font.getmetrics()
    # libass normalizes the font to its ascender + descender height.
    return int(round((offset[0] + bounds[0]) * font_size / (ascent + descent)))


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


def _title_subtitle_font_size(layout: SubtitleLayout) -> int:
    scale = layout.height / 1920.0 if layout.height > layout.width else layout.width / 1080.0
    if layout.height > layout.width:
        base = TITLE_SUBTITLE_VERTICAL_FONT_SIZE
    elif layout.width > layout.height:
        base = TITLE_SUBTITLE_HORIZONTAL_FONT_SIZE
    else:
        base = TITLE_SUBTITLE_SQUARE_FONT_SIZE
    return int(round(base * scale))


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


def _measure_title_line(
    draw: ImageDraw.ImageDraw, line: str, font: ImageFont.FreeTypeFont,
    *, stroke_width: int = TITLE_OVERLAY_OUTLINE,
) -> int:
    bbox = draw.textbbox((0, 0), line or " ", font=font, stroke_width=stroke_width)
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
    stroke_width: int = TITLE_OVERLAY_OUTLINE,
) -> tuple[int, int]:
    if not lines:
        return 0, 0
    bbox = draw.multiline_textbbox(
        (0, 0),
        "\n".join(lines),
        font=font,
        spacing=spacing,
        stroke_width=stroke_width,
    )
    width = max(_measure_title_line(draw, line, font, stroke_width=stroke_width) for line in lines)
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


def _title_overlay_outline(language: str) -> int:
    # Keep the established Taller treatment for English titles.
    return TITLE_OVERLAY_EN_OUTLINE if str(language).strip().lower() == "en" else TITLE_OVERLAY_OUTLINE


def _title_overlay_shadow(layout: SubtitleLayout, language: str = "ru") -> int:
    if str(language).strip().lower() == "en":
        return TITLE_OVERLAY_SHADOW
    # ASS shadow units follow PlayRes: a 4K canvas needs twice the HD offset.
    scale = min(layout.width, layout.height) / float(TARGET_WIDTH)
    return max(1, int(round(TITLE_OVERLAY_SHADOW * scale)))


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
    outline = _title_overlay_outline(language)
    max_width = max(
        1,
        int(
            (
                layout.width
                - (margin_h * 2)
                - _title_overlay_shadow(layout, language)
                - (outline * 2)
            )
            * _title_overlay_ass_safe_width_ratio(language)
        ),
    )
    _ = (top_margin_ratio, has_series_cue)
    max_height = max(1, int(layout.height * TITLE_OVERLAY_MAX_HEIGHT_RATIO))
    # SnowCapCyr has different font ascent/descent metrics in libass. Fit
    # to the existing visible title box, not the larger Pillow measurement.
    render_scale = 0.81 if holiday_font("dish", language) else 1.0
    max_width = int(max_width / render_scale)
    max_height = int(max_height / render_scale)
    base_size = _title_overlay_font_size(layout, clean)
    max_size = max(base_size, int(layout.height * 0.42))
    min_size = max(32, int(round(base_size * 0.45)))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    for size in range(max_size, min_size - 1, -2):
        font = ImageFont.truetype(str(font_path), size=size)
        lines = [clean]
        spacing = max(8, int(size * 0.10))
        width, height = _measure_title_block(draw, lines, font, spacing=spacing, stroke_width=outline)
        if width <= max_width and height <= max_height:
            return TitleOverlayFit("\n".join(lines), size, int(width * render_scale), int(height * render_scale))
    font = ImageFont.truetype(str(font_path), size=min_size)
    width, height = _measure_title_block(
        draw, [clean], font, spacing=max(8, int(min_size * 0.10)), stroke_width=outline,
    )
    return TitleOverlayFit(clean, min_size, int(width * render_scale), int(height * render_scale))


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

    separate_counter = bool(holiday_font("series", language))
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
        fits_height = not separate_counter or height * 0.70 <= layout.height * 0.065
        if width <= series_target_width and fits_height:
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
    fitted_width = series_width if separate_counter else series_width + gap_width + scaled_counter_width
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
        fitted_width = series_width if separate_counter else series_width + gap_width + scaled_counter_width
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


def _fit_title_subtitle_ass_text(
    text: str,
    *,
    layout: SubtitleLayout,
    language: str = "",
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
) -> TitleOverlayFit | None:
    clean = " ".join(str(text or "").split())
    font_path = _title_subtitle_font_path(language)
    if not clean or font_path is None:
        return None

    margin_h = int(layout.width * side_margin_ratio)
    max_width = max(1, layout.width - (margin_h * 2) - TITLE_SUBTITLE_SHADOW - (TITLE_SUBTITLE_OUTLINE * 2))
    base_size = _title_subtitle_font_size(layout)
    max_size = max(base_size, int(round(layout.height * 0.065)))
    min_size = max(24, int(round(base_size * 0.62)))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in range(max_size, min_size - 1, -2):
        font = ImageFont.truetype(str(font_path), size=size)
        width, height = _measure_title_block(draw, [clean], font, spacing=0)
        if width <= max_width:
            return TitleOverlayFit(clean, size, width, height)
    font = ImageFont.truetype(str(font_path), size=min_size)
    width, height = _measure_title_block(draw, [clean], font, spacing=0)
    return TitleOverlayFit(clean, min_size, width, height)


def _format_title_subtitle_ass_text(
    text: str,
    *,
    layout: SubtitleLayout,
    language: str = "",
    side_margin_ratio: float = TITLE_OVERLAY_MARGIN_RATIO,
    fit: TitleOverlayFit | None = None,
) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        return ""
    try:
        resolved_fit = fit or _fit_title_subtitle_ass_text(
            clean,
            layout=layout,
            language=language,
            side_margin_ratio=side_margin_ratio,
        )
    except Exception:
        logger.exception("post_production.title_subtitle_fit_failed")
        resolved_fit = None
    font_size = resolved_fit.font_size if resolved_fit is not None else _title_subtitle_font_size(layout)
    return f"{{\\fscx100\\fs{font_size}}}" + _escape_ass_no_wrap_spaces(clean)


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


def _extra_title_cues(style: dict | None, language: str, duration: float) -> list[dict]:
    raw = ((style or {}).get("extra_title_cues_by_lang") or {}).get(language) or []
    result = []
    for cue in raw:
        if not isinstance(cue, dict) or not str(cue.get("text") or "").strip():
            continue
        try:
            start = max(0.0, float(cue.get("start_sec", 0)))
            end = min(duration, float(cue.get("end_sec", 0)))
        except (TypeError, ValueError):
            continue
        if end > start:
            result.append({**cue, "start_sec": start, "end_sec": end,
                           "language": language, "kind": "title"})
    return result


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


def _scene_end_title_config(scene: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    meta = scene.get("prompt_meta_json") if isinstance(scene.get("prompt_meta_json"), dict) else {}
    raw_titles = scene.get("end_title_by_lang")
    if not isinstance(raw_titles, dict):
        raw_titles = meta.get("end_title_by_lang")
    titles = {
        str(key).strip().lower(): " ".join(str(value or "").split())
        for key, value in (raw_titles or {}).items()
        if str(key).strip() and str(value or "").strip()
    } if isinstance(raw_titles, dict) else {}
    raw_style = scene.get("end_title_style")
    if not isinstance(raw_style, dict):
        raw_style = meta.get("end_title_style")
    return titles, dict(raw_style) if isinstance(raw_style, dict) else {}


def _build_end_title_cue(
    scene: dict[str, Any],
    *,
    language: str,
    start_sec: float,
    end_sec: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    titles, style = _scene_end_title_config(scene)
    lang = str(language or "ru").strip().lower() or "ru"
    title = str(titles.get(lang) or "").strip()
    if not title:
        return None, style
    if bool(style.get("uppercase", True)):
        title = title.upper()
    start = max(0.0, float(start_sec or 0.0))
    end = max(start, float(end_sec or 0.0))
    if end <= start:
        return None, style
    return (
        {
            "kind": "end_title",
            "text": title,
            "start_sec": start,
            "end_sec": end,
            "style": f"EndTitle_{lang.upper()}",
            "language": lang,
            "font_name": str(style.get("font_name") or "Arial Narrow"),
            "font_size_ratio": float(style.get("font_size_ratio") or 0.028),
            "vertical_anchor_ratio": float(style.get("vertical_anchor_ratio") or 0.43),
            "tracking": float(style.get("tracking") or 3.0),
            "scale_x": int(style.get("scale_x_ru") or 82) if lang == "ru" else int(style.get("scale_x") or 100),
            "title_color": str(style.get("title_color") or "#D2A66D"),
            "outline": max(0.0, float(style.get("outline") or 0.0)),
            "shadow": max(0.0, float(style.get("shadow") or 1.0)),
        },
        style,
    )


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
        # Counter wording must not change the established series typography.
        "layout_variant": "default",
        "start_sec": 0.0,
        "end_sec": duration,
        "style": _title_series_style_name(lang),
        "language": lang,
    }


def _build_title_subtitle_overlay_cue(
    overlay_subtitle_by_lang: dict[str, Any] | None,
    *,
    language: str,
    duration_sec: float,
) -> dict[str, Any] | None:
    subtitle = _overlay_value_for_language(overlay_subtitle_by_lang, language)
    duration = max(0.0, float(duration_sec or 0.0))
    if not subtitle or duration <= 0:
        return None
    lang = str(language or "ru").strip().lower() or "ru"
    return {
        "kind": "subtitle",
        "text": subtitle,
        "start_sec": 0.0,
        "end_sec": duration,
        "style": _title_subtitle_style_name(lang),
        "language": lang,
    }


def _story_fixed_block_ass_text(cue: dict[str, Any], layout: SubtitleLayout, title_style=None) -> str | None:
    raw_words = cue.get("fixed_block_words")
    if not isinstance(raw_words, list):
        return None
    words = [str(word or "").strip() for word in raw_words if str(word or "").strip()]
    if not words:
        return None
    try:
        visible_count = int(cue.get("visible_word_count") or 0)
    except (TypeError, ValueError):
        visible_count = 0
    visible_count = max(0, min(len(words), visible_count))
    visible = " ".join(_escape_ass_text(word) for word in words[:visible_count])
    hidden = " ".join(_escape_ass_text(word) for word in words[visible_count:])
    body = visible
    if hidden:
        body += (" " if body else "") + r"{\alpha&HFF&}" + hidden + r"{\alpha&H00&}"

    font_size = max(20, int(round(layout.font_size * STORY_WORD_SYNC_FONT_SIZE_RATIO)))
    resolution_scale = max(0.5, layout.height / 1920.0)
    outline = max(2, int(round(3.0 * resolution_scale)))
    shadow = max(1, int(round(1.5 * resolution_scale)))
    anchor_x = layout.width // 2
    from .split_panel_typography import caption_anchor
    anchor_ratio = caption_anchor(title_style)
    anchor_y = int(round(layout.height * (anchor_ratio if anchor_ratio is not None else STORY_WORD_SYNC_ANCHOR_Y_RATIO)))
    prefix = (
        "{"
        f"\\an2\\pos({anchor_x},{anchor_y})"
        f"\\fn{STORY_WORD_SYNC_FONT_NAME}\\fs{font_size}\\b1\\q2"
        f"\\c{STORY_WORD_SYNC_SUBTITLE_COLOUR}"
        "\\3c&H00000000&\\4c&H70000000&"
        f"\\bord{outline}\\shad{shadow}"
        "}"
    )
    return prefix + body


def _without_large_title_words(cues, title_cues, style, language):
    """Remove caption words replaced by aligned titles, including later block echoes."""
    if not style.get("replace_subtitle_phrases"):
        return cues
    normalize = lambda text: re.sub(r"[^\w]+", "", str(text)).casefold()
    replacements = []
    for title in title_cues:
        if (title.get("kind") or "title") != "title":
            continue
        timing = title.get("word_timing", (style.get("word_timing_by_lang") or {}).get(language)) or []
        if [normalize(w.get("text")) for w in timing] == [normalize(w) for w in title.get("text", "").split()]:
            replacements.extend(timing)

    def replaced(word, start):
        ranges = (style.get('subtitle_suppression_ranges_by_lang') or {}).get(language, [])
        if any(float(begin) - .01 <= start < float(end) for begin, end in ranges):
            return True
        return any(normalize(word) == normalize(row.get("text"))
                   and float(row["start"]) - .08 <= start < float(row["end"]) + .08
                   for row in replacements)

    result = []
    group = []

    def flush():
        if not group:
            return
        words = group[0]["fixed_block_words"]
        removed = {int(c["visible_word_count"]) - 1 for c in group
                   if 0 < int(c["visible_word_count"]) <= len(words)
                   and replaced(words[int(c["visible_word_count"]) - 1], float(c["start_sec"]))}
        kept = [w for i, w in enumerate(words) if i not in removed]
        for cue in group:
            count = int(cue["visible_word_count"])
            if count - 1 in removed:
                continue
            visible = count - sum(i < count for i in removed)
            if kept and visible:
                result.append({**cue, "fixed_block_words": kept,
                               "visible_word_count": visible, "text": " ".join(kept[:visible])})
        group.clear()

    for cue in cues:
        words = cue.get("fixed_block_words")
        if words:
            if group and (words != group[-1]["fixed_block_words"]
                          or int(cue.get("visible_word_count", 0)) <= int(group[-1]["visible_word_count"])):
                flush()
            group.append(cue)
        else:
            flush()
            # Legacy single-word captions can also be replaced without hiding neighbours.
            if not replaced(cue.get("text", ""), float(cue["start_sec"])):
                result.append(cue)
    flush()
    return result


@themed_fonts
def _write_burned_subtitle_ass(
    path: Path,
    cues: list[dict[str, Any]],
    *,
    title_cues: list[dict[str, Any]] | None = None,
    title_style: dict[str, Any] | None = None,
    accent_cues: list[dict[str, Any]] | None = None,
    video_width: int | None = None,
    video_height: int | None = None,
) -> None:
    layout = _subtitle_layout_for_dimensions(video_width, video_height)
    font_name = _subtitle_font_name()
    title_style = title_style if isinstance(title_style, dict) else {}
    # Normalize before fitting so uppercase dimensions match the final glyphs.
    title_cues = [
        {**cue, "text": str(cue.get("text") or "").upper()}
        if str(cue.get("kind") or "title") == "title"
        and holiday_font("dish", str(cue.get("language") or "ru")) else cue
        for cue in title_cues or []
    ]
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
    story_series_layout = any(
        str(cue.get("kind") or "") == "series"
        and str(cue.get("layout_variant") or "") == "story"
        for cue in title_cues or []
    )
    series_row_offsets: list[int] = []
    for cue in title_cues or []:
        if str(cue.get("kind") or "") != "series":
            continue
        language = str(cue.get("language") or "ru").strip().lower() or "ru"
        is_story_series = str(cue.get("layout_variant") or "") == "story"
        fit = _fit_title_series_ass_text(
            str(cue.get("text") or ""),
            "" if is_story_series else str(cue.get("counter_text") or ""),
            layout=layout,
            language=language,
            side_margin_ratio=title_margin_ratio,
            max_width=layout.width - (title_margin_h * 2),
        )
        if is_story_series:
            series_font_size = fit.series_font_size if fit is not None else _title_series_font_size(layout)
            counter_font_size = max(24, int(round(series_font_size * STORY_SERIES_COUNTER_FONT_SCALE)))
            series_row_offsets.append(
                series_font_size
                + counter_font_size
                + int(round(layout.height * 0.018))
            )
        elif fit is not None:
            series_row_offsets.append(
                max(0, _title_series_main_row_offset(layout, fit) - int(round(layout.height * 0.012)))
                if holiday_font("series", language)
                else _title_series_main_row_offset(layout, fit)
            )
        else:
            series_row_offsets.append(_title_series_row_offset(layout, language))
    series_row_offset = max(series_row_offsets, default=0)
    main_title_margin_v = base_title_margin_v + (series_row_offset if has_series_cue else 0)
    subtitle_margin_v = main_title_margin_v
    subtitle_fits_by_key: dict[int, TitleOverlayFit | None] = {}
    main_title_heights: list[int] = []
    holiday_title_blocks: list[int] = []
    for cue in title_cues or []:
        kind = str(cue.get("kind") or "title")
        language = str(cue.get("language") or "ru").strip().lower() or "ru"
        if kind == "title":
            try:
                fit = _fit_title_overlay_ass_text(
                    str(cue.get("text") or ""),
                    layout=layout,
                    language=language,
                    side_margin_ratio=title_margin_ratio,
                    top_margin_ratio=title_top_margin_ratio,
                    has_series_cue=has_series_cue,
                )
            except Exception:
                logger.exception("post_production.title_overlay_fit_failed language=%s", language)
                fit = None
            if fit is not None:
                main_title_heights.append(fit.height)
                if holiday_font("dish", language):
                    holiday_title_blocks.append(int(round(fit.font_size * 0.90)))
        elif kind == "subtitle":
            try:
                subtitle_fits_by_key[id(cue)] = _fit_title_subtitle_ass_text(
                    str(cue.get("text") or ""),
                    layout=layout,
                    language=str(cue.get("language") or "ru"),
                    side_margin_ratio=title_margin_ratio,
                )
            except Exception:
                logger.exception("post_production.title_subtitle_fit_failed language=%s", language)
                subtitle_fits_by_key[id(cue)] = None
    if main_title_heights:
        subtitle_margin_v = main_title_margin_v + max(main_title_heights) + int(round(layout.height * TITLE_SUBTITLE_GAP_RATIO))
    holiday_day_y = main_title_margin_v + max(holiday_title_blocks, default=0) + int(round(layout.height * 0.008))
    holiday_day_size = max(24, int(round(layout.height * 0.070)))
    if holiday_title_blocks and any(cue.get("counter_text") for cue in title_cues if cue.get("kind") == "series"):
        subtitle_margin_v = max(subtitle_margin_v, holiday_day_y + holiday_day_size)
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
            if kind == "end_title":
                font_size = max(18, int(round(layout.height * float(cue.get("font_size_ratio") or 0.028))))
                anchor_ratio = min(max(float(cue.get("vertical_anchor_ratio") or 0.43), 0.05), 0.90)
                style_specs.append(
                    {
                        "style_name": style_name,
                        "font": str(cue.get("font_name") or "Arial Narrow"),
                        "font_size": font_size,
                        "primary_colour": _ass_colour_from_hex(cue.get("title_color"), default="&H006DA6D2"),
                        "outline_colour": "&H00000000",
                        "shadow_colour": "&H50000000",
                        "bold": 0,
                        "spacing": float(cue.get("tracking") or 3.0),
                        "outline": float(cue.get("outline") or 0.0),
                        "shadow": float(cue.get("shadow") or 1.0),
                        "margin_v": int(round(layout.height * anchor_ratio)),
                        "margin_l": max(0, int(layout.width * 0.05)),
                        "margin_r": max(0, int(layout.width * 0.05)),
                        "alignment": 8,
                    }
                )
            elif kind == "series":
                style_specs.append(
                    {
                        "style_name": style_name,
                        "font": _title_series_font_name(language),
                        "font_size": _title_series_font_size(layout),
                        "primary_colour": TITLE_SERIES_PRIMARY_COLOUR,
                        "outline_colour": TITLE_SERIES_OUTLINE_COLOUR,
                        "shadow_colour": TITLE_SERIES_SHADOW_COLOUR,
                        "bold": 0,
                        "spacing": 0,
                        "outline": TITLE_SERIES_OUTLINE,
                        "shadow": TITLE_SERIES_SHADOW,
                        "margin_v": base_title_margin_v,
                        "margin_l": series_margin_l,
                        "margin_r": title_margin_h,
                        "alignment": 7,
                    }
                )
            elif kind == "subtitle":
                style_specs.append(
                    {
                        "style_name": style_name,
                        "font": _title_subtitle_font_name(language),
                        "font_size": _title_subtitle_font_size(layout),
                        "primary_colour": TITLE_SUBTITLE_PRIMARY_COLOUR,
                        "outline_colour": TITLE_SUBTITLE_OUTLINE_COLOUR,
                        "shadow_colour": TITLE_SUBTITLE_SHADOW_COLOUR,
                        "bold": 0,
                        "spacing": 0,
                        "outline": TITLE_SUBTITLE_OUTLINE,
                        "shadow": TITLE_SUBTITLE_SHADOW,
                        "margin_v": subtitle_margin_v,
                        "margin_l": title_margin_h,
                        "margin_r": title_margin_h,
                        "alignment": 8,
                    }
                )
            else:
                style_specs.append(
                    {
                        "style_name": style_name,
                        "font": _title_overlay_font_name(language),
                        "font_size": _title_overlay_font_size(layout, ""),
                        "primary_colour": _ass_colour_from_hex(holiday_dish_color(title_style)) if holiday_font("dish", language) else title_primary_colour,
                        "outline_colour": TITLE_OVERLAY_OUTLINE_COLOUR,
                        "shadow_colour": TITLE_OVERLAY_SHADOW_COLOUR,
                        "bold": 0 if language == "en" or holiday_font("dish", language) else TITLE_OVERLAY_BOLD,
                        "spacing": TITLE_OVERLAY_SPACING,
                        "outline": _title_overlay_outline(language),
                        "shadow": _title_overlay_shadow(layout, language),
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
                    f"{spec.get('bold', 0)},0,0,0,100,100,{spec['spacing']},0,1,{spec['outline']},{spec['shadow']},{spec['alignment']},"
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
    from .editorial_titles import (
        HIERARCHY_KEY,
        STYLE_KEY,
        STYLES,
        ass_events,
        valid_hierarchy_parts,
    )
    editorial = str(title_style.get(STYLE_KEY) or "") in STYLES
    for cue in title_cues or []:
        language = str(cue.get("language") or "ru")
        hierarchy = (title_style.get(HIERARCHY_KEY) or {}).get(language)
        if editorial and str(cue.get("kind") or "title") == "series" and valid_hierarchy_parts(hierarchy):
            continue
        if editorial and str(cue.get("kind") or "title") == "title":
            lines.extend(ass_events(cue, title_style, layout.width, layout.height, SUBTITLE_STYLE_NAME))
            continue
        kind = str(cue.get("kind") or "title")
        if kind == "end_title":
            language = str(cue.get("language") or "ru")
            anchor_y = int(
                round(
                    layout.height
                    * min(max(float(cue.get("vertical_anchor_ratio") or 0.43), 0.05), 0.90)
                )
            )
            scale_x = max(40, min(120, int(cue.get("scale_x") or 100)))
            text = (
                f"{{\\an8\\pos({layout.width // 2},{anchor_y})\\q2\\fscx{scale_x}}}"
                + _escape_ass_no_wrap_spaces(str(cue.get("text") or ""))
            )
            layer = 3
        elif kind == "series":
            language = str(cue.get("language") or "ru")
            is_story_series = str(cue.get("layout_variant") or "") == "story"
            max_series_row_width = layout.width - series_margin_l - title_margin_h
            fit = _fit_title_series_ass_text(
                str(cue.get("text") or ""),
                "" if is_story_series else str(cue.get("counter_text") or ""),
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
            if holiday_font("series", language) and series_text:
                series_text = "{\\1c&H5AA63F&}" + series_text
            counter_text = _format_title_series_counter_ass_text(
                str(cue.get("counter_text") or ""),
                layout=layout,
                language=language,
                base_size=(
                    max(
                        24,
                        int(
                            round(
                                (fit.series_font_size if fit is not None else _title_series_font_size(layout))
                                * STORY_SERIES_COUNTER_FONT_SCALE
                            )
                        ),
                    )
                    if is_story_series
                    else fit.counter_font_size if fit is not None else None
                ),
                number_size=(
                    max(
                        24,
                        int(
                            round(
                                (fit.series_font_size if fit is not None else _title_series_font_size(layout))
                                * STORY_SERIES_COUNTER_FONT_SCALE
                            )
                        ),
                    )
                    if is_story_series
                    else fit.counter_number_font_size if fit is not None else None
                ),
                scale_x=100 if is_story_series else fit.counter_scale_x if fit is not None else 100,
            )
            if holiday_font("series", language):
                counter_text = _format_title_series_counter_ass_text(
                    str(cue.get("counter_text") or ""), layout=layout, language=language,
                    base_size=holiday_day_size, number_size=holiday_day_size,
                )
            style_name = str(cue.get("style") or _title_series_style_name(language))
            if series_text:
                lines.append(
                    "Dialogue: 2,{start},{end},{style},,0,0,0,,{text}".format(
                        start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                        end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                        style=style_name,
                        text=(
                            f"{{\\an8\\pos({layout.width // 2},{base_title_margin_v})}}" + series_text
                            if is_story_series
                            else series_text
                        ),
                    )
                )
            if counter_text:
                if holiday_font("series", language):
                    day_pos_x = title_margin_h - _title_day_ink_left_offset(
                        str(cue.get("counter_text") or ""), holiday_day_size,
                    )
                    day_pos_y = holiday_day_y - int(round(holiday_day_size * _title_series_day_y_shift_ratio(language)))
                    counter_text = "{\\an7}" + counter_text
                elif is_story_series:
                    series_font_size = fit.series_font_size if fit is not None else _title_series_font_size(layout)
                    day_font_size = max(
                        24,
                        int(round(series_font_size * STORY_SERIES_COUNTER_FONT_SCALE)),
                    )
                    day_pos_x = layout.width // 2
                    day_pos_y = int(
                        round(base_title_margin_v + series_font_size + (layout.height * 0.006))
                    )
                    counter_text = f"{{\\an8}}" + counter_text
                else:
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
        elif kind == "subtitle":
            language = str(cue.get("language") or "ru")
            text = _format_title_subtitle_ass_text(
                str(cue.get("text") or ""),
                layout=layout,
                language=language,
                side_margin_ratio=title_margin_ratio,
                fit=subtitle_fits_by_key.get(id(cue)),
            )
            if text:
                text = f"{{\\an8\\pos({layout.width // 2},{subtitle_margin_v})}}" + text
            layer = 1
        else:
            text = _format_title_overlay_ass_text(
                str(cue.get("text") or " "),
                layout=layout,
                language=str(cue.get("language") or "ru"),
                side_margin_ratio=title_margin_ratio,
                top_margin_ratio=title_top_margin_ratio,
                has_series_cue=has_series_cue,
            )
            if story_series_layout and text:
                text = f"{{\\an8\\pos({layout.width // 2},{main_title_margin_v})}}" + text
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
        if kind == "title" and holiday_font("dish", str(cue.get("language") or "ru")):
            # Same metrics, positioning and timing as the coloured base. This
            # companion font contains only the original transparent snow caps.
            lines.append(
                "Dialogue: 3,{start},{end},{style},,0,0,0,,{text}".format(
                    start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                    end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                    style=str(cue.get("style") or _title_overlay_style_name(str(cue.get("language") or "ru"))),
                    text="{\\fnCF SnowCap Snow\\1c&HFFFFFF&\\bord0\\shad0\\b0}" + text,
                )
            )
    for cue in cues:
        fixed_block_text = _story_fixed_block_ass_text(cue, layout, title_style)
        if fixed_block_text:
            lines.append(
                "Dialogue: 0,{start},{end},{style},,0,0,0,,{text}".format(
                    start=_ass_timestamp(float(cue.get("start_sec") or 0.0)),
                    end=_ass_timestamp(float(cue.get("end_sec") or 0.0)),
                    style=SUBTITLE_STYLE_NAME,
                    text=fixed_block_text,
                )
            )
            continue
        cue_lines = str(cue.get("text") or " ").splitlines() or [" "]
        for index, line in enumerate(cue_lines):
            anchor_y = _subtitle_line_anchor_y(layout, line_count=len(cue_lines), index=index)
            from .split_panel_typography import caption_anchor
            panel_anchor = caption_anchor(title_style)
            if panel_anchor is not None:
                anchor_y += round(layout.height * panel_anchor) - _subtitle_line_anchor_y(layout, line_count=1, index=0)
            colour_override = f"\\c{STORY_WORD_SYNC_SUBTITLE_COLOUR}" if cue.get("word_synced") else ""
            text = f"{{\\an1\\pos({layout.margin_l},{anchor_y}){colour_override}}}" + _escape_ass_text(line)
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

    for cue in accent_cues or []:
        start, end = float(cue["start_sec"]), float(cue["end_sec"])
        accent_lines = str(cue["text"]).splitlines()
        text = r"\N".join(_escape_ass_text(line) for line in accent_lines)
        size = round(min(layout.font_size * 1.28, layout.height * 0.062))
        font_path = _story_subtitle_font_path()
        if font_path:
            font = ImageFont.truetype(str(font_path), size=size)
            line_width = max(font.getlength(line) for line in accent_lines)
            size = max(1, min(size, round(size * layout.width * 0.80 / max(1.0, line_width))))
        x = round(layout.width * float(cue.get("anchor_x_ratio", 0.5)))
        y = round(layout.height * float(cue.get("anchor_ratio", 0.28)))
        accent_font, accent_colour, clock_tags = STORY_WORD_SYNC_FONT_NAME, "&HAAE8FF&", ""
        if cue.get("clock_display"):
            size = max(1, round(layout.height * 0.016))
            accent_font, accent_colour, clock_tags = "Courier New", "&H526AFF&", r"\bord0\shad0"
        shift = max(2, round(layout.width * 0.005))
        offsets = ((-1, 0), (1, 1), (-1, -1), (1, 0), (0, 0)) if cue.get("shake", True) else ((0, 0),)
        for index, (dx, dy) in enumerate(offsets):
            frame_start = start + index * 0.055
            frame_end = end if index == len(offsets) - 1 else min(end, frame_start + 0.055)
            if frame_end <= frame_start:
                break
            at = f"{{\\an8\\pos({x + dx * shift},{y + dy * shift})\\fn{accent_font}\\fs{size}\\b1\\c{accent_colour}{clock_tags}}}"
            lines.append(
                f"Dialogue: 4,{_ass_timestamp(frame_start)},{_ass_timestamp(frame_end)},"
                f"{SUBTITLE_STYLE_NAME},,0,0,0,,{at}{text}"
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


def _alignment_word_rows(alignment: dict[str, Any] | None) -> list[tuple[str, float, float]]:
    """Collapse ElevenLabs character alignment into exact spoken-word timings."""
    if not isinstance(alignment, dict):
        return []
    characters = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not isinstance(characters, list) or not isinstance(starts, list) or not isinstance(ends, list):
        return []
    count = min(len(characters), len(starts), len(ends))
    words: list[tuple[str, float, float]] = []
    token: list[str] = []
    token_start = 0.0
    token_end = 0.0
    for index in range(count):
        character = str(characters[index])
        try:
            character_start = float(starts[index])
            character_end = float(ends[index])
        except (TypeError, ValueError):
            continue
        if character.isspace():
            if token:
                words.append(("".join(token), token_start, token_end))
                token = []
            continue
        if not token:
            token_start = character_start
        token.append(character)
        token_end = character_end
    if token:
        words.append(("".join(token), token_start, token_end))
    return [(word, start, end) for word, start, end in words if word and end > start]


def _story_caption_word_blocks(
    words: list[tuple[str, float, float]],
) -> list[list[tuple[str, float, float]]]:
    """Create short, readable phrase blocks without exceeding four words."""
    blocks: list[list[tuple[str, float, float]]] = []
    current: list[tuple[str, float, float]] = []
    for row in words:
        current.append(row)
        token = str(row[0] or "").rstrip()
        if len(current) >= STORY_WORD_SYNC_MAX_WORDS or token.endswith((".", "!", "?", "…")):
            # Keep determiners with their noun: «каждый год», "every year".
            if len(current) >= STORY_WORD_SYNC_MAX_WORDS and token.casefold() in {
                "каждый", "каждая", "каждое", "каждые", "every", "each",
            }:
                blocks.append(current[:-1])
                current = [row]
                continue
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def build_word_synced_subtitle_cues(
    word_timings: list[dict[str, Any]] | list[tuple[str, float, float]],
    *,
    window_end_sec: float | None = None,
    timing_source: str = "audio_transcription",
) -> list[dict[str, Any]]:
    """Build the same progressive fixed-block captions used by Story mode."""
    words: list[tuple[str, float, float]] = []
    for item in word_timings or []:
        if isinstance(item, dict):
            word = " ".join(str(item.get("word") or item.get("text") or "").split())
            raw_start = item.get("start_sec")
            raw_end = item.get("end_sec")
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            word = " ".join(str(item[0] or "").split())
            raw_start = item[1]
            raw_end = item[2]
        else:
            continue
        try:
            start_sec = max(0.0, float(raw_start or 0.0))
            end_sec = float(raw_end or start_sec)
        except (TypeError, ValueError):
            continue
        if word and " " not in word and end_sec > start_sec:
            words.append((word, start_sec, end_sec))
    if not words:
        return []
    words.sort(key=lambda row: (row[1], row[2]))
    hard_end = max(float(window_end_sec or 0.0), words[-1][2])
    cues: list[dict[str, Any]] = []
    blocks = _story_caption_word_blocks(words)
    for block_index, block in enumerate(blocks):
        block_tokens = [word for word, _start, _end in block]
        next_block_start = blocks[block_index + 1][0][1] if block_index + 1 < len(blocks) else hard_end
        for word_index, (_word, word_start, word_end) in enumerate(block):
            next_start = block[word_index + 1][1] if word_index + 1 < len(block) else next_block_start
            cue_end = next_start if next_start > word_start else word_end
            if cue_end <= word_start:
                continue
            cues.append(
                {
                    "text": " ".join(block_tokens[: word_index + 1]),
                    "start_sec": word_start,
                    "end_sec": cue_end,
                    "word_synced": True,
                    "preserve_case": True,
                    "fixed_block_words": block_tokens,
                    "visible_word_count": word_index + 1,
                    "story_style_version": STORY_WORD_SYNC_STYLE_VERSION,
                    "story_timing_source": timing_source,
                }
            )
    return cues


def _estimated_word_timings_for_cue(text: str, *, start_sec: float, end_sec: float) -> list[dict[str, Any]]:
    """Backwards-compatible fallback for clips saved before audio word timestamps."""
    tokens = str(text or "").split()
    duration = max(0.0, float(end_sec) - float(start_sec))
    if not tokens or duration <= 0:
        return []
    weights = [max(1, len(re.sub(r"[^\w]+", "", token, flags=re.UNICODE))) for token in tokens]
    total_weight = sum(weights)
    cursor = float(start_sec)
    rows: list[dict[str, Any]] = []
    for index, (token, weight) in enumerate(zip(tokens, weights, strict=True)):
        next_cursor = float(end_sec) if index == len(tokens) - 1 else cursor + (duration * weight / total_weight)
        rows.append({"word": token, "start_sec": cursor, "end_sec": next_cursor})
        cursor = next_cursor
    return rows


def _story_scene_intervals(
    scenes: list[dict[str, Any]],
    *,
    language: str,
    narration_duration_sec: float,
) -> list[tuple[float, float]]:
    """Map Story clips onto the approved narration duration without cutting words."""
    raw_durations: list[float] = []
    for scene in scenes:
        duration_map = scene.get("timeline_duration_by_lang")
        raw_duration: Any = duration_map.get(language) if isinstance(duration_map, dict) else None
        if raw_duration in (None, "", 0, 0.0):
            raw_duration = scene.get("timeline_duration_sec")
        if raw_duration in (None, "", 0, 0.0):
            try:
                raw_duration = float(scene.get("end_sec") or 0.0) - float(scene.get("start_sec") or 0.0)
            except (TypeError, ValueError):
                raw_duration = 0.0
        if not raw_duration:
            raw_duration = scene.get("duration_sec")
        try:
            raw_durations.append(max(0.0, float(raw_duration or 0.0)))
        except (TypeError, ValueError):
            raw_durations.append(0.0)
    duration = max(0.0, float(narration_duration_sec or 0.0))
    raw_total = sum(raw_durations)
    if duration <= 0 or raw_total <= 0:
        return []
    scale = duration / raw_total
    intervals: list[tuple[float, float]] = []
    cursor = 0.0
    for index, raw_duration in enumerate(raw_durations):
        start = cursor
        cursor = duration if index == len(raw_durations) - 1 else cursor + (raw_duration * scale)
        intervals.append((start, cursor))
    return intervals


def apply_story_word_synced_subtitles(
    scenes: list[dict[str, Any]],
    *,
    project_slug: str | None,
    voiceover_by_lang: dict[str, str] | None,
    alignment_by_lang: dict[str, dict[str, Any]] | None,
    duration_by_lang: dict[str, float] | None,
) -> list[dict[str, Any]]:
    """Attach progressive, exact word-timed captions to every Story project.

    Each caption grows by one spoken word.  Timing and spelling come from the
    approved ElevenLabs alignment, so final rendering does not have to infer
    timings from scene length or restore language text from a video candidate.
    """
    slug = str(project_slug or "").strip().lower()
    if not scenes:
        return scenes
    alignments = alignment_by_lang if isinstance(alignment_by_lang, dict) else {}
    durations = duration_by_lang if isinstance(duration_by_lang, dict) else {}
    voiceovers = voiceover_by_lang if isinstance(voiceover_by_lang, dict) else {}
    languages = list(dict.fromkeys([*alignments, *durations, *voiceovers]))
    for language in languages:
        alignment = alignments.get(language)
        lang = str(language or "").strip().lower()
        if not lang:
            continue
        try:
            duration = float(durations.get(lang) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        words = _alignment_word_rows(alignment)
        if duration <= 0 and words:
            duration = float(words[-1][2])
        intervals = _story_scene_intervals(scenes, language=lang, narration_duration_sec=duration)
        from .story_alignment import valid_alignment
        if (str(voiceovers.get(lang) or "").strip() or alignment) and not valid_alignment(alignment):
            raise RuntimeError(
                f"Для субтитров истории ({lang}) отсутствуют точные тайминги озвучки. "
                "Восстановите alignment ElevenLabs для утверждённого аудио; "
                "приблизительные тайминги по длине текста больше не используются."
            )
        if not words:
            continue
        if len(intervals) != len(scenes):
            raise RuntimeError(f"Не удалось привязать точные субтитры истории ({lang}) к длительности сцен.")
        global_cues = build_word_synced_subtitle_cues(
            words, window_end_sec=duration, timing_source="elevenlabs_alignment",
        )
        for index, scene in enumerate(scenes):
            interval_start, interval_end = intervals[index]
            tokens = [word for word, start, end in words if start < interval_end and end > interval_start]
            speech_map = dict(scene.get("speech_by_lang") or {}) if isinstance(scene.get("speech_by_lang"), dict) else {}
            speech_map[lang] = " ".join(tokens).strip()
            scene["speech_by_lang"] = speech_map
            duration_map = dict(scene.get("timeline_duration_by_lang") or {})
            duration_map[lang] = max(0.0, interval_end - interval_start)
            scene["timeline_duration_by_lang"] = duration_map
            narration_starts = dict(scene.get("narration_subtitle_start_by_lang") or {})
            narration_starts[lang] = interval_start
            scene["narration_subtitle_start_by_lang"] = narration_starts
            # Keep a spoken word visible on both sides of a visual scene cut.
            cues = [
                {**cue,
                 "start_sec": max(interval_start, cue["start_sec"]) - interval_start,
                 "end_sec": min(interval_end, cue["end_sec"]) - interval_start}
                for cue in global_cues
                if cue["start_sec"] < interval_end and cue["end_sec"] > interval_start
            ]
            cues_map = (
                dict(scene.get("speech_cues_by_lang") or {})
                if isinstance(scene.get("speech_cues_by_lang"), dict)
                else {}
            )
            cues_map[lang] = cues
            scene["speech_cues_by_lang"] = cues_map
            synced_languages = [str(item).strip().lower() for item in scene.get("word_synced_subtitle_languages") or []]
            if lang not in synced_languages:
                synced_languages.append(lang)
            scene["word_synced_subtitle_languages"] = synced_languages
        logger.info(
            "post_production.story_word_subtitles project=%s language=%s words=%s scenes=%s source_chars=%s",
            slug,
            lang,
            len(words),
            len(scenes),
            len(str(voiceovers.get(lang) or "")),
        )
    return scenes


def _expand_dense_subtitle_entry(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one long subtitle entry into page-sized subentries when its time window is too dense."""
    text = _normalize_subtitle_text(str(item.get("text") or ""))
    start_sec = float(item.get("start_sec") or 0.0)
    end_sec = float(item.get("end_sec") or start_sec)
    fixed_block_words = item.get("fixed_block_words")
    if isinstance(fixed_block_words, list) and fixed_block_words:
        return [dict(item, text=text, start_sec=start_sec, end_sec=end_sec)]
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
    fixed_block_words = item.get("fixed_block_words")
    if isinstance(fixed_block_words, list) and fixed_block_words and end_sec > start_sec:
        return [
            {
                "start_sec": start_sec,
                "end_sec": end_sec,
                "text": str(item.get("text") or ""),
                "word_synced": True,
                "preserve_case": True,
                "fixed_block_words": list(fixed_block_words),
                "visible_word_count": int(item.get("visible_word_count") or 0),
                "story_style_version": str(item.get("story_style_version") or STORY_WORD_SYNC_STYLE_VERSION),
            }
        ]
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
        cue = {
            "start_sec": cue_start,
            "end_sec": cue_end,
            "text": "\n".join(
                _subtitle_display_text(line, preserve_case=bool(item.get("preserve_case")))
                for line in lines
            ),
        }
        if item.get("word_synced"):
            cue["word_synced"] = True
        if item.get("preserve_case"):
            cue["preserve_case"] = True
        cues.append(cue)
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
    narration_offset_sec: float = 0.0,
) -> list[dict[str, Any]]:
    if bool(scene.get("guest_mode")):
        return []
    cues_map = scene.get("speech_cues_by_lang")
    raw_cues = cues_map.get(language) if isinstance(cues_map, dict) else None
    entries: list[dict[str, Any]] = []
    safe_speed = max(0.01, float(speed_coef or 1.0))
    safe_scale = max(0.01, float(timing_scale or 1.0))
    safe_duration = max(0.0, float(fallback_duration or 0.0))
    narration_starts = scene.get("narration_subtitle_start_by_lang")
    narration_start = narration_starts.get(language) if isinstance(narration_starts, dict) else None
    if narration_start is not None:
        # The narration track is continuous and is not retimed with visual clips.
        safe_speed = safe_scale = 1.0
        safe_duration = 0.0
        start_offset = float(narration_start) + float(narration_offset_sec)
    if isinstance(raw_cues, list):
        normalized_cues: list[dict[str, Any]] = []
        for cue in raw_cues:
            if not isinstance(cue, dict):
                continue
            if cue.get("word_synced"):
                normalized_cues.append(cue)
                continue
            try:
                cue_start = float(cue.get("start_sec") or 0.0)
                cue_end = float(cue.get("end_sec") or cue_start)
            except (TypeError, ValueError):
                continue
            normalized_cues.extend(
                build_word_synced_subtitle_cues(
                    _estimated_word_timings_for_cue(
                        str(cue.get("text") or ""),
                        start_sec=cue_start,
                        end_sec=cue_end,
                    ),
                    window_end_sec=cue_end,
                    timing_source="estimated_legacy_scene_cue",
                )
            )
        for cue in normalized_cues:
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
            entry = {
                "scene": int(scene.get("scene") or 0) or None,
                "language": language,
                "text": text,
                "start_sec": start_offset + start_sec,
                "end_sec": start_offset + end_sec,
            }
            if cue.get("word_synced"):
                entry["word_synced"] = True
            if cue.get("preserve_case"):
                entry["preserve_case"] = True
            if isinstance(cue.get("fixed_block_words"), list):
                entry["fixed_block_words"] = list(cue["fixed_block_words"])
                entry["visible_word_count"] = int(cue.get("visible_word_count") or 0)
                entry["story_style_version"] = str(
                    cue.get("story_style_version") or STORY_WORD_SYNC_STYLE_VERSION
                )
                if cue.get("story_timing_source"):
                    entry["story_timing_source"] = str(cue["story_timing_source"])
            entries.append(entry)
    if entries:
        expanded: list[dict[str, Any]] = []
        for entry in entries:
            expanded.extend(_expand_dense_subtitle_entry(entry))
        return expanded
    synced_languages = {
        str(item).strip().lower()
        for item in scene.get("word_synced_subtitle_languages") or []
        if str(item).strip()
    }
    if str(language or "").strip().lower() in synced_languages:
        return []
    raw_text = _subtitle_source_text_for_scene(scene, language)
    pause_entries = _subtitle_entries_from_pause_markers(
        raw_text,
        scene=int(scene.get("scene") or 0) or None,
        language=language,
        start_sec=start_offset,
        end_sec=start_offset + fallback_duration,
    )
    if pause_entries:
        word_synced: list[dict[str, Any]] = []
        for entry in pause_entries:
            cues = build_word_synced_subtitle_cues(
                _estimated_word_timings_for_cue(
                    str(entry.get("text") or ""),
                    start_sec=float(entry.get("start_sec") or start_offset),
                    end_sec=float(entry.get("end_sec") or start_offset),
                ),
                window_end_sec=float(entry.get("end_sec") or start_offset),
                timing_source="estimated_legacy_pause_text",
            )
            for cue in cues:
                word_synced.append(
                    {
                        **cue,
                        "scene": entry.get("scene"),
                        "language": language,
                    }
                )
        return word_synced
    text = _normalize_subtitle_text(raw_text)
    if not text and str(language or "").strip().lower() != "ru":
        scene_no = int(scene.get("scene") or 0) or None
        raise RuntimeError(
            f"Missing language-specific subtitle text for scene {scene_no or '?'} ({language})."
        )
    cues = build_word_synced_subtitle_cues(
        _estimated_word_timings_for_cue(
            text,
            start_sec=start_offset,
            end_sec=start_offset + fallback_duration,
        ),
        window_end_sec=start_offset + fallback_duration,
        timing_source="estimated_legacy_scene_text",
    )
    return [
        {
            **cue,
            "scene": int(scene.get("scene") or 0) or None,
            "language": language,
        }
        for cue in cues
    ]


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


def _snapshot_text_for_language(value: Any, language: str) -> str:
    lang = str(language or "").strip().lower()
    if isinstance(value, str):
        return _speech_text_from_prompt(value) or value
    if isinstance(value, dict):
        item_text = " ".join(str(value.get(key) or "") for key in ("text", "phrase", "line")).strip()
        if item_text:
            return item_text
        direct = value.get(lang)
        if isinstance(direct, str):
            return _speech_text_from_prompt(direct) or direct
        if isinstance(direct, list):
            return " ".join(_snapshot_text_for_language(item, lang) for item in direct)
        if isinstance(direct, dict):
            return " ".join(str(direct.get(key) or "") for key in ("text", "phrase", "line"))
        return ""
    if isinstance(value, list):
        return " ".join(_snapshot_text_for_language(item, lang) for item in value)
    return ""


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
    if lang in (scene.get("narration_subtitle_start_by_lang") or {}):
        # A visual candidate may contain an old, repeated beat transcript.
        # It must never overwrite captions aligned to the approved narration.
        return scene
    out = dict(scene)
    for key in ("speech_by_lang", "speech_cues_by_lang", "speech_plan_by_lang", "video_prompt_by_lang"):
        snapshot = selected.get(key)
        if not isinstance(snapshot, dict) or not snapshot.get(lang):
            continue
        if lang == "ru":
            raw_text = _snapshot_text_for_language(snapshot, lang)
            if raw_text and not CYRILLIC_CHAR_RE.search(raw_text):
                continue
        merged = dict(out.get(key) or {}) if isinstance(out.get(key), dict) else {}
        merged[lang] = snapshot.get(lang)
        out[key] = merged
    return out


def _speech_text_from_prompt(prompt: Any) -> str:
    raw = str(prompt or "")
    match = re.search(r"(?is)\bCHARACTERS\s+SPEAKS:\s*", raw)
    direct = ""
    if match:
        tail = raw[match.end() :]
        terminator = PROMPT_SPEECH_TERMINATOR_RE.search(tail)
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
            terminator = PROMPT_SPEECH_TERMINATOR_RE.search(tail)
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
        terminator = PROMPT_SPEECH_TERMINATOR_RE.search(text)
        if terminator is not None:
            text = text[: terminator.start()].strip()
        if text:
            parts.append(text)
    return " ".join(" ".join(parts).split())


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
    return value or default_scene_video_speed_coef(scene)


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


def _frame_aligned_duration(duration: float, *, fps: int = 30) -> float:
    safe_fps = max(1, int(fps or 30))
    safe_duration = max(0.0, float(duration or 0.0))
    if safe_duration <= 0:
        return 0.0
    frames = max(1, int(round(safe_duration * safe_fps)))
    return frames / float(safe_fps)


def _proportional_frame_aligned_durations(
    durations: list[float],
    target_total_sec: float,
    *,
    fps: int = 30,
) -> list[float]:
    """Fit clip durations to one narration track without cumulative drift."""
    safe_fps = max(1, int(fps or 30))
    safe = [max(0.0, float(value or 0.0)) for value in durations]
    source_total = sum(safe)
    target_frames = max(len(safe), int(round(max(0.0, float(target_total_sec or 0.0)) * safe_fps)))
    if not safe or source_total <= 0 or target_frames <= 0:
        return safe
    raw_frames = [(value / source_total) * target_frames for value in safe]
    frames = [max(1, int(value)) for value in raw_frames]
    while sum(frames) < target_frames:
        index = max(range(len(frames)), key=lambda i: raw_frames[i] - frames[i])
        frames[index] += 1
    while sum(frames) > target_frames:
        candidates = [i for i, value in enumerate(frames) if value > 1]
        if not candidates:
            break
        index = max(candidates, key=lambda i: frames[i] - raw_frames[i])
        frames[index] -= 1
    return [value / float(safe_fps) for value in frames]


def _normalize_segment(
    *,
    ffmpeg: str,
    src_path: Path,
    dest_path: Path,
    speed_coef: float,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
    target_duration_sec: float | None = None,
    fit_full_clip: bool = False,
) -> None:
    source_duration = float(_media_duration(src_path) or 0.0)
    requested_duration = max(0.0, float(target_duration_sec or 0.0))
    if fit_full_clip and requested_duration > 0 and _has_audio_stream(src_path):
        # A longer audio stream must not make us underestimate the video slowdown.
        # Remuxing just the video is lossless and also works without ffprobe.
        with tempfile.TemporaryDirectory(prefix="cf_video_duration_") as tmp:
            video_only = Path(tmp) / "video.mp4"
            _run_ffmpeg(
                [ffmpeg, "-y", "-i", str(src_path), "-map", "0:v:0",
                 "-c:v", "copy", "-an", str(video_only)],
                timeout=FFMPEG_SHORT_TIMEOUT_SEC, stage="probe_video_duration",
            )
            source_duration = float(_media_duration(video_only) or source_duration)
    if fit_full_clip and requested_duration > 0 and source_duration > 0:
        # The following shot starts at this take's last frame in both languages.
        speed_coef = source_duration / requested_duration
    normalized_duration = (
        requested_duration
        if requested_duration > 0
        else source_duration / max(0.01, float(speed_coef or 1.0)) if source_duration > 0 else 0.0
    )
    aligned_duration = _frame_aligned_duration(normalized_duration, fps=30)
    duration_arg = f"{aligned_duration:.6f}" if aligned_duration > 0 else ""
    spatial_filter = (
        f"scale={int(target_width)}:{int(target_height)}:force_original_aspect_ratio=increase,"
        f"crop={int(target_width)}:{int(target_height)}"
    )
    vf = f"{spatial_filter},setpts=PTS/{speed_coef:.6f},fps=30,tpad=stop_mode=clone:stop_duration=0.100000"
    if duration_arg:
        vf = f"{vf},trim=duration={duration_arg}"
    vf = f"{vf},settb=AVTB,setpts=N/(30*TB)"
    if fit_full_clip and requested_duration > 0 and source_duration > 0:
        # FPS conversion may discard the last input frame when speeding up. Pin the
        # final decoded pose explicitly because it seeds the following Kling take.
        body_frames = max(0, round(aligned_duration * 30) - 1)
        tail = f"trim=start={max(0, source_duration - 1):.6f},reverse,trim=end_frame=1,{spatial_filter},settb=AVTB,setpts=0"
        if body_frames:
            vf = (
                f"split[body][tail];[body]{vf},trim=end_frame={body_frames}[fit];"
                f"[tail]{tail}[last];[fit][last]concat=n=2:v=1:a=0,settb=AVTB,setpts=N/(30*TB)"
            )
        else:
            vf = tail
    af = _atempo_filter(speed_coef)
    has_audio = _has_audio_stream(src_path)
    if duration_arg:
        if has_audio:
            filter_complex = (
                f"[0:v]{vf}[v];"
                f"[0:a]{af},aresample=async=1:first_pts=0,"
                f"apad=whole_dur={duration_arg},atrim=duration={duration_arg},asetpts=N/SR/TB[a]"
            )
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(src_path),
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "[a]",
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
                "-ar",
                "48000",
                "-ac",
                "2",
                "-video_track_timescale",
                "30000",
                "-movflags",
                "+faststart",
                str(dest_path),
            ]
            _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="normalize_segment")
            return
        filter_complex = f"[0:v]{vf}[v];[1:a]atrim=duration={duration_arg},asetpts=N/SR/TB[a]"
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(src_path),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
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
            "-ar",
            "48000",
            "-ac",
            "2",
            "-video_track_timescale",
            "30000",
            "-movflags",
            "+faststart",
            str(dest_path),
        ]
        _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="normalize_segment")
        return
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
        "-ar",
        "48000",
        "-ac",
        "2",
        "-video_track_timescale",
        "30000",
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


def _replace_master_audio_with_narration(
    *,
    ffmpeg: str,
    video_path: Path,
    narration_path: Path,
    dest_path: Path,
    narration_volume: float = 1.0,
) -> None:
    """Replace generated clip audio with one approved, seamless narration track."""
    duration = max(0.0, float(_media_duration(video_path) or 0.0))
    if duration <= 0:
        raise RuntimeError("Cannot determine story master duration before narration mux.")
    duration_arg = f"{duration:.6f}"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(narration_path),
        "-filter_complex",
        f"[1:a]volume={max(0.0, float(narration_volume)):.4f},alimiter=limit=0.97,"
        f"aresample=48000,apad=whole_dur={duration_arg},"
        f"atrim=duration={duration_arg},asetpts=N/SR/TB[a]",
        "-map",
        "0:v:0",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(dest_path),
    ]
    _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="replace_master_audio_with_narration")


def _replace_master_audio_with_intro_and_narration(
    *,
    ffmpeg: str,
    video_path: Path,
    intro_video_path: Path,
    narration_path: Path,
    dest_path: Path,
    narration_volume: float = 1.0,
) -> None:
    """Keep the approved intro dialogue, then start the ordinary Story narration."""
    duration = max(0.0, float(_media_duration(video_path) or 0.0))
    intro_duration = max(0.0, float(_media_duration(intro_video_path) or 0.0))
    if duration <= 0 or intro_duration <= 0:
        raise RuntimeError("Cannot determine Discipline intro/master duration before narration mux.")
    duration_arg = f"{duration:.6f}"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(intro_video_path),
        "-i",
        str(narration_path),
        "-filter_complex",
        "[1:a]aresample=48000,asetpts=N/SR/TB[intro];"
        f"[2:a]volume={max(0.0, float(narration_volume)):.4f},alimiter=limit=0.97,"
        "aresample=48000,asetpts=N/SR/TB[story];"
        f"[intro][story]concat=n=2:v=0:a=1,apad=whole_dur={duration_arg},"
        f"atrim=duration={duration_arg},asetpts=N/SR/TB[a]",
        "-map",
        "0:v:0",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(dest_path),
    ]
    _run_ffmpeg(cmd, timeout=FFMPEG_RENDER_TIMEOUT_SEC, stage="replace_master_audio_with_intro_and_narration")


def _upscale_master_to_4k(
    *,
    ffmpeg: str,
    src_path: Path,
    dest_path: Path,
    target_width: int = UPSCALED_4K_WIDTH,
    target_height: int = UPSCALED_4K_HEIGHT,
) -> None:
    vf = (
        f"scale={int(target_width)}:{int(target_height)}:flags=lanczos,"
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
    resolved_fonts_dir = fonts_dir
    if resolved_fonts_dir is None:
        try:
            ass_content = subtitles_path.read_text(encoding="utf-8", errors="ignore")
            uses_editorial_font = "\\fnCF " in ass_content
            uses_story_font = STORY_WORD_SYNC_FONT_NAME in ass_content and not uses_editorial_font
        except OSError:
            uses_story_font = False
            uses_editorial_font = False
        if uses_editorial_font:
            from .editorial_titles import FONT_DIR
            resolved_fonts_dir = FONT_DIR
        else:
            story_font_path = _story_subtitle_font_path() if uses_story_font else None
            resolved_fonts_dir = story_font_path.parent if story_font_path is not None else subtitle_fonts_dir()
    parts = [f"filename={_ffmpeg_filter_quote(subtitles_path)}"]
    if resolved_fonts_dir.exists():
        parts.append(f"fontsdir={_ffmpeg_filter_quote(resolved_fonts_dir)}")
    else:
        logger.warning("post_production.subtitle_fonts_dir_missing path=%s", resolved_fonts_dir)
    return "subtitles=" + ":".join(parts)


def render_title_overlay_preview(
    background: Image.Image,
    *,
    language: str,
    title: str,
    title_style: dict[str, Any] | None = None,
    series_title: str = "",
    series_counter: str = "",
    subtitle: str = "",
) -> bytes:
    """Render a still with the exact ASS styles and layout used by final video."""
    language = str(language or "ru").strip().lower() or "ru"
    cues = [
        _build_title_overlay_cue({language: title}, language=language, duration_sec=1),
        _build_title_series_overlay_cue(
            {language: series_title}, {language: series_counter}, language=language, duration_sec=1,
        ),
        _build_title_subtitle_overlay_cue({language: subtitle}, language=language, duration_sec=1),
    ]
    with tempfile.TemporaryDirectory(prefix="title_preview_") as tmp:
        directory = Path(tmp)
        source = directory / "background.png"
        ass_path = directory / "title.ass"
        destination = directory / "preview.png"
        background.convert("RGB").save(source)
        _write_burned_subtitle_ass(
            ass_path, [], title_cues=[cue for cue in cues if cue is not None],
            title_style=title_style, video_width=background.width, video_height=background.height,
        )
        from .title_depth import depth_filter_graph, has_depth, split_ass, still_foreground_mask

        ffmpeg = _resolve_ffmpeg()
        inputs = [ffmpeg, "-y", "-i", str(source)]
        if has_depth(ass_path):
            base, back, front, _ = split_ass(ass_path, directory)
            mask_path = directory / "foreground.png"
            still_foreground_mask(background).save(mask_path)
            graph = depth_filter_graph(base, back, background.width, background.height)
            inputs += ["-i", str(mask_path), "-filter_complex",
                       graph + f";[video]{_subtitle_filter(front)}[preview]", "-map", "[preview]"]
        else:
            inputs += ["-vf", _subtitle_filter(ass_path)]
        _run_ffmpeg(inputs + ["-frames:v", "1", str(destination)],
                    timeout=FFMPEG_SHORT_TIMEOUT_SEC, stage="title_preview")
        return destination.read_bytes()


def _render_subtitled_music_master(
    *,
    ffmpeg: str,
    clean_path: Path,
    subtitles_path: Path,
    music_path: str | None,
    music_volume: float,
    dest_path: Path,
) -> bool:
    from .title_depth import has_depth, render_background
    if has_depth(subtitles_path):
        with tempfile.TemporaryDirectory(prefix="cf_title_depth_") as tmp:
            depth_video, front_ass = render_background(ffmpeg, clean_path, subtitles_path, Path(tmp))
            return _render_subtitled_music_master(
                ffmpeg=ffmpeg, clean_path=depth_video, subtitles_path=front_ass,
                music_path=music_path, music_volume=music_volume, dest_path=dest_path,
            )
    subtitle_filter = _subtitle_filter(subtitles_path)
    effective_music_volume = _apply_db_attenuation(music_volume, BACKGROUND_MUSIC_ATTENUATION_DB)
    music_filter = holiday_music_filter(music_path)
    has_source_audio = _has_audio_stream(clean_path)
    if music_path:
        if has_source_audio:
            source_duration = max(0.0, float(_media_duration(clean_path) or 0.0))
            voice_duration_filter = ""
            if source_duration > 0:
                duration_arg = f"{source_duration:.3f}"
                voice_duration_filter = (
                    f",apad=whole_dur={duration_arg},atrim=duration={duration_arg},asetpts=N/SR/TB"
                )
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
                f"[0:a]volume={SOURCE_AUDIO_VOLUME:.4f}{voice_duration_filter}[voice];"
                f"[1:a]{music_filter}volume={effective_music_volume:.4f}[music];"
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
            f"[0:v]{subtitle_filter}[v];[1:a]{music_filter}volume={effective_music_volume:.4f}[a]",
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


def production_languages_for_scenes(
    scenes: list[dict[str, Any]],
    target_languages: list[str] | tuple[str, ...] | set[str] | None = None,
    *,
    require_ready: bool = False,
) -> list[str]:
    tracks = (
        dict.fromkeys(str(lang).strip().lower() for lang in target_languages if str(lang).strip())
        if target_languages is not None
        else dict.fromkeys(lang for scene in scenes for lang in _production_languages(scene))
    )
    ready = []
    missing = []
    for lang in tracks:
        orders = [
            str(index) for index, scene in enumerate(scenes, start=1)
            if resolve_latest_approved_scene_video_for_final_build(scene, lang) is None
        ]
        if orders:
            missing.append(f"{lang.upper()} ({', '.join(orders)})")
        elif scenes:
            ready.append(lang)
    if require_ready and not ready:
        if missing:
            raise RuntimeError("Missing approved scene videos for " + ", ".join(missing) + ".")
        raise RuntimeError("No scenes or languages were selected for final rendering.")
    return ready


def _prepare_inner_boss_edit(
    tmp_path: Path,
    master: Path,
    scene_timeline: list[tuple[dict[str, Any], float]],
    subtitle_entries: list[dict[str, Any]],
    language: str,
    *,
    intro_duration: float,
    artifact_bundle_key: str,
) -> tuple[str, list[dict[str, Any]]]:
    duration = _media_duration(master) or (intro_duration + sum(length for _, length in scene_timeline))
    accent_scenes = inner_boss_mix.accent_scene_numbers([scene for scene, _ in scene_timeline])
    accents, silence = inner_boss_mix.anchors(subtitle_entries, language, duration, accent_scenes)
    stem = tmp_path / f"inner_boss_{language}.wav"
    inner_boss_mix.score(
        stem, scene_timeline, duration=duration, intro_duration=intro_duration,
        silence=silence,
    )
    fingerprint = hashlib.sha256(stem.read_bytes()).hexdigest()[:16]
    asset = _persist_render_artifact(
        stem, bundle_key=artifact_bundle_key, filename=f"inner_boss_{language}_{fingerprint}.wav",
    )
    return asset, accents


def build_post_production(
    scenes: list[dict[str, Any]],
    *,
    project_slug: str | None = None,
    overlay_title_by_lang: dict[str, Any] | None = None,
    overlay_subtitle_by_lang: dict[str, Any] | None = None,
    overlay_title_style: dict[str, Any] | None = None,
    overlay_series_title_by_lang: dict[str, Any] | None = None,
    overlay_series_counter_by_lang: dict[str, Any] | None = None,
    target_languages: list[str] | tuple[str, ...] | set[str] | None = None,
    target_variants: list[str] | tuple[str, ...] | set[str] | None = None,
    progress_callback: Any | None = None,
    use_candidate_subtitle_snapshot: bool = True,
    narration_audio_path_by_lang: dict[str, str | os.PathLike[str]] | None = None,
    intro_video_path_by_lang: dict[str, str | os.PathLike[str]] | None = None,
    include_background_music: bool = True,
) -> dict[str, Any]:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    languages = production_languages_for_scenes(scenes, target_languages or None, require_ready=True)
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

    # Inner Boss has a format-specific soundtrack. Story assembly disables the generic music library.
    boss_mix = inner_boss_mix.enabled(scenes, project_slug)
    base_music_path, base_music_volume = (
        (holiday_music(overlay_title_style) or _resolve_music_path(project_slug))
        if include_background_music and not boss_mix else (None, 0.0)
    )
    music_assets_by_language: dict[str, str | None] = {}
    renders_by_language: dict[str, dict[str, Any]] = {}
    subtitle_entries_by_language: dict[str, list[dict[str, Any]]] = {}
    subtitle_fingerprint_by_language: dict[str, str] = {}
    original_audio_preserved_by_language: dict[str, bool] = {}
    title_overlay_by_language: dict[str, dict[str, Any]] = {}
    end_title_overlay_by_language: dict[str, dict[str, Any]] = {}
    subtitle_overlay_by_language: dict[str, dict[str, Any]] = {}
    series_overlay_by_language: dict[str, dict[str, Any]] = {}
    artifact_bundle_key = _make_artifact_bundle_key(scenes, project_slug=project_slug)
    narration_paths = {
        str(language).strip().lower(): Path(path)
        for language, path in (narration_audio_path_by_lang or {}).items()
        if str(language).strip() and str(path).strip() and Path(path).is_file()
    }
    intro_paths = {
        str(language).strip().lower(): Path(path)
        for language, path in (intro_video_path_by_lang or {}).items()
        if str(language).strip() and str(path).strip() and Path(path).is_file()
    }
    if narration_paths:
        narration_fingerprint = hashlib.sha256()
        narration_fingerprint.update(artifact_bundle_key.encode("utf-8"))
        for language, path in sorted(narration_paths.items()):
            narration_fingerprint.update(language.encode("utf-8"))
            narration_fingerprint.update(path.read_bytes())
        artifact_bundle_key = narration_fingerprint.hexdigest()
    if intro_paths:
        intro_fingerprint = hashlib.sha256()
        intro_fingerprint.update(artifact_bundle_key.encode("utf-8"))
        for language, path in sorted(intro_paths.items()):
            intro_fingerprint.update(language.encode("utf-8"))
            intro_fingerprint.update(path.read_bytes())
        artifact_bundle_key = intro_fingerprint.hexdigest()
    target_width, target_height = _render_dimensions_for_scenes(scenes)
    upscaled_target_width, upscaled_target_height = _upscaled_dimensions_for_render(target_width, target_height)

    for language in languages:
        music_path, music_volume = base_music_path, base_music_volume
        scene_video_fingerprint = selected_scene_video_fingerprint_for_language(scenes, language)
        selected_segments: list[dict[str, Any]] = []
        for scene_index, scene in enumerate(scenes):
            selected = resolve_latest_approved_scene_video_for_final_build(scene, language)
            video_bytes = video_candidate_bytes(selected)
            if not video_bytes:
                candidate_id = (selected or {}).get("candidate_id") or "unknown"
                raise RuntimeError(
                    f"Missing video file for {language.upper()} scene {scene_index + 1} "
                    f"(approved candidate {candidate_id})."
                )
            speed_coef = _final_scene_speed_coef(scene, selected)
            source_duration = float(scene.get("duration_sec") or 0) or 0.0
            try:
                story_timeline_duration = (
                    float(scene.get("timeline_duration_sec") or 0.0)
                    if str(scene.get("generation_mode") or "").strip().lower() == "story"
                    else 0.0
                )
            except (TypeError, ValueError):
                story_timeline_duration = 0.0
            story_duration_by_lang = scene.get("timeline_duration_by_lang")
            try:
                language_story_duration = (
                    float(story_duration_by_lang.get(language) or 0.0)
                    if isinstance(story_duration_by_lang, dict)
                    else 0.0
                )
            except (TypeError, ValueError):
                language_story_duration = 0.0
            planned_final_duration = (
                language_story_duration
                if language_story_duration > 0
                else story_timeline_duration
                if story_timeline_duration > 0
                else source_duration / speed_coef
                if source_duration > 0
                else 0.0
            )
            # Scene.duration_sec is a generation plan, not proof of the media
            # file's real duration. Providers can return a longer clip (for
            # example 8 seconds for a 6-second request). Only an explicit Story
            # timeline, or a later narration fit, is allowed to trim the source.
            target_duration = (
                planned_final_duration
                if language_story_duration > 0 or story_timeline_duration > 0
                else None
            )
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
                    "target_duration": target_duration,
                }
            )
        if not selected_segments:
            continue

        narration_path = narration_paths.get(language)
        narration_volume = (
            DISCIPLINE_ANASTASIA_NARRATION_VOLUME
            if not boss_mix and str(project_slug or "").strip().lower() == "discipline" and language == "ru"
            else 1.0
        )
        narration_duration = float(_media_duration(narration_path) or 0.0) if narration_path is not None else 0.0
        planned_track_total = sum(
            float(item.get("planned_final_duration") or 0.0) for item in selected_segments
        )
        # A story timeline must end together with its approved narration.
        # Shorten every internal clip proportionally instead of padding silence
        # at the master tail; trimming only the last clip would leave the middle
        # of the video semantically out of sync. This applies to both language
        # tracks: a translation can be longer or shorter than Russian.
        is_story_track = any(
            str(item["scene"].get("generation_mode") or "").strip().lower() == "story"
            for item in selected_segments
        )
        if (
            is_story_track
            and narration_duration > 0
            and selected_segments
            and planned_track_total - narration_duration > (1.0 / 30.0)
        ):
            fitted = _proportional_frame_aligned_durations(
                [float(item.get("planned_final_duration") or 0.0) for item in selected_segments],
                narration_duration,
                fps=30,
            )
            for item, fitted_duration in zip(selected_segments, fitted):
                item["planned_final_duration"] = fitted_duration
                item["target_duration"] = fitted_duration
            logger.info(
                "post_production.story_narration_fit language=%s planned=%.3f narration=%.3f clips=%s",
                language,
                planned_track_total,
                narration_duration,
                len(selected_segments),
            )

        with tempfile.TemporaryDirectory(prefix=f"post_production_{language}_") as tmp:
            emit_progress(language, "normalizing", "Preparing scene clips")
            tmp_path = Path(tmp)
            normalized_paths: list[Path] = []
            normalized_intro_path: Path | None = None
            intro_duration = 0.0
            intro_path = intro_paths.get(language)
            if intro_path is not None:
                normalized_intro_path = tmp_path / f"discipline_intro_{language}_norm.mp4"
                _normalize_segment(
                    ffmpeg=ffmpeg,
                    src_path=intro_path,
                    dest_path=normalized_intro_path,
                    speed_coef=1.0,
                    target_width=target_width,
                    target_height=target_height,
                )
                intro_duration = float(_media_duration(normalized_intro_path) or 0.0)
                normalized_paths.append(normalized_intro_path)
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
                try:
                    _normalize_segment(
                        ffmpeg=ffmpeg,
                        src_path=src_path,
                        dest_path=normalized_path,
                        speed_coef=float(item["speed_coef"]),
                        target_width=target_width,
                        target_height=target_height,
                        **({"fit_full_clip": True} if (
                            item["scene"].get(MODE_KEY) == FLOW
                            or (is_story_track and narration_duration > 0 and item["target_duration"])
                        ) else {}),
                        target_duration_sec=(
                            float(item["target_duration"] or 0.0)
                            if item.get("target_duration") is not None
                            else None
                        ),
                    )
                except TypeError as exc:
                    if "target_duration_sec" in str(exc):
                        _normalize_segment(
                            ffmpeg=ffmpeg,
                            src_path=src_path,
                            dest_path=normalized_path,
                            speed_coef=float(item["speed_coef"]),
                            target_width=target_width,
                            target_height=target_height,
                        )
                    elif "target_width" in str(exc) or "target_height" in str(exc):
                        _normalize_segment(
                            ffmpeg=ffmpeg,
                            src_path=src_path,
                            dest_path=normalized_path,
                            speed_coef=float(item["speed_coef"]),
                        )
                    else:
                        raise
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
            if selected_segments:
                end_item = selected_segments[-1]
                end_duration = float(
                    end_item.get("actual_duration") or end_item.get("planned_final_duration") or 0.0
                )
                end_start = intro_duration + sum(
                    float(item.get("actual_duration") or item.get("planned_final_duration") or 0.0)
                    for item in selected_segments[:-1]
                )
                end_cue, end_style = _build_end_title_cue(
                    end_item["scene"],
                    language=language,
                    start_sec=end_start,
                    end_sec=end_start + end_duration,
                )
                if end_cue is not None:
                    end_title_ass_path = tmp_path / f"end_title_{language}.ass"
                    _write_burned_subtitle_ass(
                        end_title_ass_path,
                        [],
                        title_cues=[end_cue],
                        title_style=end_style,
                        video_width=target_width,
                        video_height=target_height,
                    )
                    end_title_path = tmp_path / f"final_clean_master_{language}_end_title.mp4"
                    emit_progress(language, "end_title_building", "Burning localized final title")
                    _render_subtitled_music_master(
                        ffmpeg=ffmpeg,
                        clean_path=clean_path,
                        subtitles_path=end_title_ass_path,
                        music_path=None,
                        music_volume=0.0,
                        dest_path=end_title_path,
                    )
                    clean_path = end_title_path
                    end_title_overlay_by_language[language] = {
                        "title": str(end_cue.get("text") or ""),
                        "start_sec": float(end_cue.get("start_sec") or 0.0),
                        "end_sec": float(end_cue.get("end_sec") or 0.0),
                        "style": end_style,
                    }
                    emit_progress(language, "end_title_ready", "Localized final title ready")
            if narration_path is not None:
                narrated_path = tmp_path / f"final_clean_master_{language}_narrated.mp4"
                emit_progress(language, "narration_building", "Adding approved ElevenLabs narration")
                if normalized_intro_path is not None:
                    _replace_master_audio_with_intro_and_narration(
                        ffmpeg=ffmpeg,
                        video_path=clean_path,
                        intro_video_path=normalized_intro_path,
                        narration_path=narration_path,
                        dest_path=narrated_path,
                        narration_volume=narration_volume,
                    )
                else:
                    _replace_master_audio_with_narration(
                        ffmpeg=ffmpeg,
                        video_path=clean_path,
                        narration_path=narration_path,
                        dest_path=narrated_path,
                        narration_volume=narration_volume,
                    )
                clean_path = narrated_path
                emit_progress(language, "narration_ready", "Approved narration added")
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
                try:
                    _upscale_master_to_4k(
                        ffmpeg=ffmpeg,
                        src_path=clean_path,
                        dest_path=upscaled_path,
                        target_width=upscaled_target_width,
                        target_height=upscaled_target_height,
                    )
                except TypeError as exc:
                    if "target_width" not in str(exc) and "target_height" not in str(exc):
                        raise
                    _upscale_master_to_4k(ffmpeg=ffmpeg, src_path=clean_path, dest_path=upscaled_path)
                upscaled_w, upscaled_h = _video_dimensions(upscaled_path)
                emit_progress(language, "upscale_ready", "4K master ready")
            render_base_path = upscaled_path or clean_path
            render_base_w = upscaled_w or clean_w
            render_base_h = upscaled_h or clean_h

            subtitle_entries: list[dict[str, Any]] = []
            needs_subtitled = bool(requested_variants & {"subtitled_music", "titled_subtitled_music"})
            accent_options: dict[str, Any] = {}
            if needs_subtitled:
                current_start = intro_duration
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
                        narration_offset_sec=intro_duration,
                    )
                    for entry in scene_entries:
                        entry["scene"] = int(item["scene_index"]) + 1
                        entry["candidate_id"] = str(item.get("candidate_id") or "")
                        entry["speed_coef"] = float(item["speed_coef"])
                        entry["timing_scale"] = timing_scale
                    subtitle_entries.extend(scene_entries)
                    current_start += actual_duration
                if boss_mix:
                    scene_timeline = [
                        (item["scene"], float(item.get("actual_duration") or item["planned_final_duration"]))
                        for item in selected_segments
                    ]
                    music_path, accents = _prepare_inner_boss_edit(
                        tmp_path, render_base_path, scene_timeline, subtitle_entries, language,
                        intro_duration=intro_duration,
                        artifact_bundle_key=artifact_bundle_key,
                    )
                    music_volume = inner_boss_mix.MIX_VOLUME
                    if accents:
                        accent_options["accent_cues"] = accents
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
            subtitle_cue = _build_title_subtitle_overlay_cue(
                overlay_subtitle_by_lang,
                language=language,
                duration_sec=first_segment_duration,
            )
            if title_cue:
                title_overlay_by_language[language] = {
                    "title": str(title_cue.get("text") or ""),
                    "start_sec": float(title_cue.get("start_sec") or 0.0),
                    "end_sec": float(title_cue.get("end_sec") or 0.0),
                }
            if subtitle_cue:
                subtitle_overlay_by_language[language] = {
                    "subtitle": str(subtitle_cue.get("text") or ""),
                    "start_sec": float(subtitle_cue.get("start_sec") or 0.0),
                    "end_sec": float(subtitle_cue.get("end_sec") or 0.0),
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
                    title_style=overlay_title_style,
                    video_width=render_base_w,
                    video_height=render_base_h,
                    **accent_options,
                )

                subtitled_path = tmp_path / f"final_subtitled_music_master_{language}.mp4"
                emit_progress(
                    language,
                    "subtitles_building",
                    "Rendering subtitles and music" if music_path else "Rendering subtitles without background music",
                )
                render_preserved_source_audio = _render_subtitled_music_master(
                    ffmpeg=ffmpeg,
                    clean_path=render_base_path,
                    subtitles_path=ass_path,
                    music_path=music_path,
                    music_volume=music_volume,
                    dest_path=subtitled_path,
                )
                emit_progress(
                    language,
                    "subtitles_ready",
                    "Subtitled/music master ready" if music_path else "Subtitled master without background music ready",
                )
            titled_subtitled_path: Path | None = None
            titled_render_preserved_source_audio: bool | None = None
            title_cues = [cue for cue in (series_cue, title_cue, subtitle_cue) if cue]
            title_cues.extend(_extra_title_cues(overlay_title_style, language, _media_duration(render_base_path) or 0.0))
            if "titled_subtitled_music" in requested_variants and title_cues:
                if subtitled_path is None and not (overlay_title_style or {}).get("replace_subtitle_phrases"):
                    ass_path = tmp_path / f"subtitles_{language}.ass"
                    _write_burned_subtitle_ass(
                        ass_path,
                        subtitle_cues,
                        title_style=overlay_title_style,
                        video_width=render_base_w,
                        video_height=render_base_h,
                        **accent_options,
                    )
                    subtitled_path = tmp_path / f"final_subtitled_music_master_{language}.mp4"
                    emit_progress(
                        language,
                        "subtitles_building",
                        "Rendering subtitles and music" if music_path else "Rendering subtitles without background music",
                    )
                    render_preserved_source_audio = _render_subtitled_music_master(
                        ffmpeg=ffmpeg,
                        clean_path=render_base_path,
                        subtitles_path=ass_path,
                        music_path=music_path,
                        music_volume=music_volume,
                        dest_path=subtitled_path,
                    )
                    emit_progress(
                        language,
                        "subtitles_ready",
                        "Subtitled/music master ready" if music_path else "Subtitled master without background music ready",
                    )
                titled_ass_path = tmp_path / f"title_overlay_{language}.ass"
                _write_burned_subtitle_ass(
                    titled_ass_path,
                    _without_large_title_words(subtitle_cues, title_cues, overlay_title_style, language)
                    if (overlay_title_style or {}).get("replace_subtitle_phrases") else [],
                    title_cues=title_cues,
                    title_style=overlay_title_style,
                    video_width=render_base_w,
                    video_height=render_base_h,
                    **(accent_options if (overlay_title_style or {}).get("replace_subtitle_phrases") else {}),
                )
                titled_subtitled_path = tmp_path / f"final_titled_subtitled_music_master_{language}.mp4"
                emit_progress(language, "title_building", "Burning title overlay")
                titled_render_preserved_source_audio = _render_subtitled_music_master(
                    ffmpeg=ffmpeg,
                    clean_path=render_base_path if (overlay_title_style or {}).get("replace_subtitle_phrases") else subtitled_path,
                    subtitles_path=titled_ass_path,
                    music_path=music_path if (overlay_title_style or {}).get("replace_subtitle_phrases") else None,
                    music_volume=music_volume if (overlay_title_style or {}).get("replace_subtitle_phrases") else 0.0,
                    dest_path=titled_subtitled_path,
                )
                emit_progress(language, "title_ready", "Title overlay master ready")

            music_assets_by_language[language] = music_path
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
                    "scene_video_fingerprint": scene_video_fingerprint,
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
                    "scene_video_fingerprint": scene_video_fingerprint,
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
                    "background_music_included": bool(music_path),
                    "source_audio_preserved": original_audio_preserved,
                    "subtitle_fingerprint": subtitle_fingerprint_by_language.get(language),
                    "scene_video_fingerprint": scene_video_fingerprint,
                }
            if titled_subtitled_path:
                titled_bytes = titled_subtitled_path.read_bytes()
                titled_w, titled_h = _video_dimensions(titled_subtitled_path)
                titled_original_audio_preserved = (
                    bool(titled_render_preserved_source_audio)
                    if titled_render_preserved_source_audio is not None
                    else _has_audio_stream(subtitled_path or render_base_path) and _has_audio_stream(titled_subtitled_path)
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
                    "background_music_included": bool(music_path),
                    "source_audio_preserved": titled_original_audio_preserved,
                    "subtitle_fingerprint": subtitle_fingerprint_by_language.get(language),
                    "scene_video_fingerprint": scene_video_fingerprint,
                }
            if not renders_by_language[language]:
                renders_by_language.pop(language, None)

    if not renders_by_language:
        raise RuntimeError("No final renders were produced for any language.")

    first_lang = next(iter(renders_by_language))
    music_path = music_assets_by_language[first_lang]
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
        "end_title_overlay_by_language": end_title_overlay_by_language,
        "subtitle_overlay_by_language": subtitle_overlay_by_language,
        "series_overlay_by_language": series_overlay_by_language,
        "title_overlay_style": dict(overlay_title_style or {}),
        "speech_budget_by_language": speech_budget_by_language,
        "original_audio_preserved_by_language": original_audio_preserved_by_language,
        "music_asset_uri": music_path,
        "background_music_included": bool(music_path),
        "languages": list(renders_by_language.keys()),
        "artifact_bundle_key": artifact_bundle_key,
    }


def build_upscaled_post_production_from_clean_master(
    *,
    clean_video_path: str | Path,
    language: str,
    project_slug: str | None = None,
    progress_callback: Any | None = None,
    scene_video_fingerprint: str | None = None,
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
        clean_w, clean_h = _video_dimensions(source_path)
        upscale_w, upscale_h = _upscaled_dimensions_for_render(clean_w or TARGET_WIDTH, clean_h or TARGET_HEIGHT)
        try:
            _upscale_master_to_4k(
                ffmpeg=ffmpeg,
                src_path=source_path,
                dest_path=upscaled_path,
                target_width=upscale_w,
                target_height=upscale_h,
            )
        except TypeError as exc:
            if "target_width" not in str(exc) and "target_height" not in str(exc):
                raise
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
            "scene_video_fingerprint": scene_video_fingerprint,
        }
    return {
        "final_upscaled_4k_video": payload,
        "renders_by_language": {lang: {"upscaled_4k": payload}},
        "subtitle_entries": [],
        "subtitle_entries_by_language": {},
        "title_overlay_by_language": {},
        "subtitle_overlay_by_language": {},
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
    scene_intervals: list[tuple[float, float]] | None = None,
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
        if scene_intervals:
            current_start, scene_end = scene_intervals[scene_index]
            planned_duration = scene_end - current_start
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
                "start_sec": round(float(entry.get("start_sec") or 0.0), 4),
                "end_sec": round(float(entry.get("end_sec") or 0.0), 4),
                "speed_coef": round(float(entry.get("speed_coef") or 0.0), 4),
                "fixed_block_words": [str(word) for word in entry.get("fixed_block_words") or []],
                "visible_word_count": int(entry.get("visible_word_count") or 0),
                "story_style_version": str(entry.get("story_style_version") or ""),
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


def _inner_boss_reused_timeline(
    scenes: list[dict[str, Any]], scene_intervals: list[tuple[float, float]] | None,
) -> list[tuple[dict[str, Any], float]]:
    return [(scene, end - start) for scene, (start, end) in zip(scenes, scene_intervals or [])]


def build_subtitled_post_production_from_upscaled_master(
    scenes: list[dict[str, Any]],
    *,
    upscaled_video_path: str | Path,
    language: str,
    project_slug: str | None = None,
    progress_callback: Any | None = None,
    use_candidate_subtitle_snapshot: bool = True,
    include_background_music: bool = True,
    overlay_title_style: dict[str, Any] | None = None,
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
    boss_mix = inner_boss_mix.enabled(scenes, project_slug)
    music_path, music_volume = (
        (holiday_music(overlay_title_style) or _resolve_music_path(project_slug))
        if include_background_music and not boss_mix else (None, 0.0)
    )
    scene_intervals = (
        _story_scene_intervals(scenes, language=lang, narration_duration_sec=_media_duration(source_path) or 0.0)
        if boss_mix else None
    )
    subtitle_entries = _subtitle_entries_for_language_from_plan(
        scenes,
        lang,
        use_candidate_subtitle_snapshot=use_candidate_subtitle_snapshot,
        scene_intervals=scene_intervals,
    )
    scene_video_fingerprint = selected_scene_video_fingerprint_for_language(scenes, lang)
    subtitle_fingerprint = subtitle_entries_fingerprint(subtitle_entries)
    subtitle_cues: list[dict[str, Any]] = []
    for item in subtitle_entries:
        subtitle_cues.extend(_build_burned_subtitle_cues(item, video_width=source_w, video_height=source_h))

    artifact_bundle_key = _make_artifact_bundle_key(scenes, project_slug=project_slug)
    with tempfile.TemporaryDirectory(prefix=f"post_production_subtitles_{lang}_") as tmp:
        tmp_path = Path(tmp)
        emit_progress("reuse_upscaled", "Using existing 4K master")
        accent_options: dict[str, Any] = {}
        if boss_mix:
            music_path, accents = _prepare_inner_boss_edit(
                tmp_path, source_path, _inner_boss_reused_timeline(scenes, scene_intervals),
                subtitle_entries, lang, intro_duration=0.0, artifact_bundle_key=artifact_bundle_key,
            )
            music_volume = inner_boss_mix.MIX_VOLUME
            if accents:
                accent_options["accent_cues"] = accents
        ass_path = tmp_path / f"subtitles_{lang}.ass"
        _write_burned_subtitle_ass(
            ass_path,
            subtitle_cues,
            title_style=overlay_title_style,
            video_width=source_w,
            video_height=source_h,
            **accent_options,
        )
        subtitled_path = tmp_path / f"final_subtitled_music_master_{lang}.mp4"
        emit_progress(
            "subtitles_building",
            "Rendering subtitles and music" if music_path else "Rendering subtitles without background music",
        )
        source_audio_preserved = _render_subtitled_music_master(
            ffmpeg=ffmpeg,
            clean_path=source_path,
            subtitles_path=ass_path,
            music_path=music_path,
            music_volume=music_volume,
            dest_path=subtitled_path,
        )
        emit_progress(
            "subtitles_ready",
            "Subtitled/music master ready" if music_path else "Subtitled master without background music ready",
        )
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
            "background_music_included": bool(music_path),
            "source_audio_preserved": bool(source_audio_preserved),
            "source_variant": "upscaled_4k",
            "source_video_path": str(source_path),
            "subtitle_fingerprint": subtitle_fingerprint,
            "scene_video_fingerprint": scene_video_fingerprint,
        }
    return {
        "final_subtitled_music_video": payload,
        "renders_by_language": {lang: {"subtitled_music": payload}},
        "subtitle_entries": subtitle_entries,
        "subtitle_entries_by_language": {lang: subtitle_entries},
        "subtitle_fingerprint_by_language": {lang: subtitle_fingerprint},
        "title_overlay_by_language": {},
        "subtitle_overlay_by_language": {},
        "series_overlay_by_language": {},
        "speech_budget_by_language": {},
        "original_audio_preserved_by_language": {lang: bool(source_audio_preserved)},
        "music_asset_uri": music_path,
        "background_music_included": bool(music_path),
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
    overlay_subtitle_by_lang: dict[str, Any] | None = None,
    overlay_title_style: dict[str, Any] | None = None,
    overlay_series_title_by_lang: dict[str, Any] | None = None,
    overlay_series_counter_by_lang: dict[str, Any] | None = None,
    progress_callback: Any | None = None,
    use_candidate_subtitle_snapshot: bool = True,
    source_has_subtitles: bool = True,
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
    scene_video_fingerprint = selected_scene_video_fingerprint_for_language(scenes, lang)
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
    subtitle_cue = _build_title_subtitle_overlay_cue(
        overlay_subtitle_by_lang,
        language=lang,
        duration_sec=title_duration,
    )
    title_cues = [cue for cue in (series_cue, title_cue, subtitle_cue) if cue]
    title_cues.extend(_extra_title_cues(overlay_title_style, lang, source_duration))
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
                    "source_variant": "subtitled_music" if source_has_subtitles else "upscaled_4k",
                }
            )
        except Exception:
            logger.exception("post_production.progress_callback_failed language=%s stage=%s", lang, stage)

    artifact_bundle_key = _make_artifact_bundle_key(scenes, project_slug=project_slug)
    caption_cues = []
    entries = []
    rebuild_boss_mix = not source_has_subtitles and inner_boss_mix.enabled(scenes, project_slug)
    scene_intervals = (
        _story_scene_intervals(scenes, language=lang, narration_duration_sec=source_duration)
        if rebuild_boss_mix else None
    )
    if not source_has_subtitles:
        entries = _subtitle_entries_for_language_from_plan(
            scenes, lang, use_candidate_subtitle_snapshot=use_candidate_subtitle_snapshot,
            scene_intervals=scene_intervals,
        )
        if rebuild_boss_mix:
            subtitle_fingerprint = subtitle_entries_fingerprint(entries)
        for entry in entries:
            caption_cues.extend(_build_burned_subtitle_cues(entry, video_width=source_w, video_height=source_h))
        caption_cues = _without_large_title_words(caption_cues, title_cues, overlay_title_style or {}, lang)
    with tempfile.TemporaryDirectory(prefix=f"post_production_title_{lang}_") as tmp:
        tmp_path = Path(tmp)
        emit_progress("reuse_subtitled", "Using existing subtitled/music master")
        music_path, music_volume = None, 0.0
        accent_options: dict[str, Any] = {}
        if rebuild_boss_mix:
            music_path, accents = _prepare_inner_boss_edit(
                tmp_path, source_path, _inner_boss_reused_timeline(scenes, scene_intervals),
                entries, lang, intro_duration=0.0, artifact_bundle_key=artifact_bundle_key,
            )
            music_volume = inner_boss_mix.MIX_VOLUME
            if accents:
                accent_options["accent_cues"] = accents
        titled_ass_path = tmp_path / f"title_overlay_{lang}.ass"
        _write_burned_subtitle_ass(
            titled_ass_path,
            caption_cues,
            title_cues=title_cues,
            title_style=overlay_title_style,
            video_width=source_w,
            video_height=source_h,
            **accent_options,
        )
        titled_path = tmp_path / f"final_titled_subtitled_music_master_{lang}.mp4"
        emit_progress("title_building", "Burning title overlay")
        source_audio_preserved = _render_subtitled_music_master(
            ffmpeg=ffmpeg,
            clean_path=source_path,
            subtitles_path=titled_ass_path,
            music_path=music_path,
            music_volume=music_volume,
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
            "music_asset_uri": music_path,
            "music_volume": music_volume,
            "source_audio_preserved": bool(source_audio_preserved),
            "source_variant": "subtitled_music" if source_has_subtitles else "upscaled_4k",
            "source_video_path": str(source_path),
            "subtitle_fingerprint": subtitle_fingerprint,
            "scene_video_fingerprint": scene_video_fingerprint,
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
    subtitle_overlay_by_language = {}
    if subtitle_cue:
        subtitle_overlay_by_language[lang] = {
            "subtitle": str(subtitle_cue.get("text") or ""),
            "start_sec": float(subtitle_cue.get("start_sec") or 0.0),
            "end_sec": float(subtitle_cue.get("end_sec") or 0.0),
        }
    return {
        "final_titled_subtitled_music_video": payload,
        "renders_by_language": {lang: {"titled_subtitled_music": payload}},
        "subtitle_entries": entries,
        "subtitle_entries_by_language": {lang: entries} if entries else {},
        "subtitle_fingerprint_by_language": {lang: subtitle_fingerprint},
        "title_overlay_by_language": title_overlay_by_language,
        "subtitle_overlay_by_language": subtitle_overlay_by_language,
        "series_overlay_by_language": series_overlay_by_language,
        "title_overlay_style": dict(overlay_title_style or {}),
        "speech_budget_by_language": {},
        "original_audio_preserved_by_language": {lang: bool(source_audio_preserved)},
        "music_asset_uri": music_path,
        "languages": [lang],
        "artifact_bundle_key": artifact_bundle_key,
    }
