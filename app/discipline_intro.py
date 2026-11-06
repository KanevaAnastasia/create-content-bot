"""Approved two-speaker intro workflow for the Discipline project."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .ai_approval_service import _resolve_ffmpeg
from .db import SessionLocal
from .elevenlabs_tts import generate_elevenlabs_speech
from .llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, extract_json_object, gemini_generate_sync
from .models import Character, Project, ProjectCharacter
from .character_catalog import GRANDMA_KITCHEN_PROJECT_SLUG
from .constants import ELEVENLABS_VOICE_ID_ENV_NAME
from .project_profiles import DISCIPLINE_PROJECT_SLUG, DISCIPLINE_VOICE_ID
from .discipline_modes import MALE_VOICE_ID


STATE_WAITING_DISCIPLINE_INTRO_CHOICE = "waiting_discipline_intro_choice"
STATE_WAITING_DISCIPLINE_INTRO_DIALOGUE_APPROVAL = "waiting_discipline_intro_dialogue_approval"
STATE_WAITING_DISCIPLINE_INTRO_IMAGE_APPROVAL = "waiting_discipline_intro_image_approval"
STATE_WAITING_DISCIPLINE_INTRO_VIDEO_APPROVAL = "waiting_discipline_intro_video_approval"

DISCIPLINE_INTRO_ENABLED_SESSION_KEY = "discipline_intro_enabled"
DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY = "discipline_intro_dialogue"
DISCIPLINE_INTRO_DIALOGUE_VERSIONS_SESSION_KEY = "discipline_intro_dialogue_versions"
DISCIPLINE_INTRO_IMAGE_PATH_SESSION_KEY = "discipline_intro_image_path"
DISCIPLINE_INTRO_IMAGE_VERSIONS_SESSION_KEY = "discipline_intro_image_versions"
DISCIPLINE_INTRO_AUDIO_PATH_SESSION_KEY = "discipline_intro_audio_path"
DISCIPLINE_INTRO_VIDEO_PATH_SESSION_KEY = "discipline_intro_video_path"
DISCIPLINE_INTRO_VIDEO_VERSIONS_SESSION_KEY = "discipline_intro_video_versions"
DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY = "discipline_intro_approved_video_path"
DISCIPLINE_INTRO_APPROVED_PROVIDER_SESSION_KEY = "discipline_intro_approved_provider"

DISCIPLINE_INTRO_DURATION_SEC = 8
DISCIPLINE_INTRO_TURN_DURATION_SEC = 4
DISCIPLINE_VASILY_VOICE_ID = MALE_VOICE_ID
GRANDMA_GRANDSON_VOICE_ID_ENV_NAME = "ELEVENLABS_GRANDSON_VOICE_ID"
INTRO_PROJECT_SLUGS = {DISCIPLINE_PROJECT_SLUG, GRANDMA_KITCHEN_PROJECT_SLUG}


def is_discipline_project_session(session: dict[str, Any] | None) -> bool:
    return str((session or {}).get("selected_project_key") or "").strip() == DISCIPLINE_PROJECT_SLUG


def is_project_intro_session(session: dict[str, Any] | None) -> bool:
    return str((session or {}).get("selected_project_key") or "").strip() in INTRO_PROJECT_SLUGS


def _intro_project_slug(project_slug: str | None) -> str:
    slug = str(project_slug or "").strip()
    return slug if slug in INTRO_PROJECT_SLUGS else DISCIPLINE_PROJECT_SLUG


def _intro_cast(project_slug: str | None) -> tuple[str, str, str, str]:
    if _intro_project_slug(project_slug) == GRANDMA_KITCHEN_PROJECT_SLUG:
        return "grandson", "grandma", "Внук", "Бабушка"
    return "vasily", "anastasia", "Василий", "Анастасия"


def intro_character_names(project_slug: str | None) -> tuple[str, str]:
    _first_key, _second_key, first_name, second_name = _intro_cast(project_slug)
    return first_name, second_name


def parse_intro_dialogue_text(
    text: str,
    *,
    project_slug: str | None = DISCIPLINE_PROJECT_SLUG,
) -> dict[str, str] | None:
    """Return explicitly labelled user dialogue verbatim instead of rewriting it with an LLM."""
    first_key, second_key, first_name, second_name = _intro_cast(project_slug)
    source = str(text or "").strip()
    source = re.sub(r"^(?:исправь|добавь\s+в\s+промпт)\s*:\s*", "", source, flags=re.IGNORECASE)
    labels = {
        first_name.casefold(): first_key,
        second_name.casefold(): second_key,
    }
    parsed: dict[str, str] = {}
    for raw_line in source.splitlines():
        match = re.match(r"^\s*([^:]{1,40})\s*:\s*(.+?)\s*$", raw_line)
        if not match:
            continue
        key = labels.get(match.group(1).strip().casefold())
        if key:
            parsed[key] = _clean_line(match.group(2))
    if parsed.get(first_key) and parsed.get(second_key):
        return {first_key: parsed[first_key], second_key: parsed[second_key]}
    return None


def _clean_line(value: Any) -> str:
    return " ".join(str(value or "").strip().strip("\"'«»“”").split())


def generate_discipline_intro_dialogue(
    source_story: str,
    *,
    previous: dict[str, str] | None = None,
    feedback: str = "",
    project_slug: str | None = DISCIPLINE_PROJECT_SLUG,
) -> dict[str, str]:
    """Create the project-specific two-speaker exchange shown before a Story."""
    slug = _intro_project_slug(project_slug)
    first_key, second_key, first_name, second_name = _intro_cast(slug)
    if slug == GRANDMA_KITCHEN_PROJECT_SLUG:
        project_direction = """
Ты пишешь короткое вступление к ролику бабушкиного кулинарно-исторического проекта.
В ролике всегда ровно две реплики и только два говорящих:
1. Внук задаёт бабушке короткий живой вопрос, который открывает тему последующей истории.
2. Бабушка отвечает одной интригующей фразой; её ответ должен естественно и без повтора продолжаться первым предложением основной истории.

Внук не объясняет историю сам и не даёт совет. Бабушка не завершает тему в интро, а создаёт мостик к дальнейшему рассказу.
""".strip()
    else:
        project_direction = """
Ты пишешь короткое вступление к ролику проекта «Дисциплина».
В ролике всегда ровно две реплики и только два говорящих:
1. Василий кратко формулирует бытовую проблему с дисциплиной, прокрастинацией или незавершённым делом.
2. Анастасия спокойно даёт один конкретный принцип, который помогает начать и довести дело до конца.

Анастасия не делает всё за Василия: она помогает ему стать дисциплинированнее и завершить дело.
""".strip()
    prompt = f"""
{project_direction}

Обе реплики вместе должны естественно помещаться максимум в 8 секунд: ориентир 5–10 слов у {first_name} и 7–12 слов у {second_name}. Никакого закадрового текста, приветствий, пояснений и третьего говорящего. Реплики должны прямо иллюстрировать тему последующей истории.

Тема истории:
{str(source_story or '').strip()}

Предыдущий вариант:
{json.dumps(previous or {}, ensure_ascii=False)}

Правка пользователя:
{str(feedback or '').strip() or 'нет'}

Верни только JSON:
{{"{first_key}": "...", "{second_key}": "..."}}
""".strip()
    raw = gemini_generate_sync(prompt, model_profile=GEMINI_MODEL_PROFILE_CREATIVE)
    payload = extract_json_object(raw)
    first_line = _clean_line(payload.get(first_key))
    second_line = _clean_line(payload.get(second_key))
    if not first_line or not second_line:
        raise ValueError("Gemini не вернул обе реплики вступительного диалога.")
    if len(first_line.split()) > 16 or len(second_line.split()) > 18:
        raise ValueError("Вступительный диалог не помещается в восемь секунд.")
    return {first_key: first_line, second_key: second_line}


def format_discipline_intro_dialogue(
    dialogue: dict[str, str], *, project_slug: str | None = DISCIPLINE_PROJECT_SLUG
) -> str:
    first_key, second_key, first_name, second_name = _intro_cast(project_slug)
    return (
        "ДИАЛОГ ИНТРО · 8 СЕКУНД\n\n"
        f"{first_name}: «{_clean_line(dialogue.get(first_key))}»\n"
        f"{second_name}: «{_clean_line(dialogue.get(second_key))}»"
    )


def discipline_intro_video_prompt(
    dialogue: dict[str, str], *, project_slug: str | None = DISCIPLINE_PROJECT_SLUG
) -> str:
    first_key, second_key, first_name, second_name = _intro_cast(project_slug)
    first_line = _clean_line(dialogue.get(first_key))
    second_line = _clean_line(dialogue.get(second_key))
    if _intro_project_slug(project_slug) == GRANDMA_KITCHEN_PROJECT_SLUG:
        first_prompt_name, second_prompt_name = "Grandson", "Grandmother"
        first_focus, second_focus = "his face", "her face"
        identity_lock = "Preserve the grandmother, grandson, kitchen, lighting, props, clothing, proportions and identities."
        audio_direction = (
            "Generate clean native Russian dialogue audio with no music and no narrator. "
            "Voice lock GRANDSON: natural Russian-speaking boy around eight to ten years old, bright child timbre, "
            "medium-high pitch, clear articulation, never an adult male voice. "
            "Voice lock GRANDMOTHER: natural elderly Russian-speaking woman, warm calm wise timbre, unhurried but clear. "
        )
    else:
        first_prompt_name, second_prompt_name = "Vasily", "Anastasia"
        first_focus, second_focus = "his face", "her face"
        identity_lock = "Preserve Vasily, Anastasia, Knopka, the room, lighting, props, clothing, proportions and identities."
        audio_direction = "Generate no audio. "
    return (
        "Create one 8-second vertical 9:16 handcrafted clay stop-motion dialogue scene from the supplied approved "
        f"start frame. {identity_lock} "
        "No morphing, no extra characters, no captions, subtitles, speech bubbles or readable text. "
        f"{audio_direction}"
        f"0.0-4.0 seconds: {first_prompt_name} is the only speaker and clearly mouths the exact Russian sentence: “{first_line}” while "
        f"the camera makes a gentle smooth push-in toward {first_focus}. {second_prompt_name} remains silent and listens. "
        f"4.0-8.0 seconds: {second_prompt_name} is the only speaker and clearly mouths the exact Russian sentence: “{second_line}” while "
        f"the camera smoothly redirects and gently pushes in toward {second_focus}. {first_prompt_name} remains silent and listens. "
        "Keep the clay style and every character identity stable throughout."
    )


def _intro_character_rows(project_slug: str | None) -> list[tuple[Character, ProjectCharacter]]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(Character, ProjectCharacter)
                .join(ProjectCharacter, ProjectCharacter.character_id == Character.id)
                .join(Project, Project.id == ProjectCharacter.project_id)
                .where(Project.slug == _intro_project_slug(project_slug), Character.is_active.is_(True))
                .order_by(ProjectCharacter.sort_order, Character.created_at)
            ).all()
        )


def discipline_intro_character_references(
    project_slug: str | None = DISCIPLINE_PROJECT_SLUG,
) -> list[tuple[str, bytes]]:
    wanted = (
        {"бабушка": 0, "внук": 1}
        if _intro_project_slug(project_slug) == GRANDMA_KITCHEN_PROJECT_SLUG
        else {"василий": 0, "анастасия": 1, "кнопка": 2}
    )
    items: list[tuple[int, str, bytes]] = []
    for character, link in _intro_character_rows(project_slug):
        name = str(link.display_name or character.name or "").strip()
        order = wanted.get(name.casefold())
        uri = str(link.asset_uri or character.default_asset_uri or "").strip()
        path = Path(uri[7:] if uri.startswith("file://") else uri)
        if order is None or not path.is_file():
            continue
        items.append((order, name, path.read_bytes()))
    items.sort(key=lambda item: item[0])
    return [(name, payload) for _order, name, payload in items]


def discipline_intro_start_frame_prompt(
    dialogue: dict[str, str],
    *,
    feedback: str = "",
    project_slug: str | None = DISCIPLINE_PROJECT_SLUG,
) -> str:
    first_key, second_key, first_name, second_name = _intro_cast(project_slug)
    if _intro_project_slug(project_slug) == GRANDMA_KITCHEN_PROJECT_SLUG:
        scene_direction = (
            "Use the attached references in this exact identity order: grandmother, grandson. Preserve their faces, materials, colors, clothing and proportions exactly. "
            "Show the grandson beside the grandmother in the familiar cozy project kitchen. The grandson looks up with lively curiosity, just about to ask his question. "
            "The grandmother is attentive and warm, ready to answer and continue the historical story. Keep the established cartoon style of the character references. "
        )
    else:
        scene_direction = (
            "Use the attached references in this exact identity order: Vasily, Anastasia, Knopka. Preserve their faces, materials, colors, clothing and proportions exactly. "
            "Show Vasily seated at a cluttered work desk, overwhelmed by one unfinished task, with both hands near his hair. Anastasia stands beside him, attentive and calm. "
            "Knopka is clearly visible on the desk and remains a recurring character. Warm cozy evening room, practical desk lamp, "
        )
    return (
        "Create the approved opening frame for an 8-second vertical 9:16 handcrafted clay stop-motion scene. "
        + scene_direction
        + "expressive cinematic composition with enough space for a later camera push-in to each speaker. No text, letters, "
        "captions, speech bubbles, logos, watermarks or extra characters. "
        f"Dialogue context only: {first_name} says “{_clean_line(dialogue.get(first_key))}”; {second_name} answers "
        f"“{_clean_line(dialogue.get(second_key))}”. "
        + (f"User correction: {str(feedback).strip()}." if str(feedback).strip() else "")
    )


def discipline_intro_voice_turns(
    dialogue: dict[str, str], *, project_slug: str | None = DISCIPLINE_PROJECT_SLUG
) -> list[dict[str, str]]:
    slug = _intro_project_slug(project_slug)
    if slug == GRANDMA_KITCHEN_PROJECT_SLUG:
        grandma_voice = str(os.getenv(ELEVENLABS_VOICE_ID_ENV_NAME) or DISCIPLINE_VOICE_ID).strip()
        grandson_voice = str(os.getenv(GRANDMA_GRANDSON_VOICE_ID_ENV_NAME) or "loY1uopAz31XyhAEhNSa").strip()
        return [
            {"speaker": "Внук", "text": _clean_line(dialogue.get("grandson")), "voice_id": grandson_voice},
            {"speaker": "Бабушка", "text": _clean_line(dialogue.get("grandma")), "voice_id": grandma_voice},
        ]
    voices = {
        "василий": DISCIPLINE_VASILY_VOICE_ID,
        "анастасия": DISCIPLINE_VOICE_ID,
    }
    for character, link in _intro_character_rows(slug):
        name = str(link.display_name or character.name or "").strip().casefold()
        meta = character.meta_json if isinstance(character.meta_json, dict) else {}
        voice_id = str(meta.get("voice_id") or "").strip()
        if name in voices and name != "василий" and voice_id:
            voices[name] = voice_id
    return [
        {"speaker": "Василий", "text": _clean_line(dialogue.get("vasily")), "voice_id": voices["василий"]},
        {"speaker": "Анастасия", "text": _clean_line(dialogue.get("anastasia")), "voice_id": voices["анастасия"]},
    ]


def _artifact_dir(chat_id: int) -> Path:
    target = Path.cwd() / "artifacts" / "discipline_intro" / str(int(chat_id))
    target.mkdir(parents=True, exist_ok=True)
    return target


def store_discipline_intro_image(chat_id: int, image_bytes: bytes) -> str:
    path = _artifact_dir(chat_id) / f"start-{uuid.uuid4()}.png"
    path.write_bytes(image_bytes)
    return str(path.resolve())


def store_discipline_intro_video(chat_id: int, video_bytes: bytes, *, provider: str) -> str:
    path = _artifact_dir(chat_id) / f"{str(provider or 'video').strip().lower()}-native-{uuid.uuid4()}.mp4"
    path.write_bytes(video_bytes)
    return str(path.resolve())


def _media_duration(ffmpeg: str, path: Path) -> float:
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr or probe.stdout or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _atempo_chain(factor: float) -> str:
    remaining = max(0.01, float(factor or 1.0))
    factors: list[float] = []
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={item:.6f}" for item in factors)


def generate_discipline_intro_audio(
    chat_id: int,
    dialogue: dict[str, str],
    *,
    project_slug: str | None = DISCIPLINE_PROJECT_SLUG,
) -> str:
    """Create an exact 4s + 4s dialogue track with the assigned character voices."""
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    turns = discipline_intro_voice_turns(dialogue, project_slug=project_slug)
    with tempfile.TemporaryDirectory(prefix="discipline_intro_audio_") as tmp:
        tmp_path = Path(tmp)
        inputs: list[Path] = []
        filters: list[str] = []
        for index, turn in enumerate(turns):
            result = generate_elevenlabs_speech(
                turn["text"],
                voice_id=turn["voice_id"],
                language_code="ru",
            )
            source = tmp_path / f"turn_{index}.mp3"
            source.write_bytes(result.audio_bytes)
            inputs.append(source)
            duration = _media_duration(ffmpeg, source)
            speed = duration / 3.85 if duration > 3.85 else 1.0
            filters.append(
                f"[{index}:a]aresample=48000,{_atempo_chain(speed)},"
                f"apad=whole_dur={DISCIPLINE_INTRO_TURN_DURATION_SEC},"
                f"atrim=duration={DISCIPLINE_INTRO_TURN_DURATION_SEC},asetpts=N/SR/TB[a{index}]"
            )
        filters.append("[a0][a1]concat=n=2:v=0:a=1[outa]")
        target = _artifact_dir(chat_id) / f"dialogue-{uuid.uuid4()}.mp3"
        cmd = [ffmpeg, "-y"]
        for source in inputs:
            cmd.extend(["-i", str(source)])
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[outa]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(target),
            ]
        )
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return str(target.resolve())


def compose_discipline_intro(
    chat_id: int,
    *,
    video_bytes: bytes,
    audio_path: str,
    provider: str,
) -> str:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available")
    target_dir = _artifact_dir(chat_id)
    source_video = target_dir / f"{provider}-silent-{uuid.uuid4()}.mp4"
    source_video.write_bytes(video_bytes)
    target = target_dir / f"{provider}-dialogue-{uuid.uuid4()}.mp4"
    duration = float(_media_duration(ffmpeg, source_video) or DISCIPLINE_INTRO_DURATION_SEC)
    duration = min(duration, float(DISCIPLINE_INTRO_DURATION_SEC))
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source_video),
            "-i",
            str(audio_path),
            "-filter_complex",
            f"[1:a]aresample=48000,apad=whole_dur={duration:.6f},atrim=duration={duration:.6f},asetpts=N/SR/TB[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-t",
            f"{duration:.6f}",
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
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=240,
    )
    return str(target.resolve())
