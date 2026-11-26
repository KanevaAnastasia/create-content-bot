"""Manual, resumable scene production for Discipline's Inner Boss."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from telegram import ReplyKeyboardMarkup

from ..discipline_modes import BOSS, BOSS_VOICE_ID, MALE_VOICE_ID, SHORT_FORMATS
from ..inner_boss_scenes import (
    DIRECTIVE, PILOT_SCRIPT, SCRIPT_HEADER, TITLE_POSITION_HINT, TITLE_POSITION_LABEL, effect_events, frame_actions,
    parse_script, render_master, render_scene, speech_lead, title_position,
)

STATE = "waiting_inner_boss_scene_workflow"
KEY = "inner_boss_scene_workflow"
LOGGER = logging.getLogger(__name__)
FRAME_POLICY = 3
IMAGE_STAGES = {"image", "boss_image"}
SPEECH_TIMING_BUTTON = "Сдвинуть озвучку"
SPEECH_TIMING_HINT = "Укажи, когда должна начаться речь от начала сцены. Например: 4,4 или 00:04.4."
VIDEO_PROMPT_BUTTON = "Видеопромпт"
SCENE_EDIT_BUTTON = "Править сцену"
SCENE_EDIT_STAGES = IMAGE_STAGES | {"scene", "translation", "audio", "video_prompt", "video", "edit"}
SAVED_MUSIC_BUTTON = "Собрать с музыкой"
NO_TEXT = (
    "All artwork surfaces must be completely unmarked. No letters, numbers, digits, speech bubbles, "
    "captions, pictograms or watermarks. Clock screens and papers are solid blank shapes. "
    "Use polished flat 2D geometry, stable original character proportions and the dark navy palette. "
    "Render recognizable furniture and a few purposeful details in flat color planes."
)
STYLE_DIR = Path(__file__).resolve().parents[2] / "assets/story"
REFERENCE = STYLE_DIR / "inner_boss_character_reference.png"
VISUAL_REFERENCE = STYLE_DIR / "inner_boss_visual_reference.png"


def initialize(session: dict, script: str) -> None:
    scenes = parse_script(script)
    session[KEY] = {"id": uuid4().hex, "scenes": scenes, "index": 0, "stage": "script"}
    session["story_voiceover_text"] = script
    session["story_source_text"] = script
    session["recipe_full_text"] = script
    session["state"] = STATE


def keyboard(session):
    data = session[KEY]
    stage = data["stage"]
    if stage in SCENE_EDIT_STAGES and data.get("scene_edit_index") == data["index"]:
        return ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)
    if stage == "music":
        rows = [["Без музыки"], ["Показать сцену"]]
        if data.get("music_plan"):
            rows.insert(0, [SAVED_MUSIC_BUTTON])
    elif stage == "done":
        rows = [["Прислать ролик", "Добавить музыку"], ["Показать сцену"]]
    else:
        rows = [["ОК"], ["Показать сцену", "Сценарий"]]
        if stage in SCENE_EDIT_STAGES:
            rows.append([SCENE_EDIT_BUTTON])
        if stage in IMAGE_STAGES:
            rows.extend([["ИЗМЕНИТЬ"], ["ФИДБЕК"], ["ПЕРЕГЕНЕРИРОВАТЬ"]])
        elif stage == "video_prompt":
            rows.extend([["ПРОМПТ:", "ФИДБЕК"]])
            review = data.get("video_prompt_review") or {}
            if review.get("return_stage") or review.get("pending_input"):
                rows.append(["Отмена"])
        elif stage in {"audio", "video", "edit"}:
            rows.append(["Перегенерировать"])
            if stage in {"video", "edit"}:
                rows.append([VIDEO_PROMPT_BUTTON])
            if stage == "edit" and data["scenes"][data["index"]].get("audio"):
                rows.append([SPEECH_TIMING_BUTTON])
                if data.get("speech_timing_input_scene") == data["index"]:
                    rows.append(["Отмена"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def announce(message, session):
    data = session[KEY]
    blocks = [f"{scene['title']}\n{scene['body']}" for scene in data["scenes"]]
    chunk = session.get("story_title", "Внутренний босс") + "\n\n"
    for block in blocks:
        if len(chunk) + len(block) > 3500:
            await message.reply_text(chunk.rstrip())
            chunk = ""
        chunk += block + "\n\n"
    await message.reply_text(chunk.rstrip())
    await message.reply_text(
        "Проверь весь сценарий. «ОК» откроет первую сцену. Можно прислать новый сценарий целиком.",
        reply_markup=keyboard(session),
    )


def draft_script(idea: str) -> str:
    from ..llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, extract_json_object, gemini_generate_sync
    from ..writing_style import with_writing_style

    return str(extract_json_object(gemini_generate_sync(
        with_writing_style(
            "Верни JSON с полем script. Разбей идею на столько последовательных сцен, сколько требует действие. "
            "Все заголовки строго «Сцена N. Название · 7 секунд», подставь длительность от 3 до 15 секунд. "
            "Внутри сцены чередуй описания действия и отдельные реплики в порядке монтажа. "
            "Речь оформляй только Босс: «реплика» или Герой: «реплика». Не используй имена в диалоге. "
            "Звуковые эффекты пиши отдельной строкой «Звук:». Надписи без озвучки пиши строками «Титр:». "
            "Сохрани требования исходной идеи. Акцентируй конкретный поступок и его последствия. "
            "Не навязывай сюжет пилота или будильник. Музыки на этапе сцен нет.\nИдея: " + idea
        ), model_profile=GEMINI_MODEL_PROFILE_CREATIVE,
    )).get("script") or "")


def revise_scene(scene: dict, feedback: str) -> str:
    """Keep short text feedback from accidentally replacing the entire scene."""
    if SCRIPT_HEADER.match(feedback):
        return feedback
    directives = list(DIRECTIVE.finditer(feedback))
    if directives and not DIRECTIVE.sub("", feedback).strip():
        labels = {directive[1].casefold() for directive in directives}
        original = DIRECTIVE.sub(
            lambda match: "" if match[1].casefold() in labels else match[0],
            scene["body"],
        ).strip()
        return original + "\n" + feedback
    if len(feedback.splitlines()) > 1 and ("«" in feedback or "В кадре:" in feedback):
        return feedback
    from ..llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, extract_json_object, gemini_generate_sync
    from ..writing_style import with_writing_style

    data = {"current_scene": scene["title"] + "\n" + scene["body"], "feedback": feedback}
    raw = gemini_generate_sync(
        with_writing_style(
            'Верни JSON {"scene":"..."} с полным текстом только этой сцены. '
            "Примени только правку. Сохрани неизменённые реплики дословно, все действия, "
            "звуки и титры на своих местах. Оставь заголовок и длительность, если их не просят изменить. "
            "Реплики оформляй Босс: «...» и Герой: «...».\n" + json.dumps(data, ensure_ascii=False)
        ), model_profile=GEMINI_MODEL_PROFILE_CREATIVE,
    )
    replacement = str(extract_json_object(raw).get("scene") or "").strip()
    if not replacement:
        raise ValueError("Правка не получилась. Пришли полный текст сцены.")
    return replacement


def _translate(lines: list[str]) -> list[str]:
    from ..llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, extract_json_object, gemini_generate_sync

    if not lines:
        return []
    raw = gemini_generate_sync(
        "Translate each item faithfully into natural English, without shortening, omission or added speaker names. "
        'Return JSON {"lines": [...]} with the same item count and order.\n' + json.dumps(lines, ensure_ascii=False),
        model_profile=GEMINI_MODEL_PROFILE_CREATIVE,
    )
    translated = extract_json_object(raw).get("lines")
    if not isinstance(translated, list) or len(translated) != len(lines) or not all(
        isinstance(line, str) and line.strip() for line in translated
    ):
        raise ValueError("Перевод неполный. Повтори этот этап.")
    return [line.strip() for line in translated]


def _snapshot(session):
    """Expose the current script and scene indexes to the existing admin run."""
    data = session[KEY]
    session["recipe_full_text"] = "\n\n".join(f"{scene['title']}\n{scene['body']}" for scene in data["scenes"])
    session["selected_concept"] = session.get("story_title") or "Внутренний босс"
    session["image_scene_index"] = data["index"]
    session["scenes_plan"] = [
        {"scene": index, "text": scene["body"], "duration_sec": scene.get("video_duration") or scene["duration"],
         "generation_mode": "story", "discipline_narrative_mode": BOSS}
        for index, scene in enumerate(data["scenes"], 1)
    ]


def _save(update, session):
    from ..session_store import save_session

    if session[KEY]["stage"] != "script":
        _snapshot(session)
    save_session(int(update.effective_chat.id), session)


def _directory(data):
    root = Path.cwd() / "artifacts" / "inner_boss_scenes" / data["id"]
    root.mkdir(parents=True, exist_ok=True)
    return root


def _target(data, suffix):
    return _directory(data) / f"scene-{data['index'] + 1:02}-{uuid4().hex[:10]}{suffix}"


def _duration(scene):
    lead = speech_lead(scene)
    longest = max((asset["duration"] for asset in scene.get("audio", {}).values()), default=0)
    result = max(scene["duration"], math.ceil(lead + longest + 0.25))
    if result > 15:
        raise ValueError("Озвучка длиннее доступного клипа. Сократи реплики или раздели сцену.")
    return result


def _clean_opening(scene):
    return scene.get("image") and scene.get("frame_policy") == FRAME_POLICY


async def _audio(update, session, scene):
    from ..story_generation import generate_story_audio, generate_story_dialogue_audio, store_story_audio
    from ..story_alignment import valid_alignment

    data = session[KEY]
    languages = data["languages"]
    turns = scene["turns"]
    paths = scene.setdefault("audio", {})
    voice_ids = {"hero": MALE_VOICE_ID, "boss": os.getenv("DISCIPLINE_BOSS_VOICE_ID") or BOSS_VOICE_ID}
    for lang in languages:
        if lang in paths and Path(paths[lang]["path"]).is_file():
            continue
        if not turns:
            continue
        lines = [item["text"] for item in turns] if lang == "ru" else scene["en_turns"]
        inputs = [{"text": line, "voice_id": voice_ids[turn["speaker"]]} for turn, line in zip(turns, lines)]
        if len(inputs) == 1:
            result = await asyncio.to_thread(generate_story_audio, lines[0], language=lang,
                                             voice_id=inputs[0]["voice_id"])
        else:
            result = await asyncio.to_thread(generate_story_dialogue_audio, inputs, language=lang)
        if not valid_alignment(result.alignment) or result.duration_sec <= 0:
            raise ValueError(f"Озвучка {lang.upper()} пришла без точных таймингов.")
        path = await asyncio.to_thread(store_story_audio, int(update.effective_chat.id), result)
        paths[lang] = {"path": path, "duration": result.duration_sec, "alignment": result.alignment}
        if result.dialogue_turn_starts:
            paths[lang]["turn_starts"] = list(result.dialogue_turn_starts)
        _save(update, session)


async def _image(data, scene):
    from ..vertex_images import generate_image_vertex

    references = [REFERENCE.read_bytes(), VISUAL_REFERENCE.read_bytes()]
    if data["index"]:
        previous = data["scenes"][data["index"] - 1]
        previous_video = previous.get("video")
        if previous.get("approved") and previous_video and Path(previous_video).is_file():
            from ..video_frame_extraction import extract_last_video_frame

            try:
                references.append(await asyncio.to_thread(extract_last_video_frame, video_path=Path(previous_video)))
            except (OSError, RuntimeError):
                LOGGER.warning("inner_boss.previous_frame_unavailable")
    opening, appearance = frame_actions(scene)
    opening = scene.get("frame_prompt_overrides", {}).get("image", opening)
    prompt = (
        SHORT_FORMATS[BOSS].frame
        + "\nVertical 9:16 single opening image. Reference 1 is the exact character model. "
        "Reference 2 fixes the 2D graphic style, shape detail and night palette. Choose the layout and scenery from the scene. "
        "When returning to the pilot bedroom match the same bed, left-hand clock and upper-right window. "
        "If reference 3 is present it is the last approved frame: use it to match geography and object positions "
        "in the same location, and preserve the character model across a location change. Frame the initial pose. "
        + ("Show only the Hero. The wall must be completely empty where Boss will appear later. " if appearance else "")
        + NO_TEXT + "\n" + opening
    )
    result = await asyncio.to_thread(generate_image_vertex, prompt, reference_images=references, aspect_ratio="9:16")
    path = _target(data, ".png")
    path.write_bytes(result)
    scene["image"], scene["frame_policy"] = str(path), FRAME_POLICY
    scene.pop("boss_image", None)
    scene.pop("end_image", None)
    scene.pop("clock_position", None)


async def _boss_image(data, scene):
    from ..vertex_images import generate_image_vertex

    opening, appearance = frame_actions(scene)
    appearance_override = scene.get("frame_prompt_overrides", {}).get("boss_image")
    prompt = (
        "Edit the first reference as the exact same vertical 9:16 shot. Preserve lens, framing, perspective, "
        "all props and their screen positions, the Hero's identity, and the flat nighttime 2D style. "
        "Add the Boss wall shadow from the second reference. Keep him visibly separated from the Hero. "
        "Depict only the instant Boss has just appeared. " + NO_TEXT + "\n"
        + (appearance_override or re.split(r"(?<=[.!?])\s+", appearance, maxsplit=1)[0])
        + "\nAction leading into this instant. Advance the Hero to the final scripted pose: " + opening
    )
    result = await asyncio.to_thread(
        generate_image_vertex, prompt,
        reference_images=[Path(scene["image"]).read_bytes(), REFERENCE.read_bytes()], aspect_ratio="9:16",
    )
    path = _target(data, "-boss.png")
    path.write_bytes(result)
    scene["boss_image"] = str(path)


async def _revise_frame(data, scene, stage, feedback):
    from ..vertex_images import generate_image_vertex

    prompt = (
        "Edit reference image 1 in place. It is the exact approved composition and drawing style for this shot. "
        "Reference image 2 anchors the Hero and Boss designs. Preserve identities, the vertical framing, colors, "
        "lighting and prop positions except for changes explicitly requested below. "
        + ("Keep the Boss shadow present and visually separate from the Hero. " if stage == "boss_image"
           else "Preserve the characters already present in this still. ")
        + NO_TEXT + "\nRequested visual change: " + feedback
    )
    result = await asyncio.to_thread(
        generate_image_vertex, prompt,
        reference_images=[Path(scene[stage]).read_bytes(), REFERENCE.read_bytes()], aspect_ratio="9:16",
    )
    path = _target(data, "-boss-fix.png" if stage == "boss_image" else "-opening-fix.png")
    path.write_bytes(result)
    scene[stage] = str(path)
    _invalidate_frame_dependants(data, scene, stage)


def _invalidate_frame_dependants(data, scene, stage):
    if stage == "image":
        scene["frame_policy"] = FRAME_POLICY
        scene.pop("boss_image", None)
        scene.pop("end_image", None)
    for key in ("video", "video_duration", "previews", "approved"):
        scene.pop(key, None)
    data.pop("masters", None)


async def _rewrite_frame_prompt(data, scene, stage, feedback):
    from ..llm_utils import GEMINI_MODEL_PROFILE_EDIT, gemini_generate_sync

    opening, appearance = frame_actions(scene)
    current = scene.get("frame_prompt_overrides", {}).get(stage, appearance if stage == "boss_image" else opening)
    rewritten = await asyncio.to_thread(
        gemini_generate_sync,
        "Перепиши промпт только для одного опорного кадра «Внутреннего босса» с учётом фидбэка. "
        "Выведи один визуальный промпт. Сохрани композицию, реквизит, геометрическую 2D-рисовку и внешность героев. "
        "Текст, цифры и реплики добавляются на монтаже."
        f"\nТекущий промпт:\n{current}\nФидбэк:\n{feedback}",
        model_profile=GEMINI_MODEL_PROFILE_EDIT,
    )
    candidate = dict(
        scene, frame_prompt_overrides={
            **scene.get("frame_prompt_overrides", {}),
            stage: f"{rewritten.strip() or current}\nRequired visual correction: {feedback}",
        },
    )
    generator = _image if stage == "image" else _boss_image
    await generator(data, candidate)
    _invalidate_frame_dependants(data, candidate, stage)
    data["scenes"][data["index"]] = candidate


async def _handle_frame_feedback(update, session, text):
    data = session[KEY]
    stage, index = data["stage"], data["index"]
    if stage not in IMAGE_STAGES:
        return False
    from .handlers_media_scene import _extract_format_or_feedback
    from .handlers_state_media import (
        SCENE_IMAGE_EDIT_PENDING_KEY, _ask_for_feedback_text, _extract_image_edit_instruction,
        _is_empty_feedback_command, _pop_scene_image_edit_pending, _set_scene_image_edit_pending,
    )

    low = text.strip().casefold()
    pending = data.get(SCENE_IMAGE_EDIT_PENDING_KEY)
    if data.pop("awaiting_image_feedback", None):
        _set_scene_image_edit_pending(data, scene_idx=index, target=stage)
        pending = data[SCENE_IMAGE_EDIT_PENDING_KEY]
    if pending and pending["target"] == stage and pending["scene_idx"] == index:
        if low == "отмена":
            _pop_scene_image_edit_pending(data, scene_idx=index)
            _save(update, session)
            await _show(update, session)
            return True
        mode = pending.get("mode", "edit")
        instruction = _extract_format_or_feedback(text)
        if instruction is None:
            instruction = _extract_image_edit_instruction(text)
        if instruction is None:
            instruction = text.strip()
    elif low == "отмена":
        await _show(update, session)
        return True
    elif low in {"ок", "ok", "перегенерировать", "показать сцену", "сценарий", "прислать ролик"} or re.fullmatch(r"сцена\s+\d+", low):
        return False
    else:
        instruction = _extract_image_edit_instruction(text)
        if low == "исправить изображение":
            instruction = ""
        mode = "edit" if instruction is not None else "prompt_feedback"
        if instruction is None:
            instruction = _extract_format_or_feedback(text)
        if instruction is None:
            instruction = "" if _is_empty_feedback_command(text) or low == "фидбэк" else text.strip()
    if not instruction or (pending and (
        low in {"ок", "ok", "перегенерировать", "показать сцену", "сценарий", "фидбэк"}
        or _is_empty_feedback_command(text) or re.fullmatch(r"сцена\s+\d+", low)
    )):
        _set_scene_image_edit_pending(data, scene_idx=index, target=stage, mode=mode)
        _save(update, session)
        if mode == "edit":
            await update.message.reply_text("Напишите, что именно поменять в текущем изображении. Можно одной фразой.")
        else:
            await _ask_for_feedback_text(update, "кадре")
        return True
    scene = data["scenes"][index]
    if mode == "edit":
        await update.message.reply_text("Изменяю текущий кадр по вашему описанию.")
        await _revise_frame(data, scene, stage, instruction)
    else:
        await update.message.reply_text("Перегенерирую промпт и кадр по фидбеку.")
        await _rewrite_frame_prompt(data, scene, stage, instruction)
    _pop_scene_image_edit_pending(data, scene_idx=index)
    _save(update, session)
    await _show(update, session)
    return True


def _video_dialogue_direction(scene):
    turns = scene["turns"]
    speakers = {turn["speaker"] for turn in turns}
    if not speakers:
        return "No one speaks. Keep mouths still throughout the shot."
    roles = {"boss": "Boss, the red-edged shadow", "hero": "Hero, the human character"}
    dialogue = "\n".join(
        f"{index}. {roles[turn['speaker']]} speaks: {turn['text']}"
        for index, turn in enumerate(turns, 1)
    )
    if speakers == {"boss"}:
        direction = "Only Boss speaks. The Hero keeps his lips closed throughout the shot and reacts with his eyes or posture."
    elif speakers == {"hero"}:
        direction = "Only the Hero speaks. If Boss is visible, his mouth stays still throughout the shot."
    else:
        direction = (
            "Follow the dialogue in order. Only the current speaker moves their mouth. "
            "When Boss speaks, animate the shadow's mouth while Hero listens with his lips closed. "
            "When Hero speaks, animate Hero's mouth while Boss stays quiet with his mouth still."
        )
    return f"{direction}\n{dialogue}"


def _video_prompt(scene, duration, opening, appearance):
    override = str(scene.get("video_prompt_override") or "").strip()
    performance_cue = _video_dialogue_direction(scene)
    if override:
        if not scene["turns"] or performance_cue in override:
            return override
        return f"{override}\n{performance_cue}"
    visible_cues = {
        "alarm": "the alarm clock visibly vibrates",
        "click": "the Hero presses the clock button",
    }
    cues = sorted(
        (at, visible_cues[cue]) for cue, at, _ in effect_events(scene, duration, 0.0)
        if cue == "alarm" or (cue == "click" and "alarm" in scene["fx"] and cue in scene.get("fx_times", {}))
    )
    cue_notes = "\n".join(f"At {at:g}s: {action}." for at, action in cues)
    entrance = "Boss enters at the scripted moment and is visible before his first line." if appearance else ""
    return "\n".join(filter(None, (
        "Keep the framing and characters consistent with the supplied artwork. Do not add overlay text.",
        " ".join(filter(None, (opening, appearance))), entrance, performance_cue, cue_notes,
    )))


def _video_prompt_for_review(scene):
    from .handlers_media_core import enforce_video_prompt_visual_source_rule
    from ..syntax_telethon_fallback import _prepare_prompt_for_syntax, _syntax_prompt_max_chars

    text = enforce_video_prompt_visual_source_rule(_video_prompt(scene, _duration(scene), *frame_actions(scene)))
    limit = _syntax_prompt_max_chars()
    if not text or len(text) > limit:
        raise ValueError(f"Пришли текст видеопромпта длиной до {limit} символов.")
    prepared = _prepare_prompt_for_syntax(text, max_len=limit + 32)
    if prepared is None or len(prepared[0]) > limit:
        raise ValueError(f"Пришли текст видеопромпта длиной до {limit} символов.")
    return prepared[0]


def _open_video_prompt(data, scene, *, return_stage=None):
    text = _video_prompt_for_review(scene)
    data["video_prompt_review"] = {"index": data["index"], "text": text, "return_stage": return_stage}
    data["stage"] = "video_prompt"


async def _rewrite_video_prompt(scene, current, feedback):
    from ..llm_utils import GEMINI_MODEL_PROFILE_EDIT, gemini_generate_sync

    result = await asyncio.to_thread(
        gemini_generate_sync,
        "Перепиши промпт движения только этого кадра по замечанию пользователя. "
        "Сохрани действие, таймкоды, ракурс и роли говорящих из сценария, кроме явно изменённых деталей движения. "
        "Не добавляй реплики, технастройки или текст в кадре. Верни только готовый видеопромпт.\n"
        f"Текущий промпт:\n{current}\nПравка:\n{feedback}",
        model_profile=GEMINI_MODEL_PROFILE_EDIT,
    )
    return str(result or "").strip()


async def _video(update, data, scene, *, approved_prompt=None):
    from .handlers_media_core import _generate_video_bytes_with_high_load_retry
    from ..syntax_telethon_fallback import SYNTAX_PROVIDER_KLING

    duration = _duration(scene)
    opening, appearance = frame_actions(scene)
    start_image = Path(scene["image"]).read_bytes()
    if appearance and not scene.get("boss_image"):
        raise ValueError("Для Kling Keyframes нужны два опорных кадра. Сначала открой и утверди конечный кадр.")
    end_image = Path(scene["boss_image"]).read_bytes() if appearance else None
    prompt = approved_prompt if approved_prompt is not None else _video_prompt(scene, duration, opening, appearance)
    raw = await _generate_video_bytes_with_high_load_retry(
        update, prompt=prompt, model="kling_3_0", duration_sec=duration,
        start_image=start_image, end_image=end_image,
        quality="1080", aspect_ratio="9:16", audio_enabled=False, syntax_provider=SYNTAX_PROVIDER_KLING,
        **({"kling_keyframes": True} if appearance else {}),
    )
    path = _target(data, ".mp4")
    path.write_bytes(raw)
    scene["video"] = str(path)


async def _edit(data, scene):
    previews = {}
    for language in data["languages"]:
        path = _target(data, f"-edit-{language}.mp4")
        await asyncio.to_thread(render_scene, scene, path, language=language, duration=_duration(scene))
        previews[language] = str(path)
    scene["previews"] = previews


def _speech_timestamp(text):
    parts = text.strip().replace(",", ".").split(":")
    if (len(parts) > 3 or not all(re.fullmatch(r"[0-9]{1,3}", part) for part in parts[:-1])
            or not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3})?", parts[-1])):
        raise ValueError(SPEECH_TIMING_HINT)
    numbers = [float(part) for part in parts]
    if any(number >= 60 for number in numbers[1:]):
        raise ValueError(SPEECH_TIMING_HINT)
    return sum(number * 60 ** index for index, number in enumerate(reversed(numbers)))


async def _handle_speech_timing(update, session, text):
    data = session[KEY]
    if data["stage"] != "edit":
        return False
    low = text.strip().casefold()
    command = re.fullmatch(r"(?:сдвинуть\s+)?озвучк[ау](?:\s*:\s*|\s+)(.+)", low)
    awaiting = data.get("speech_timing_input_scene") == data["index"]
    if not (low == SPEECH_TIMING_BUTTON.casefold() or command or awaiting):
        return False
    scene = data["scenes"][data["index"]]
    if not scene.get("audio"):
        raise ValueError("В этой сцене нет озвучки.")
    if low == SPEECH_TIMING_BUTTON.casefold():
        data["speech_timing_input_scene"] = data["index"]
        _save(update, session)
        await update.message.reply_text(SPEECH_TIMING_HINT, reply_markup=keyboard(session))
        return True
    if awaiting and low == "отмена":
        data.pop("speech_timing_input_scene", None)
    else:
        start = _speech_timestamp(command[1] if command else low)
        clip_duration = scene.get("video_duration") or _duration(scene)
        if start >= clip_duration:
            raise ValueError(f"Выбери время внутри исходного клипа, до {clip_duration:g} с.")
        revised = dict(scene, speech_start_sec=start)
        await update.message.reply_text("Пересвожу озвучку и субтитры этой сцены.")
        await _edit(data, revised)
        revised.pop("approved", None)
        data["scenes"][data["index"]] = revised
        data.pop("masters", None)
        data.pop("speech_timing_input_scene", None)
    _save(update, session)
    await _show(update, session)
    return True


def _replace_scene(data, body):
    index = data["index"]
    previous = data["scenes"][index]
    header = re.sub(r"(?i)сцена\s+\d+", "Сцена 1", previous["title"], count=1)
    supplied = SCRIPT_HEADER.match(body)
    if supplied:
        header = re.sub(r"(?i)сцена\s+\d+", "Сцена 1", supplied[0], count=1)
        body = body[supplied.end():].strip()
    replacement, = parse_script(f"{header}\n{body}")
    replacement["title"] = re.sub(r"(?i)сцена\s+1\b", f"Сцена {index + 1}", replacement["title"], count=1)
    if replacement["turns"] == previous["turns"]:
        for key in ("audio", "en_turns"):
            if key in previous:
                replacement[key] = previous[key]
    if frame_actions(replacement) == frame_actions(previous) and previous.get("image"):
        for key in ("image", "frame_policy", "boss_image", "clock_position", "frame_prompt_overrides"):
            if key in previous:
                replacement[key] = previous[key]
        if (replacement["turns"] == previous["turns"] and _duration(replacement) == _duration(previous)
                and previous.get("video")):
            replacement["video"] = previous["video"]
            replacement["video_duration"] = previous.get("video_duration", _duration(previous))
            for key in ("speech_not_before_sec", "speech_start_sec"):
                if key in previous:
                    replacement[key] = previous[key]
    data["scenes"][index] = replacement
    data.pop("masters", None)
    data.pop("video_prompt_review", None)
    data["stage"] = "scene"


async def _handle_title_position(update, session, text):
    data = session[KEY]
    directive = DIRECTIVE.fullmatch(text.strip())
    if data["stage"] != "edit" or not directive or directive[1].casefold() != TITLE_POSITION_LABEL:
        return False
    index = data["index"]
    scene = data["scenes"][index]
    if scene.get("title_style") != "2":
        raise ValueError("В этой сцене нет титра в жёлтом стиле 2.")
    position = title_position(directive[2], _duration(scene))
    revised = dict(scene, body=revise_scene(scene, text), title_position=position)
    await _edit(data, revised)
    revised.pop("approved", None)
    data["scenes"][index] = revised
    data.pop("masters", None)
    _save(update, session)
    await _show(update, session)
    return True


async def _handle_scene_edit(update, session, text):
    data = session[KEY]
    stage, index = data["stage"], data["index"]
    if stage not in SCENE_EDIT_STAGES:
        return False
    command = text.strip().casefold()
    if command == SCENE_EDIT_BUTTON.casefold():
        data["scene_edit_index"] = index
    elif data.get("scene_edit_index") != index:
        return False
    elif command == "отмена":
        data.pop("scene_edit_index", None)
    elif command not in {"ок", "ok", "показать сцену", "сценарий", "перегенерировать"}:
        scene = data["scenes"][index]
        revised = await asyncio.to_thread(revise_scene, scene, text)
        _replace_scene(data, revised)
        data.pop("scene_edit_index", None)
    _save(update, session)
    await _show(update, session)
    return True


async def _handle_video_prompt_review(update, session, text):
    from .handlers_media_scene import _extract_format_or_feedback, _extract_prompt_override

    data = session[KEY]
    stage = data["stage"]
    low = text.strip().casefold()
    scene = data["scenes"][data["index"]]
    navigation = low in {"показать сцену", "сценарий", "прислать ролик"} or re.fullmatch(r"сцена\s+\d+", low)
    script_edit = SCRIPT_HEADER.match(text) or DIRECTIVE.search(text)
    video_feedback = (
        stage == "video" and not navigation and not script_edit
        and low not in {"ок", "ok", "отмена", "перегенерировать", VIDEO_PROMPT_BUTTON.casefold()}
    )
    if stage in {"video", "edit"} and (low == VIDEO_PROMPT_BUTTON.casefold() or stage == "video" and low == "перегенерировать"):
        _open_video_prompt(data, scene, return_stage=stage)
    elif navigation or (stage != "video_prompt" and not video_feedback):
        return False
    else:
        review = ({"return_stage": stage, "pending_input": "feedback"} if video_feedback
                  else data.get("video_prompt_review") or {})
        if low in {"ок", "ok"}:
            if review.get("pending_input"):
                await update.message.reply_text("Пришли правку или нажми «Отмена».", reply_markup=keyboard(session))
                return True
            current = _video_prompt_for_review(scene)
            if review.get("index") != data["index"] or current != review.get("text"):
                _open_video_prompt(data, scene, return_stage=review.get("return_stage"))
            else:
                await _video(update, data, scene, approved_prompt=review["text"])
                scene["video_duration"] = _duration(scene)
                scene.pop("previews", None)
                scene.pop("approved", None)
                data.pop("masters", None)
                data.pop("video_prompt_review", None)
                data["stage"] = "video"
        elif low == "отмена":
            if review.get("pending_input"):
                review.pop("pending_input", None)
            elif review.get("return_stage"):
                data["stage"] = review["return_stage"]
                data.pop("video_prompt_review", None)
        elif low in {"перегенерировать", VIDEO_PROMPT_BUTTON.casefold()}:
            pass
        else:
            replacement = _extract_prompt_override(text)
            feedback = _extract_format_or_feedback(text)
            if low.rstrip(":") in {"фидбек", "фидбэк", "feedback"}:
                if video_feedback:
                    _open_video_prompt(data, scene, return_stage=stage)
                    review = data["video_prompt_review"]
                review["pending_input"] = "feedback"
                _save(update, session)
                await update.message.reply_text("Напиши, что изменить в видеопромпте.", reply_markup=keyboard(session))
                return True
            if replacement == "" or low in {"промпт", "промт"}:
                if video_feedback:
                    _open_video_prompt(data, scene, return_stage=stage)
                    review = data["video_prompt_review"]
                review["pending_input"] = "replace"
                _save(update, session)
                await update.message.reply_text("Пришли полный текст видеопромпта.", reply_markup=keyboard(session))
                return True
            if feedback is not None or replacement is None and review.get("pending_input") == "feedback":
                replacement = await _rewrite_video_prompt(scene, review.get("text") or _video_prompt_for_review(scene),
                                                          feedback or text.strip())
            replacement = text.strip() if replacement is None else replacement
            if not replacement:
                raise ValueError("Видеопромпт пустой. Пришли текст или комментарий.")
            revised = dict(scene, video_prompt_override=replacement)
            _open_video_prompt(data, revised, return_stage=review.get("return_stage"))
            scene["video_prompt_override"] = replacement
    _save(update, session)
    await _show(update, session)
    return True


async def _show(update, session):
    data = session[KEY]
    stage = data["stage"]
    if stage == "script":
        await announce(update.message, session)
        return
    if stage == "music":
        note = "Все сцены утверждены. Пришли музыку аудиофайлом или выбери «Без музыки»."
        if plan := data.get("music_plan"):
            note = f"Все сцены утверждены. Первый трек начнётся со сцены 1, второй со сцены {plan['second_scene']}. Выбери «Собрать с музыкой»."
        await update.message.reply_text(note, reply_markup=keyboard(session))
        return
    if stage == "done":
        await _send_masters(update, session)
        return
    scene = data["scenes"][data["index"]]
    if data.get("scene_edit_index") == data["index"]:
        await update.message.reply_text(f"{scene['title']}\n\n{scene['body']}")
        await update.message.reply_text(
            "Пришли новый сценарий этой сцены или напиши, что изменить. «Отмена» вернёт к прежнему шагу.",
            reply_markup=keyboard(session),
        )
        return
    if stage == "scene":
        await update.message.reply_text(f"{scene['title']}\n\n{scene['body']}")
        note = ("«ОК» продолжит сцену. Для правки пришли её новый текст. "
                "«Сцена N» откроет другой кадр. В строке звука можно указать секунду, например «гром@1.2».")
    elif stage == "translation":
        rows = [f"{'Boss' if turn['speaker'] == 'boss' else 'Hero'}: {line}"
                for turn, line in zip(scene["turns"], scene.get("en_turns", []))]
        note = "Озвучка EN\n" + "\n".join(rows) + "\n\n«ОК» создаст озвучку. Можно прислать исправленный перевод с одной репликой на строку."
    elif stage == "audio":
        for lang, asset in scene.get("audio", {}).items():
            with Path(asset["path"]).open("rb") as file:
                await update.message.reply_audio(audio=file, caption=f"{scene['title']} · {lang.upper()}")
        note = "Проверь речь. «ОК» создаст стартовый кадр."
    elif stage == "image":
        with Path(scene["image"]).open("rb") as file:
            await update.message.reply_photo(photo=file, caption=scene["title"])
        if not _clean_opening(scene):
            note = "Этот кадр был создан по старому промту. Нажми «Перегенерировать», чтобы сделать чистый."
        elif frame_actions(scene)[1]:
            note = "Кадр до появления Босса. «ОК» откроет отдельный кадр с Боссом."
        else:
            note = "Все персонажи уже в кадре. «ОК» покажет видеопромпт или сохранённый клип."
    elif stage == "boss_image":
        with Path(scene["boss_image"]).open("rb") as file:
            await update.message.reply_photo(photo=file, caption=f"{scene['title']} · Босс появился")
        note = "«ОК» покажет видеопромпт или сохранённый клип. Оба кадра передадутся в Kling Keyframes."
    elif stage == "video_prompt":
        review = data.get("video_prompt_review") or {}
        prompt = review.get("text") or _video_prompt_for_review(scene)
        message = f"{scene['title']} · видеопромпт\n\n{prompt}"
        for offset in range(0, len(message), 3800):
            await update.message.reply_text(message[offset:offset + 3800])
        note = "«ОК» запустит Kling с этим текстом. «ПРОМПТ:» заменит текст целиком. «ФИДБЕК» позволит внести правку."
    elif stage == "video":
        with Path(scene["video"]).open("rb") as file:
            await update.message.reply_video(video=file, caption=scene["title"])
        note = ("«ОК» сведёт речь, звуки и титры этой сцены. "
                "Если нужно поправить движение или камеру, напиши правку здесь или нажми «Видеопромпт».")
    else:
        for lang, preview in scene["previews"].items():
            with Path(preview).open("rb") as file:
                await update.message.reply_video(video=file, caption=f"{scene['title']} · монтаж {lang.upper()}")
        note = "«ОК» закрепит монтаж и откроет следующую сцену. Для правки пришли новый текст сцены."
        if scene.get("title_style") == "2":
            note += "\n" + TITLE_POSITION_HINT
        if scene.get("audio"):
            note += "\n«Сдвинуть озвучку» задаёт время начала речи в этой сцене."
            if scene.get("speech_start_sec") is not None:
                start = f"{scene['speech_start_sec']:g}".replace(".", ",")
                note += f" Сейчас речь начинается на {start} с."
    await update.message.reply_text(note, reply_markup=keyboard(session))


async def handle_text(update, session, text):
    data = session[KEY]
    stage = data["stage"]
    low = text.strip().casefold()
    scene = data["scenes"][data["index"]]
    try:
        if await _handle_scene_edit(update, session, text):
            return
        if stage == "end_image":
            data["stage"] = "image"
            _save(update, session)
            await _show(update, session)
            return
        if await _handle_speech_timing(update, session, text):
            return
        if await _handle_title_position(update, session, text):
            return
        if await _handle_video_prompt_review(update, session, text):
            return
        if await _handle_frame_feedback(update, session, text):
            return
        if low == "сценарий":
            for item in data["scenes"]:
                await update.message.reply_text(f"{item['title']}\n\n{item['body']}")
            return
        jump = re.fullmatch(r"сцена\s+(\d+)", low)
        if jump and stage != "script":
            index = int(jump[1]) - 1
            if not 0 <= index < len(data["scenes"]):
                raise ValueError("Такой сцены нет.")
            next_pending = next((i for i, item in enumerate(data["scenes"]) if not item.get("approved")), len(data["scenes"]))
            if index > next_pending:
                raise ValueError("Сначала утверди монтаж предыдущих сцен.")
            data["index"], data["stage"] = index, "scene"
            data.pop("video_prompt_review", None)
        elif low == "показать сцену" and stage in {"music", "done"}:
            data["stage"] = "scene"
        elif low == "добавить музыку" and stage == "done":
            data["stage"] = "music"
        elif low in {"показать сцену", "прислать ролик"}:
            pass
        elif low == "без музыки" and stage == "music":
            await finish(update, session, None)
            return
        elif low == SAVED_MUSIC_BUTTON.casefold() and stage == "music" and data.get("music_plan"):
            await finish(update, session, None, use_saved_music=True)
            return
        elif low in {"ок", "ok", "перегенерировать"}:
            if stage in {"music", "done"}:
                await _show(update, session)
                return
            if low == "перегенерировать":
                if stage == "audio":
                    previous_audio = scene.pop("audio", None)
                    try:
                        await _audio(update, session, scene)
                    except Exception:
                        scene["audio"] = {**(previous_audio or {}), **scene.get("audio", {})}
                        _save(update, session)
                        raise
                    if _duration(scene) != scene.get("video_duration"):
                        scene.pop("video", None)
                elif stage == "image":
                    await _image(data, scene)
                    scene.pop("video", None)
                elif stage == "boss_image":
                    await _boss_image(data, scene)
                    scene.pop("video", None)
                elif stage == "edit":
                    await _edit(data, scene)
                else:
                    raise ValueError("На этом шаге пришли правку текстом.")
                scene.pop("approved", None)
                data.pop("masters", None)
                if stage != "edit":
                    scene.pop("previews", None)
            elif stage == "script":
                data["languages"] = ["ru", "en"] if session.get("story_english_requested", True) else ["ru"]
                data["stage"] = "scene"
            elif stage == "scene":
                if "en" in data["languages"] and (scene["turns"] and not scene.get("en_turns")
                                                  or scene["titles"]["ru"] and not scene["titles"]["en"]):
                    if scene["turns"] and not scene.get("en_turns"):
                        scene["en_turns"] = await asyncio.to_thread(_translate, [turn["text"] for turn in scene["turns"]])
                    if scene["titles"]["ru"] and not scene["titles"]["en"]:
                        scene["titles"]["en"] = await asyncio.to_thread(_translate, scene["titles"]["ru"])
                    if scene["turns"]:
                        data["stage"] = "translation"
                if data["stage"] == "scene":
                    if scene["turns"]:
                        await _audio(update, session, scene)
                        data["stage"] = "audio"
                    else:
                        if not _clean_opening(scene):
                            await _image(data, scene)
                        data["stage"] = "image"
            elif stage == "translation":
                await _audio(update, session, scene)
                data["stage"] = "audio"
            elif stage == "audio":
                _duration(scene)
                if not _clean_opening(scene):
                    await _image(data, scene)
                data["stage"] = "image"
            elif stage in IMAGE_STAGES:
                if not _clean_opening(scene):
                    raise ValueError("Этот кадр был сделан по старому промту. Сначала нажми «Перегенерировать».")
                if stage == "image" and frame_actions(scene)[1]:
                    if not scene.get("boss_image"):
                        await _boss_image(data, scene)
                    data["stage"] = "boss_image"
                else:
                    if scene.get("video"):
                        data["stage"] = "video"
                    else:
                        _open_video_prompt(data, scene)
            elif stage == "video":
                if not scene.get("previews"):
                    await _edit(data, scene)
                data["stage"] = "edit"
            elif stage == "edit":
                scene["approved"] = True
                pending = next((i for i, item in enumerate(data["scenes"]) if not item.get("approved")), None)
                if pending is None:
                    data["stage"] = "music"
                else:
                    data["index"], data["stage"] = pending, "scene"
        elif stage == "script":
            initialize(session, text)
        elif stage == "translation" and not SCRIPT_HEADER.match(text):
            lines = [re.sub(r"(?i)^\s*(?:hero|boss)\s*:\s*", "", line).strip().strip('«»"')
                     for line in text.splitlines() if line.strip()]
            if len(lines) != len(scene["turns"]) or not all(lines):
                raise ValueError("Пришли столько же реплик, по одной на строку.")
            scene["en_turns"] = lines
        elif stage in {"scene", "translation", "audio", "image", "boss_image", "video", "edit"}:
            _replace_scene(data, await asyncio.to_thread(revise_scene, scene, text))
        else:
            raise ValueError("На этом шаге пришли музыку файлом или открой сцену по номеру.")
        _save(update, session)
        await _show(update, session)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=keyboard(session))
    except Exception:
        LOGGER.exception("inner_boss.scene_stage_failed stage=%s", stage)
        note = (
            "Ролик собран и сохранён. Нажми «Прислать ролик», чтобы повторить отправку."
            if session[KEY]["stage"] == "done"
            else "Этап не завершился. Готовые материалы сохранены. Можно повторить этот шаг."
        )
        await update.message.reply_text(note, reply_markup=keyboard(session))


async def _send_masters(update, session):
    from ..final_video_delivery import _prepare_telegram_delivery_file
    from .handlers_state_media import _safe_reply_video

    data = session[KEY]
    for lang, path in data["masters"].items():
        source = Path(path)
        workflow_id = UUID(data["id"])
        delivery, _ = await asyncio.to_thread(
            _prepare_telegram_delivery_file, source, run_uuid=workflow_id,
            artifact_uuid=uuid5(workflow_id, source.name),
        )
        with delivery.open("rb") as file:
            await _safe_reply_video(update.message, video=file,
                                    caption=f"{session.get('story_title') or 'Внутренний босс'} · {lang.upper()}")
    await update.message.reply_text("Готовые версии сохранены в админке.", reply_markup=keyboard(session))


async def finish(update, session, music_bytes, *, use_saved_music=False):
    from ..artifact_store import persist_post_production_artifacts
    from ..generation_store import ensure_generation_run_for_session, mark_generation_run_ready
    from ..post_production import _has_audio_stream

    data = session[KEY]
    if not all(scene.get("approved") for scene in data["scenes"]):
        raise ValueError("Сначала утверди монтаж каждой сцены.")
    target = _directory(data)
    music = None
    transition = None
    if use_saved_music:
        plan = data["music_plan"]
        music, second = Path(plan["first"]), Path(plan["second"])
        transition = (plan["second_scene"] - 1, second)
        for path in (music, second):
            if not path.is_file() or not await asyncio.to_thread(_has_audio_stream, path):
                raise ValueError("Один из сохранённых треков недоступен или не распознан как аудио.")
    elif music_bytes:
        music = target / f"music-{uuid4().hex}.audio"
        music.write_bytes(music_bytes)
        if not await asyncio.to_thread(_has_audio_stream, music):
            raise ValueError("Этот файл не распознан как аудио.")
    batch = uuid4().hex[:10]
    masters = {}
    for lang in data["languages"]:
        destination = target / f"final-{batch}-{lang}.mp4"
        clips = [Path(scene["previews"][lang]) for scene in data["scenes"]]
        await asyncio.to_thread(render_master, clips, music, destination, music_transition=transition)
        masters[lang] = str(destination)
    _snapshot(session)
    run_id, _ = await asyncio.to_thread(ensure_generation_run_for_session, session)
    if not run_id:
        raise RuntimeError("Could not record the Inner Boss run in the admin.")
    await asyncio.to_thread(persist_post_production_artifacts, session, {
        "artifact_bundle_key": data["id"],
        "renders_by_language": {
            lang: {"titled_subtitled_music": {"video_path": path, "width": 1080, "height": 1920}}
            for lang, path in masters.items()
        },
    })
    data["masters"], data["stage"] = masters, "done"
    _save(update, session)
    await asyncio.to_thread(mark_generation_run_ready, session)
    await _send_masters(update, session)
