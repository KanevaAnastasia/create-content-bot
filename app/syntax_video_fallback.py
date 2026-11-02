"""Syntax fallback public contract and adapter entrypoint."""

from __future__ import annotations

from dataclasses import dataclass

from .syntax_telethon_fallback import run_syntax_telethon_fallback, run_syntax_telethon_fallback_batch


@dataclass(frozen=True, slots=True)
class FallbackResult:
    """
    Унифицированный результат попытки fallback-генерации видео.

    Поля:
        success: Успешна ли попытка fallback.
        video_bytes: Готовое видео в памяти (если доступно).
        video_uri: URI до видео (если провайдер вернул ссылку вместо байтов).
        provider_message: Текст провайдера для UX/логов.
        attempt_id: Идентификатор попытки для трассировки.
    """

    success: bool
    video_bytes: bytes | None
    video_uri: str | None
    provider_message: str
    attempt_id: str


@dataclass(frozen=True, slots=True)
class BatchFallbackRequest:
    key: str
    prompt: str
    start_image: bytes
    end_image: bytes | None = None


def try_generate_via_syntax(
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None = None,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
    syntax_provider: str | None = None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
) -> FallbackResult:
    """Try generating video via Syntax Telegram fallback channel."""
    return run_syntax_telethon_fallback(
        prompt=prompt,
        model=model,
        duration_sec=duration_sec,
        start_image=start_image,
        end_image=end_image,
        timeout_sec=timeout_sec,
        acquire_timeout_sec=acquire_timeout_sec,
        syntax_provider=syntax_provider,
        aspect_ratio=aspect_ratio,
        audio_enabled=audio_enabled,
    )


def try_generate_via_syntax_batch(
    *,
    requests: list[BatchFallbackRequest],
    model: str,
    duration_sec: int,
    timeout_sec: int | None = None,
    acquire_timeout_sec: int | None = None,
    syntax_provider: str | None = None,
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
) -> dict[str, FallbackResult]:
    """Try generating multiple videos via one Syntax Telegram session."""
    return run_syntax_telethon_fallback_batch(
        requests=requests,
        model=model,
        duration_sec=duration_sec,
        timeout_sec=timeout_sec,
        acquire_timeout_sec=acquire_timeout_sec,
        syntax_provider=syntax_provider,
        aspect_ratio=aspect_ratio,
        audio_enabled=audio_enabled,
    )
