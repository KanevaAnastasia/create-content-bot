"""Input-media preparation and timed transcription for visualization mode."""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ai_approval_service import _resolve_ffmpeg
from .llm_utils import GEMINI_MODEL_PROFILE_EDIT, extract_json_object, gemini_generate_sync


VISUALIZATION_SOURCE_MEDIA_SESSION_KEY = "visualization_source_media"
VISUALIZATION_TRANSCRIPT_SEGMENTS_SESSION_KEY = "visualization_transcript_segments"
VISUALIZATION_INPUT_KIND_SESSION_KEY = "visualization_input_kind"
STATE_WAITING_VISUALIZATION_MEDIA = "waiting_visualization_media"


@dataclass(frozen=True, slots=True)
class PreparedVisualizationMedia:
    audio_bytes: bytes
    audio_path: str
    duration_sec: float
    analysis_video_bytes: bytes


@dataclass(frozen=True, slots=True)
class VisualizationTranscript:
    title: str
    text: str
    segments: list[dict[str, Any]]
    alignment: dict[str, list[Any]]


def _media_duration(ffmpeg: str, path: Path) -> float:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or proc.stdout or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def prepare_visualization_media(
    media_bytes: bytes,
    *,
    chat_id: int,
    file_name: str = "",
) -> PreparedVisualizationMedia:
    """Extract one normalized MP3 and make an MP4 Gemini can timestamp reliably."""
    if not media_bytes:
        raise ValueError("Медиафайл пуст.")
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    suffix = Path(str(file_name or "")).suffix.lower()
    if not suffix or len(suffix) > 8:
        suffix = ".bin"
    target_dir = Path.cwd() / "artifacts" / "visualization_audio" / str(int(chat_id))
    target_dir.mkdir(parents=True, exist_ok=True)
    audio_path = target_dir / f"{uuid.uuid4()}.mp3"
    with tempfile.TemporaryDirectory(prefix="content_fabric_visualization_") as tmp:
        tmp_dir = Path(tmp)
        source_path = tmp_dir / f"source{suffix}"
        source_path.write_bytes(media_bytes)
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-map",
                "0:a:0",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(audio_path),
            ],
            check=False,
            capture_output=True,
            timeout=600,
        )
        if proc.returncode != 0 or not audio_path.is_file() or audio_path.stat().st_size <= 0:
            audio_path.unlink(missing_ok=True)
            detail = (proc.stderr or b"").decode("utf-8", errors="replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
            raise RuntimeError(f"Не удалось извлечь аудиодорожку: {detail[-500:]}")
        duration = _media_duration(ffmpeg, audio_path)
        if duration <= 0:
            audio_path.unlink(missing_ok=True)
            raise RuntimeError("Не удалось определить длительность аудиодорожки.")
        proxy_path = tmp_dir / "transcription_proxy.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=360x640:r=1",
                "-i",
                str(audio_path),
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "stillimage",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(proxy_path),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
        analysis_video_bytes = proxy_path.read_bytes()
    return PreparedVisualizationMedia(
        audio_bytes=audio_path.read_bytes(),
        audio_path=str(audio_path.resolve()),
        duration_sec=duration,
        analysis_video_bytes=analysis_video_bytes,
    )


def trim_visualization_audio(audio_path: str, *, end_sec: float) -> str:
    """Create a deterministic trimmed copy used by project output policies."""
    source = Path(str(audio_path or ""))
    duration = max(0.0, float(end_sec or 0.0))
    if not source.is_file() or duration <= 0:
        raise ValueError("Не удалось обрезать исходную аудиодорожку.")
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    suffix = str(duration).replace(".", "_")
    target = source.with_name(f"{source.stem}_trimmed_{suffix}.mp3")
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(target),
        ],
        check=False,
        capture_output=True,
        timeout=600,
    )
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        target.unlink(missing_ok=True)
        detail = (proc.stderr or b"").decode("utf-8", errors="replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
        raise RuntimeError(f"Не удалось обрезать аудиодорожку: {detail[-500:]}")
    return str(target.resolve())


def resolve_visualization_trim_end(
    duration_sec: float,
    *,
    output_policy: dict[str, Any] | None = None,
    source_media: dict[str, Any] | None = None,
) -> float:
    """Resolve project-specific outro removal and small tail rounding."""
    duration = max(0.0, float(duration_sec or 0.0))
    trim_end = duration
    policy = output_policy if isinstance(output_policy, dict) else {}
    media = source_media if isinstance(source_media, dict) else {}

    # Keep support for an explicitly configured one-off target, but do not
    # impose one when a project has no target_duration_sec setting.
    try:
        target = float(policy.get("target_duration_sec") or 0.0)
        tolerance = max(0.0, float(policy.get("duration_tolerance_sec") or 0.0))
    except (TypeError, ValueError):
        target = 0.0
        tolerance = 0.0
    if target > 0 and trim_end > target + tolerance:
        trim_end = target

    if bool(policy.get("remove_social_outro")):
        raw_outro_start = media.get("author_outro_start_sec", media.get("social_outro_start_sec"))
        try:
            outro_start = float(raw_outro_start or 0.0)
        except (TypeError, ValueError):
            outro_start = 0.0
        if 0 < outro_start < trim_end:
            trim_end = outro_start

    try:
        multiple = float(policy.get("trim_to_previous_multiple_sec") or 0.0)
        max_tail = max(0.0, float(policy.get("max_discard_tail_sec") or 0.0))
    except (TypeError, ValueError):
        multiple = 0.0
        max_tail = 0.0
    if multiple > 0 and max_tail > 0:
        lower_multiple = math.floor((trim_end + 1e-9) / multiple) * multiple
        discarded_tail = trim_end - lower_multiple
        if lower_multiple > 0 and 1e-3 < discarded_tail <= max_tail + 1e-3:
            trim_end = lower_multiple

    return round(trim_end, 3)


def visualization_transcription_prompt(duration_sec: float) -> str:
    return (
        "Ты профессиональный транскрибатор. Прослушай приложенную аудиодорожку целиком; чёрный видеоряд игнорируй. "
        "Определи язык автоматически. Выпиши речь дословно, без пересказа, без исправления фактов и без описания звуков. "
        "Раздели речь на короткие законченные фразы по естественным паузам и укажи реальные временные границы каждой фразы. "
        "Сегменты должны непрерывно следовать по порядку, не перекрываться и покрывать всю распознанную речь. "
        "start_sec — начало первого произнесённого слова, end_sec — конец последнего слова сегмента. "
        "Верни только JSON без Markdown:\n"
        "{\n"
        '  "title": "короткий заголовок по содержанию",\n'
        '  "full_text": "полная дословная расшифровка с пунктуацией",\n'
        '  "segments": [\n'
        '    {"start_sec": 0.0, "end_sec": 3.2, "text": "точный текст фразы"}\n'
        "  ]\n"
        "}\n"
        f"Длительность дорожки по ffmpeg: {float(duration_sec):.3f} сек. "
        "Ни один end_sec не должен превышать эту длительность. Если речи нет, верни пустые full_text и segments."
    )


def _clean_segments(raw: Any, duration_sec: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    previous_end = 0.0
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        try:
            start = max(previous_end, float(item.get("start_sec") or 0.0))
            end = min(float(duration_sec), float(item.get("end_sec") or start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        result.append({"start_sec": round(start, 3), "end_sec": round(end, 3), "text": text})
        previous_end = end
    return result


def _alignment_from_segments(text: str, segments: list[dict[str, Any]], duration_sec: float) -> dict[str, list[Any]]:
    """Build Story-compatible character timings while preserving phrase timestamps."""
    clean_text = str(text or "").strip()
    if not clean_text:
        return {"characters": [], "character_start_times_seconds": [], "character_end_times_seconds": []}
    characters = list(clean_text)
    starts = [0.0] * len(characters)
    ends = [0.0] * len(characters)
    cursor = 0
    last_end = 0.0
    folded = clean_text.casefold()
    for segment in segments:
        segment_text = str(segment.get("text") or "").strip()
        if not segment_text:
            continue
        found = folded.find(segment_text.casefold(), cursor)
        if found < 0:
            # Punctuation or spacing may differ slightly; keep chronological order.
            found = cursor
            length = min(len(segment_text), len(characters) - found)
        else:
            length = len(segment_text)
        stop = min(len(characters), found + max(1, length))
        start_sec = max(last_end, float(segment.get("start_sec") or 0.0))
        end_sec = max(start_sec + 0.001, float(segment.get("end_sec") or start_sec))
        width = max(1, stop - found)
        for index in range(found, stop):
            starts[index] = start_sec + (end_sec - start_sec) * ((index - found) / width)
            ends[index] = start_sec + (end_sec - start_sec) * ((index - found + 1) / width)
        cursor = stop
        last_end = end_sec
    # Fill unmatched punctuation/gaps monotonically between the timed regions.
    timed_indices = [index for index, value in enumerate(ends) if value > 0]
    if not timed_indices:
        step = max(0.001, float(duration_sec) / max(1, len(characters)))
        starts = [index * step for index in range(len(characters))]
        ends = [(index + 1) * step for index in range(len(characters))]
    else:
        for index in range(len(characters)):
            if ends[index] > 0:
                continue
            previous = ends[index - 1] if index > 0 else 0.0
            next_start = next((starts[j] for j in range(index + 1, len(characters)) if ends[j] > 0), float(duration_sec))
            starts[index] = previous
            ends[index] = max(previous + 0.001, next_start)
    return {
        "characters": characters,
        "character_start_times_seconds": [round(value, 4) for value in starts],
        "character_end_times_seconds": [round(min(float(duration_sec), value), 4) for value in ends],
    }


def parse_visualization_transcript(raw: str, *, duration_sec: float) -> VisualizationTranscript:
    data = extract_json_object(str(raw or ""))
    segments = _clean_segments(data.get("segments"), duration_sec)
    full_text = " ".join(str(data.get("full_text") or "").split())
    if not full_text and segments:
        full_text = " ".join(str(item["text"]) for item in segments)
    if not full_text:
        raise ValueError("В дорожке не удалось распознать речь.")
    if not segments:
        segments = [{"start_sec": 0.0, "end_sec": float(duration_sec), "text": full_text}]
    title = " ".join(str(data.get("title") or "Визуализация").split()) or "Визуализация"
    return VisualizationTranscript(
        title=title,
        text=full_text,
        segments=segments,
        alignment=_alignment_from_segments(full_text, segments, duration_sec),
    )


def transcribe_visualization_media(analysis_video_bytes: bytes, *, duration_sec: float) -> VisualizationTranscript:
    raw = gemini_generate_sync(
        visualization_transcription_prompt(duration_sec),
        images=[analysis_video_bytes],
        model_profile=GEMINI_MODEL_PROFILE_EDIT,
    )
    return parse_visualization_transcript(raw, duration_sec=duration_sec)


def format_visualization_transcript(transcript: VisualizationTranscript) -> str:
    lines = [f"РАСШИФРОВКА · {transcript.title}", "", transcript.text, "", "ТАЙМИНГИ"]
    for item in transcript.segments:
        lines.append(
            f"{float(item['start_sec']):.2f}–{float(item['end_sec']):.2f} сек · {item['text']}"
        )
    return "\n".join(lines)
