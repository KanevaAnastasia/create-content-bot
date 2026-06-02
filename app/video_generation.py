"""
Унифицированный вызов генератора видео по сцене.

Рендер делегируется функции `tools.gen_video.generate_video_via_vertex`
(без shell-команд и `VIDEO_GEN_CMD_TEMPLATE`).
"""

from __future__ import annotations

from tools.gen_video import (
    generate_video_via_vertex,
    get_last_video_result_source as _get_last_video_result_source,
    get_last_video_result_uri as _get_last_video_result_uri,
)


def _video_render_defaults(model: str) -> tuple[str, str, bool]:
    """
    Возвращает дефолтные параметры рендера по модели.

    Для ``veo3_fast`` фиксируем профиль:
    - качество: ``720``;
    - аспект: ``9:16``;
    - аудио: ``True``.
    """
    m = (model or "").strip().lower()
    if m == "veo3_fast":
        return ("720", "9:16", True)
    return ("720", "9:16", False)


def generate_video_bytes(
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None = None,
    quality: str | None = None,
    aspect_ratio: str | None = None,
    audio_enabled: bool | None = None,
    location: str | None = None,
) -> bytes:
    """
    Генерирует mp4 через прямой вызов Vertex-функции.

    Args:
        prompt: Финальный video prompt.
        model: Модель сцены (kling/veo3_fast/heygen...).
        duration_sec: Длительность сцены в секундах.
        start_image: Стартовый кадр (bytes).
        end_image: Конечный кадр (bytes), если есть.
        quality: Качество видео (например `1080`, `720`); если не задано — берётся профиль модели.
        aspect_ratio: Соотношение сторон (например `9:16`); если не задано — берётся профиль модели.
        audio_enabled: Нужен ли звук; если не задано — берётся профиль модели.
        location: Регион Vertex (например `us-central1`); если не задан — берётся из env.
    Returns:
        Байты готового mp4.
    Raises:
        RuntimeError: При ошибке запроса к Vertex API или пустом ответе.
    """
    default_quality, default_aspect_ratio, default_audio_enabled = _video_render_defaults(model)
    eff_quality = (quality or default_quality).strip()
    eff_aspect_ratio = (aspect_ratio or default_aspect_ratio).strip()
    eff_audio_enabled = default_audio_enabled if audio_enabled is None else bool(audio_enabled)
    return generate_video_via_vertex(
        prompt=prompt,
        model=model,
        duration_sec=duration_sec,
        start_image=start_image,
        end_image=end_image,
        quality=eff_quality,
        aspect_ratio=eff_aspect_ratio,
        audio_enabled=eff_audio_enabled,
        location=location,
    )


def get_last_video_result_source() -> str | None:
    """Returns where the last video payload was obtained from."""
    return _get_last_video_result_source()


def get_last_video_result_uri() -> str | None:
    """Returns URI used for last video payload fetch, if available."""
    return _get_last_video_result_uri()
