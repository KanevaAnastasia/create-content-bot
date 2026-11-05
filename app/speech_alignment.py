"""Precise word-level speech alignment for short scene videos."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

import google.auth
from google.auth.transport.requests import AuthorizedSession


GOOGLE_CLOUD_SPEECH_TIMING_PROVIDER = "google_cloud_speech_v1"
GOOGLE_CLOUD_SPEECH_TIMING_SOURCE = "google_cloud_speech_word_offsets"
_SPEECH_RECOGNIZE_URL = "https://speech.googleapis.com/v1/speech:recognize"
_LANGUAGE_CODES = {
    "en": "en-US",
    "ru": "ru-RU",
}


@dataclass(frozen=True, slots=True)
class SpeechAlignmentResult:
    recognized_text: str
    word_timings: list[dict[str, Any]]
    confidence: float | None = None
    billed_duration_sec: float = 0.0
    provider: str = GOOGLE_CLOUD_SPEECH_TIMING_PROVIDER


def _resolve_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("ffmpeg is required for precise subtitle alignment") from exc


def _seconds(value: Any) -> float:
    raw = str(value or "0s").strip()
    if raw.endswith("s"):
        raw = raw[:-1]
    try:
        return max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _normalized_word(value: str) -> str:
    return "".join(re.findall(r"[\w]+", str(value or "").casefold(), flags=re.UNICODE))


def _words_with_transcript_punctuation(
    words: list[dict[str, Any]],
    transcript: str,
) -> list[dict[str, Any]]:
    transcript_words = str(transcript or "").split()
    if len(transcript_words) != len(words):
        return words
    if any(
        _normalized_word(item.get("word", "")) != _normalized_word(transcript_word)
        for item, transcript_word in zip(words, transcript_words, strict=True)
    ):
        return words
    return [
        {**item, "word": transcript_word}
        for item, transcript_word in zip(words, transcript_words, strict=True)
    ]


def _parse_speech_response(payload: dict[str, Any]) -> SpeechAlignmentResult:
    transcript_parts: list[str] = []
    word_timings: list[dict[str, Any]] = []
    confidences: list[float] = []
    for result in payload.get("results") or []:
        alternatives = result.get("alternatives") if isinstance(result, dict) else None
        alternative = alternatives[0] if isinstance(alternatives, list) and alternatives else None
        if not isinstance(alternative, dict):
            continue
        transcript = str(alternative.get("transcript") or "").strip()
        if transcript:
            transcript_parts.append(transcript)
        confidence = alternative.get("confidence")
        if isinstance(confidence, (int, float)):
            confidences.append(float(confidence))
        for word in alternative.get("words") or []:
            if not isinstance(word, dict) or not str(word.get("word") or "").strip():
                continue
            start_sec = _seconds(word.get("startTime"))
            end_sec = max(start_sec, _seconds(word.get("endTime")))
            word_timings.append(
                {
                    "word": str(word["word"]).strip(),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                }
            )

    recognized_text = " ".join(transcript_parts).strip()
    word_timings = _words_with_transcript_punctuation(word_timings, recognized_text)
    if not recognized_text or not word_timings:
        raise RuntimeError("Cloud Speech-to-Text returned no word-level timestamps")
    confidence = sum(confidences) / len(confidences) if confidences else None
    billed_duration_sec = _seconds(payload.get("totalBilledTime"))
    if billed_duration_sec <= 0 and word_timings:
        billed_duration_sec = float(math.ceil(max(float(item["end_sec"]) for item in word_timings)))
    return SpeechAlignmentResult(
        recognized_text=recognized_text,
        word_timings=word_timings,
        confidence=confidence,
        billed_duration_sec=billed_duration_sec,
    )


def align_video_speech(
    *,
    video_path: str | os.PathLike[str],
    language: str,
) -> SpeechAlignmentResult:
    """Transcribe a short scene and return Cloud STT word start/end offsets."""
    source = Path(video_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError(f"Video for speech alignment is missing: {source}")
    language_code = _LANGUAGE_CODES.get(str(language or "").strip().lower())
    if not language_code:
        raise RuntimeError(f"Precise speech alignment is not configured for language: {language}")

    with tempfile.TemporaryDirectory(prefix="speech_alignment_") as tmp:
        audio_path = Path(tmp) / "speech.flac"
        command = [
            _resolve_ffmpeg(),
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            str(audio_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Could not extract audio for precise subtitle alignment") from exc
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            raise RuntimeError("Video contains no readable audio for subtitle alignment")
        audio_bytes = audio_path.read_bytes()
        if len(audio_bytes) > 9_000_000:
            raise RuntimeError("Scene audio is too large for synchronous precise alignment")

    credentials, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = AuthorizedSession(credentials)
    response = session.post(
        _SPEECH_RECOGNIZE_URL,
        json={
            "config": {
                "encoding": "FLAC",
                "sampleRateHertz": 16000,
                "languageCode": language_code,
                "enableAutomaticPunctuation": True,
                "enableWordTimeOffsets": True,
                "enableWordConfidence": True,
            },
            "audio": {"content": base64.b64encode(audio_bytes).decode("ascii")},
        },
        timeout=90,
    )
    if not response.ok:
        try:
            error_payload = response.json()
            message = str((error_payload.get("error") or {}).get("message") or "").strip()
        except (TypeError, ValueError):
            message = ""
        detail = f": {message}" if message else ""
        raise RuntimeError(f"Cloud Speech-to-Text failed with HTTP {response.status_code}{detail}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Cloud Speech-to-Text returned an invalid response")
    return _parse_speech_response(payload)
