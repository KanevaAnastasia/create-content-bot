"""Small ElevenLabs text-to-speech client for one-off audio generation."""

from __future__ import annotations

from dataclasses import dataclass
import os

import requests

from .constants import ELEVENLABS_API_KEY_ENV_NAME, ELEVENLABS_VOICE_ID_ENV_NAME


DEFAULT_ELEVENLABS_TTS_MODEL = "eleven_v4"
DEFAULT_ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"


@dataclass(frozen=True, slots=True)
class ElevenLabsAudioResult:
    audio_bytes: bytes
    content_type: str
    request_id: str | None
    history_item_id: str | None
    model_id: str
    voice_id: str
    output_format: str


def _env_required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Не задан {name}.")
    return value


def generate_elevenlabs_speech(
    text: str,
    *,
    voice_id: str | None = None,
    model_id: str = DEFAULT_ELEVENLABS_TTS_MODEL,
    output_format: str = DEFAULT_ELEVENLABS_OUTPUT_FORMAT,
    language_code: str = "ru",
) -> ElevenLabsAudioResult:
    """Generate speech with the configured ElevenLabs API key and voice id."""
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("Текст для озвучки пустой.")

    api_key = _env_required(ELEVENLABS_API_KEY_ENV_NAME)
    effective_voice_id = (voice_id or os.getenv(ELEVENLABS_VOICE_ID_ENV_NAME) or "").strip()
    if not effective_voice_id:
        raise RuntimeError(f"Не задан {ELEVENLABS_VOICE_ID_ENV_NAME}.")

    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{effective_voice_id}",
        params={"output_format": output_format},
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={
            "text": clean_text,
            "model_id": model_id,
            "language_code": language_code,
            "apply_text_normalization": "auto",
        },
        timeout=180,
    )
    content_type = response.headers.get("content-type") or ""
    if not response.ok or not content_type.lower().startswith("audio/"):
        try:
            err = response.json()
        except Exception:
            err = response.text[:1000]
        raise RuntimeError(f"ElevenLabs TTS failed: HTTP {response.status_code}: {err}")

    return ElevenLabsAudioResult(
        audio_bytes=response.content,
        content_type=content_type,
        request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
        history_item_id=response.headers.get("history-item-id"),
        model_id=model_id,
        voice_id=effective_voice_id,
        output_format=output_format,
    )
