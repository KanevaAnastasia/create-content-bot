"""Single-frame Veo generation from a user composition reference."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from .character_catalog import CHARACTER_CATALOG, character_prompt_catalog
from .generation_modes import GENERATION_MODE_REFERENCE
from .llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, TEXT_MODEL_PROFILE_SHORT, gemini_generate_sync


REFERENCE_MODE_LABEL = "По референсу"

STATE_WAITING_REFERENCE_IMAGE = "waiting_reference_image"
STATE_WAITING_REFERENCE_CHARACTERS = "waiting_reference_characters"
STATE_WAITING_REFERENCE_COMMENT = "waiting_reference_comment"
STATE_WAITING_REFERENCE_DESCRIPTION_APPROVAL = "waiting_reference_description_approval"
STATE_WAITING_REFERENCE_ORIGINAL_AUDIO_CHOICE = "waiting_reference_original_audio_choice"
STATE_WAITING_REFERENCE_MUSIC_CHOICE = "waiting_reference_music_choice"
STATE_WAITING_REFERENCE_CUSTOM_MUSIC = "waiting_reference_custom_music"

REFERENCE_SOURCE_IMAGE_SESSION_KEY = "reference_source_image"
REFERENCE_COMMENT_SESSION_KEY = "reference_comment"
REFERENCE_CHARACTERS_SESSION_KEY = "reference_characters"
REFERENCE_DESCRIPTION_SESSION_KEY = "reference_short_description"
REFERENCE_DESCRIPTION_FEEDBACK_PENDING_KEY = "reference_description_feedback_pending"
REFERENCE_ORIGINAL_AUDIO_CHOICE_SESSION_KEY = "reference_original_audio_choice"
REFERENCE_MUSIC_CHOICE_SESSION_KEY = "reference_music_choice"
REFERENCE_MUSIC_SOURCE_PATH_SESSION_KEY = "reference_music_source_path"
REFERENCE_MUSIC_RESULT_PATH_SESSION_KEY = "reference_music_result_path"

REFERENCE_MUSIC_NONE = "Без музыки"
REFERENCE_MUSIC_DEFAULT = "Дефолтная"
REFERENCE_MUSIC_HOLIDAY = "Новогодняя"
REFERENCE_MUSIC_CUSTOM = "Своя"

REFERENCE_ORIGINAL_AUDIO_KEEP = "Сохранить оригинальное аудио"
REFERENCE_ORIGINAL_AUDIO_REMOVE = "Убрать оригинальное аудио"

REFERENCE_NO_EXTRA_CHARACTERS = "Без других"
REFERENCE_NO_COMMENT = "Без комментария"

_BASE_CHARACTERS = ("grandma", "cat")
_EXTRA_CHARACTER_ORDER = (
    "zaya",
    "markiz",
    "grandfather",
    "daughter",
    "granddaughter",
    "grandson",
    "dog",
)

_ALIASES = {
    "зая": "zaya",
    "белая кошка": "zaya",
    "маркиз": "markiz",
    "черно-белый кот": "markiz",
    "чёрно-белый кот": "markiz",
    "дед": "grandfather",
    "дедушка": "grandfather",
    "дочка": "daughter",
    "дочь": "daughter",
    "внучка": "granddaughter",
    "внук": "grandson",
    "пес": "dog",
    "пёс": "dog",
    "собака": "dog",
}


def is_reference_generation(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get("generation_mode")
    return str(value or "").strip().lower() == GENERATION_MODE_REFERENCE


def reference_character_choices_text() -> str:
    catalog = character_prompt_catalog(list(_EXTRA_CHARACTER_ORDER))
    names = [str((catalog.get(key) or {}).get("name") or key) for key in _EXTRA_CHARACTER_ORDER]
    return ", ".join(names)


def parse_reference_extra_characters(value: str) -> tuple[list[str], list[str]]:
    raw = str(value or "").strip()
    if not raw or raw.casefold() in {
        REFERENCE_NO_EXTRA_CHARACTERS.casefold(),
        "нет",
        "никого",
        "none",
    }:
        return [], []
    normalized = raw.casefold().replace("ё", "е")
    selected: list[str] = []
    matched_spans: list[tuple[int, int]] = []
    aliases = sorted(_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    for alias, key in aliases:
        alias_norm = alias.casefold().replace("ё", "е")
        for match in re.finditer(rf"(?<![\w-]){re.escape(alias_norm)}(?![\w-])", normalized):
            if any(match.start() < end and match.end() > start for start, end in matched_spans):
                continue
            matched_spans.append((match.start(), match.end()))
            if key not in selected:
                selected.append(key)
    leftovers = normalized
    for start, end in sorted(matched_spans, reverse=True):
        leftovers = leftovers[:start] + " " * (end - start) + leftovers[end:]
    leftovers = re.sub(r"[\s,;/.+]+", " ", leftovers).strip()
    unknown = [part for part in leftovers.split() if part not in {"и", "еще", "ещё", "также"}]
    ordered = [key for key in _EXTRA_CHARACTER_ORDER if key in selected]
    return ordered, unknown


def reference_character_summary(characters: list[str] | None) -> str:
    keys = [str(key) for key in (characters or _BASE_CHARACTERS)]
    if keys == ["vasily"]:
        return "Василий"
    catalog = character_prompt_catalog(keys)
    return ", ".join(str((catalog.get(key) or {}).get("name") or key) for key in keys)


def _character_context(characters: list[str]) -> str:
    catalog = character_prompt_catalog(characters)
    blocks: list[str] = []
    for key in characters:
        item = catalog.get(key) or CHARACTER_CATALOG.get(key) or {}
        blocks.append(
            f"{key}: {item.get('name') or key}. {str(item.get('description') or '').strip()}"
        )
    return "\n".join(blocks)


def load_reference_vasily() -> dict[str, Any]:
    from .project_profiles import DISCIPLINE_PROJECT_SLUG, project_story_characters
    from .story_generation import _is_vasily_identity

    characters, _ = project_story_characters(DISCIPLINE_PROJECT_SLUG)
    vasily = next((character for character in characters if _is_vasily_identity(character)), None)
    if not vasily or not Path(vasily["asset_uri"].removeprefix("file://")).is_file():
        raise ValueError("Не найден утверждённый референс Василия в проекте «Дисциплина».")
    return vasily


def generate_reference_image_prompt(
    image_bytes: bytes,
    *,
    comment: str = "",
    extra_characters: list[str] | None = None,
    vasily: dict[str, Any] | None = None,
) -> str:
    # The user comment is the image prompt, in every project. Attachments carry
    # composition and character identity; never ask a text model to reinterpret it.
    return comment if comment.strip() else "Повтори изображение-референс."


def generate_reference_video_prompt(
    frame_bytes: bytes,
    *,
    image_prompt: str,
    comment: str = "",
    characters: list[str] | None = None,
) -> str:
    cast = characters or list(_BASE_CHARACTERS)
    prompt = (
        "Напиши один точный English production prompt for Veo from the attached approved vertical start frame. "
        "This is one continuous 8-second 9:16 shot. Preserve the exact character identities, clothing, set, props, "
        "lighting, colors and starting composition from the frame. Add one clear, natural action that develops the "
        "visible situation without a cut, teleportation, morphing, duplicated limbs, disappearing objects or camera jumps. "
        "Use restrained camera movement only when it helps the action. No captions, logos, music or dialogue unless the "
        "user explicitly requested speech. Natural room ambience is allowed. Return only the final Veo prompt.\n\n"
        f"CAST: {reference_character_summary(cast)}\n"
        f"APPROVED IMAGE PROMPT:\n{image_prompt.strip()}\n\n"
        f"USER COMMENT:\n{comment.strip() or 'none'}"
    )
    return str(
        gemini_generate_sync(
            prompt,
            images=[frame_bytes],
            model_profile=GEMINI_MODEL_PROFILE_CREATIVE,
        )
        or ""
    ).strip()


def _clean_description(value: str) -> str:
    text = " ".join(str(value or "").strip().strip('"«»').split())
    text = re.sub(r"^(?:описание|description)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip().strip('"«»').strip()


def _sentence_count(value: str) -> int:
    return len(re.findall(r"[.!?]+(?=\s|$)", str(value or "").strip()))


def _force_one_sentence(value: str) -> str:
    text = _clean_description(value).replace("—", " ").replace("–", " ")
    text = re.sub(r"[.!?]+(?=\s+\S)", ";", text)
    text = re.sub(r"\s+", " ", text).strip(" ;")
    if text and text[-1] not in ".!?":
        text += "."
    return text


def generate_reference_description(
    *,
    image_prompt: str,
    video_prompt: str,
    comment: str = "",
    current: str = "",
    feedback: str = "",
) -> str:
    prompt = (
        "Напиши короткое естественное описание к ролику на русском языке, не длиннее 160 символов. "
        "Строго одно предложение, без заголовка, "
        "списка, хэштегов, эмодзи и призыва подписаться. Назови конкретное действие или ситуацию из ролика. "
        "Не выдумывай рецепт, результат или реплику, которых нет в материалах. Не используй тире. "
        "Верни только это предложение.\n\n"
        f"ПРОМПТ КАДРА:\n{image_prompt.strip()}\n\n"
        f"ПРОМПТ ВИДЕО:\n{video_prompt.strip()}\n\n"
        f"КОММЕНТАРИЙ ПОЛЬЗОВАТЕЛЯ:\n{comment.strip() or 'нет'}\n\n"
        f"ТЕКУЩИЙ ВАРИАНТ:\n{current.strip() or 'нет'}\n\n"
        f"ПРАВКА ПОЛЬЗОВАТЕЛЯ:\n{feedback.strip() or 'нет'}"
    )
    description = _clean_description(
        gemini_generate_sync(prompt, model_profile=TEXT_MODEL_PROFILE_SHORT)
    )
    if _sentence_count(description) > 1 or len(description) > 180:
        repair_prompt = (
            "Перепиши текст ниже в одно короткое естественное русское предложение до 160 символов. Сохрани конкретное действие, "
            "не добавляй факты, хэштеги, эмодзи, заголовок или тире. Верни только одно предложение.\n\n"
            + description
        )
        description = _clean_description(
            gemini_generate_sync(repair_prompt, model_profile=TEXT_MODEL_PROFILE_SHORT)
        )
    return _force_one_sentence(description)


def build_reference_scene(
    *,
    source_image: bytes,
    image_prompt: str,
    comment: str = "",
    extra_characters: list[str] | None = None,
    vasily: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import base64

    characters = list(_BASE_CHARACTERS)
    for key in extra_characters or []:
        if key in _EXTRA_CHARACTER_ORDER and key not in characters:
            characters.append(key)
    scene = {
        "scene": 1,
        "generation_mode": GENERATION_MODE_REFERENCE,
        "model": "veo3_fast",
        "duration_sec": 8,
        "aspect_ratio": "9:16",
        "characters": characters,
        "text": "",
        "speech_by_lang": {"ru": ""},
        "action": comment.strip() or "Оживить действие из пользовательского референса.",
        "recipe_step": comment.strip() or "Оживить действие из пользовательского референса.",
        "shot_type": "reference",
        "continuity_anchors": ["композиция пользовательского референса", "утверждённые персонажи"],
        "start_frame_prompt": image_prompt.strip(),
        "start_frame_prompt_ru": image_prompt.strip(),
        "start_frame_prompt_raw_mode": True,
        "end_frame_prompt": "",
        "video_prompt": "",
        "video_prompt_by_lang": {"ru": ""},
        "scene_user_reference_images_by_kind": {
            "start": [base64.b64encode(source_image).decode("ascii")]
        },
    }
    if vasily is not None:
        scene.update({
            "characters": ["vasily"],
            "references": {"vasily": vasily["asset_uri"].removeprefix("file://")},
            "story_required_reference_ids": ["vasily"],
        })
    return scene


def reference_music_choice(value: str) -> str | None:
    low = " ".join(str(value or "").strip().casefold().split())
    aliases = {
        REFERENCE_MUSIC_NONE.casefold(): "none",
        "без": "none",
        "нет": "none",
        "none": "none",
        REFERENCE_MUSIC_DEFAULT.casefold(): "default",
        "обычная": "default",
        "стандартная": "default",
        "default": "default",
        REFERENCE_MUSIC_HOLIDAY.casefold(): "holiday",
        "новый год": "holiday",
        "праздничная": "holiday",
        "christmas": "holiday",
        REFERENCE_MUSIC_CUSTOM.casefold(): "custom",
        "моя": "custom",
        "загрузить": "custom",
        "custom": "custom",
    }
    return aliases.get(low)


def reference_original_audio_choice(value: str) -> str | None:
    low = " ".join(str(value or "").strip().casefold().split())
    aliases = {
        REFERENCE_ORIGINAL_AUDIO_KEEP.casefold(): "keep",
        "сохранить": "keep",
        "оставить": "keep",
        "оставить звук": "keep",
        "сохранить оригинальный звук": "keep",
        "keep": "keep",
        REFERENCE_ORIGINAL_AUDIO_REMOVE.casefold(): "remove",
        "убрать": "remove",
        "убрать звук": "remove",
        "убрать оригинальный звук": "remove",
        "без оригинального звука": "remove",
        "remove": "remove",
    }
    return aliases.get(low)


def _safe_media_suffix(file_name: str, mime_type: str) -> str:
    suffix = Path(str(file_name or "")).suffix.lower()
    if suffix in {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}:
        return suffix
    mime = str(mime_type or "").casefold()
    if "wav" in mime:
        return ".wav"
    if "ogg" in mime or "opus" in mime:
        return ".ogg"
    if "mp4" in mime or "m4a" in mime:
        return ".m4a"
    return ".mp3"


def store_reference_custom_music(
    music_bytes: bytes,
    *,
    run_id: str = "",
    file_name: str = "",
    mime_type: str = "",
) -> str:
    if not music_bytes:
        raise ValueError("Пустой музыкальный файл")
    safe_run_id = str(run_id or "").strip()
    try:
        safe_run_id = str(uuid.UUID(safe_run_id))
    except (TypeError, ValueError, AttributeError):
        safe_run_id = f"draft-{uuid.uuid4()}"
    root = Path(__file__).resolve().parents[1] / "artifacts/reference_generation" / safe_run_id
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"custom_music{_safe_media_suffix(file_name, mime_type)}"
    destination.write_bytes(music_bytes)
    return str(destination)


def render_reference_music_variant(
    video_bytes: bytes,
    *,
    music_path: str | None,
    music_volume: float = 0.0,
    keep_original_audio: bool = True,
    run_id: str = "",
    choice: str = "music",
    output_root: str | Path | None = None,
) -> str:
    if not video_bytes:
        raise ValueError("Нет утверждённого видео для добавления музыки")
    source_music = Path(str(music_path)) if music_path else None
    if source_music is not None and not source_music.is_file():
        raise FileNotFoundError(f"Музыкальный файл не найден: {source_music}")

    from .ai_approval_service import _resolve_ffmpeg
    from .post_production import (
        BACKGROUND_MUSIC_ATTENUATION_DB,
        FFMPEG_UPSCALE_TIMEOUT_SEC,
        TARGET_HEIGHT,
        TARGET_WIDTH,
        SOURCE_AUDIO_VOLUME,
        _apply_db_attenuation,
        _has_audio_stream,
        _media_duration,
        _run_ffmpeg,
        _upscale_master_to_4k,
        _upscaled_dimensions_for_render,
        _video_dimensions,
    )
    from .series_theme import holiday_music_filter

    safe_run_id = str(run_id or "").strip()
    try:
        safe_run_id = str(uuid.UUID(safe_run_id))
    except (TypeError, ValueError, AttributeError):
        safe_run_id = f"draft-{uuid.uuid4()}"
    root_base = (
        Path(output_root)
        if output_root is not None
        else Path(__file__).resolve().parents[1] / "artifacts/reference_generation"
    )
    root = root_base / safe_run_id
    root.mkdir(parents=True, exist_ok=True)
    clean_path = root / "approved_veo.mp4"
    clean_path.write_bytes(video_bytes)
    safe_choice = re.sub(r"[^a-z0-9_-]+", "_", str(choice or "music").casefold()).strip("_") or "music"
    processed_path = clean_path
    audio_path = root / f"approved_veo_{safe_choice}_audio.mp4"
    destination = root / f"approved_veo_{safe_choice}_upscaled_4k.mp4"
    ffmpeg = _resolve_ffmpeg()
    music_filter = holiday_music_filter(str(source_music)) if source_music is not None else ""
    has_source_audio = bool(keep_original_audio and _has_audio_stream(clean_path))
    effective_volume = (
        _apply_db_attenuation(float(music_volume), BACKGROUND_MUSIC_ATTENUATION_DB)
        if has_source_audio
        else float(music_volume)
    )

    if source_music is not None and has_source_audio:
        duration = max(0.0, float(_media_duration(clean_path) or 0.0))
        duration_filter = ""
        if duration > 0:
            duration_arg = f"{duration:.3f}"
            duration_filter = f",apad=whole_dur={duration_arg},atrim=duration={duration_arg},asetpts=N/SR/TB"
        filter_complex = (
            f"[0:a]volume={SOURCE_AUDIO_VOLUME:.4f}{duration_filter}[voice];"
            f"[1:a]{music_filter}volume={effective_volume:.4f}[music];"
            "[voice][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
        )
        command = [
            ffmpeg, "-y", "-i", str(clean_path), "-stream_loop", "-1", "-i", str(source_music),
            "-filter_complex", filter_complex, "-map", "0:v:0", "-map", "[a]", "-shortest",
            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(audio_path),
        ]
        _run_ffmpeg(command, timeout=FFMPEG_UPSCALE_TIMEOUT_SEC, stage="reference_music_mix")
        processed_path = audio_path
    elif source_music is not None:
        filter_complex = f"[1:a]{music_filter}volume={effective_volume:.4f}[music]"
        command = [
            ffmpeg, "-y", "-i", str(clean_path), "-stream_loop", "-1", "-i", str(source_music),
            "-filter_complex", filter_complex, "-map", "0:v:0", "-map", "[music]", "-shortest",
            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(audio_path),
        ]
        _run_ffmpeg(command, timeout=FFMPEG_UPSCALE_TIMEOUT_SEC, stage="reference_music_only")
        processed_path = audio_path
    elif not keep_original_audio:
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(clean_path),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            str(audio_path),
        ]
        _run_ffmpeg(command, timeout=FFMPEG_UPSCALE_TIMEOUT_SEC, stage="reference_audio_remove")
        processed_path = audio_path

    if not processed_path.is_file() or processed_path.stat().st_size <= 0:
        raise RuntimeError("Финальная аудиодорожка не создана")
    source_width, source_height = _video_dimensions(processed_path)
    target_width, target_height = _upscaled_dimensions_for_render(
        source_width or TARGET_WIDTH,
        source_height or TARGET_HEIGHT,
    )
    _upscale_master_to_4k(
        ffmpeg=ffmpeg,
        src_path=processed_path,
        dest_path=destination,
        target_width=target_width,
        target_height=target_height,
    )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("Финальный ролик после апскейла не создан")
    return str(destination)
