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
    APPEARANCE_LEAD, DIRECTIVE, PILOT_SCRIPT, SCRIPT_HEADER, frame_actions, join_appearance,
    parse_script, render_master, render_scene, speech_lead,
)

STATE = "waiting_inner_boss_scene_workflow"
KEY = "inner_boss_scene_workflow"
LOGGER = logging.getLogger(__name__)
FRAME_POLICY = 2
NO_TEXT = (
    "All artwork surfaces must be completely unmarked. No letters, numbers, digits, speech bubbles, "
    "captions, pictograms or watermarks. Clock screens and papers are solid blank shapes. "
    "Use only flat geometric 2D silhouettes, stable original character proportions and the dark navy palette."
)
REFERENCE = Path(__file__).resolve().parents[2] / "assets/story/inner_boss_character_reference.png"


def initialize(session: dict, script: str) -> None:
    scenes = parse_script(script)
    session[KEY] = {"id": uuid4().hex, "scenes": scenes, "index": 0, "stage": "script"}
    session["story_voiceover_text"] = script
    session["story_source_text"] = script
    session["recipe_full_text"] = script
    session["state"] = STATE


def keyboard(session):
    stage = session[KEY]["stage"]
    if stage == "music":
        rows = [["Без музыки"], ["Показать сцену"]]
    elif stage == "done":
        rows = [["Прислать ролик", "Добавить музыку"], ["Показать сцену"]]
    else:
        rows = [["ОК"], ["Показать сцену", "Сценарий"]]
        if stage in {"audio", "image", "boss_image", "video", "edit"}:
            rows.append(["Перегенерировать"])
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
        if len({item["voice_id"] for item in inputs}) == 1:
            result = await asyncio.to_thread(generate_story_audio, " ".join(lines), language=lang,
                                             voice_id=inputs[0]["voice_id"])
        else:
            result = await asyncio.to_thread(generate_story_dialogue_audio, inputs, language=lang)
        if not valid_alignment(result.alignment) or result.duration_sec <= 0:
            raise ValueError(f"Озвучка {lang.upper()} пришла без точных таймингов.")
        path = await asyncio.to_thread(store_story_audio, int(update.effective_chat.id), result)
        paths[lang] = {"path": path, "duration": result.duration_sec, "alignment": result.alignment}
        _save(update, session)


async def _image(data, scene):
    from ..vertex_images import generate_image_vertex

    references = [REFERENCE.read_bytes()]
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
    prompt = (
        SHORT_FORMATS[BOSS].frame
        + "\nVertical 9:16 single opening image. Use the first reference for the exact character model. "
        "Keep the setting from any previous frame only if it is the same location. Frame the initial pose. "
        + ("Show only the Hero. The wall must be completely empty where Boss will appear later. " if appearance else "")
        + NO_TEXT + "\n" + opening
    )
    result = await asyncio.to_thread(generate_image_vertex, prompt, reference_images=references, aspect_ratio="9:16")
    path = _target(data, ".png")
    path.write_bytes(result)
    scene["image"], scene["frame_policy"] = str(path), FRAME_POLICY
    scene.pop("boss_image", None)


async def _boss_image(data, scene):
    from ..vertex_images import generate_image_vertex

    _, appearance = frame_actions(scene)
    prompt = (
        "Edit the first reference as the exact same vertical 9:16 shot. Preserve lens, framing, perspective, "
        "all props and their screen positions, the Hero's identity, and the flat nighttime 2D style. "
        "Add the Boss wall shadow from the second reference. Keep him visibly separated from the Hero. "
        "Depict only the instant Boss has just appeared. " + NO_TEXT + "\n" + appearance
    )
    result = await asyncio.to_thread(
        generate_image_vertex, prompt,
        reference_images=[Path(scene["image"]).read_bytes(), REFERENCE.read_bytes()], aspect_ratio="9:16",
    )
    path = _target(data, "-boss.png")
    path.write_bytes(result)
    scene["boss_image"] = str(path)


async def _video(update, data, scene):
    from .handlers_media_core import _generate_video_bytes_with_high_load_retry
    from ..syntax_telethon_fallback import SYNTAX_PROVIDER_KLING

    duration = _duration(scene)
    opening, appearance = frame_actions(scene)
    seconds = max(3, math.ceil(duration - APPEARANCE_LEAD)) if appearance else duration
    source = scene["boss_image"] if appearance else scene["image"]
    prompt = (
        f"{seconds}-second Kling 3.0 image-to-video shot. No audio. 9:16. "
        "Animate the supplied start image, matching all identities and materials. "
        + ("Boss is already present in the first frame. Preserve his silhouette and framing. " if appearance else "")
        + "Use gaze and facial reactions, no lip sync or audio. " + NO_TEXT + " "
        + SHORT_FORMATS[BOSS].motion + "\n" + (appearance or opening)
    )
    raw = await _generate_video_bytes_with_high_load_retry(
        update, prompt=prompt, model="kling_3_0", duration_sec=seconds,
        start_image=Path(source).read_bytes(), end_image=None,
        quality="1080", aspect_ratio="9:16", audio_enabled=False, syntax_provider=SYNTAX_PROVIDER_KLING,
    )
    path = _target(data, ".mp4")
    path.write_bytes(raw)
    if appearance:
        joined = _target(data, "-appearance.mp4")
        await asyncio.to_thread(join_appearance, Path(scene["image"]), path, joined, duration)
        path = joined
    scene["video"] = str(path)


async def _edit(data, scene):
    previews = {}
    for language in data["languages"]:
        path = _target(data, f"-edit-{language}.mp4")
        await asyncio.to_thread(render_scene, scene, path, language=language, duration=_duration(scene))
        previews[language] = str(path)
    scene["previews"] = previews


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
        for key in ("image", "frame_policy", "boss_image"):
            if key in previous:
                replacement[key] = previous[key]
        if (replacement["turns"] == previous["turns"] and _duration(replacement) == _duration(previous)
                and previous.get("video")):
            replacement["video"] = previous["video"]
            replacement["video_duration"] = previous.get("video_duration", _duration(previous))
    data["scenes"][index] = replacement
    data.pop("masters", None)
    data["stage"] = "scene"


async def _show(update, session):
    data = session[KEY]
    stage = data["stage"]
    if stage == "script":
        await announce(update.message, session)
        return
    if stage == "music":
        await update.message.reply_text("Все сцены утверждены. Пришли музыку аудиофайлом или выбери «Без музыки».",
                                        reply_markup=keyboard(session))
        return
    if stage == "done":
        await _send_masters(update, session)
        return
    scene = data["scenes"][data["index"]]
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
            note = "«ОК» запустит немой клип Kling."
    elif stage == "boss_image":
        with Path(scene["boss_image"]).open("rb") as file:
            await update.message.reply_photo(photo=file, caption=f"{scene['title']} · Босс появился")
        note = "«ОК» запустит немой Kling с этого кадра. Первый кадр бот добавит на монтаже."
    elif stage == "video":
        with Path(scene["video"]).open("rb") as file:
            await update.message.reply_video(video=file, caption=scene["title"])
        note = "«ОК» сведёт речь, звуки и титры этой сцены."
    else:
        for lang, preview in scene["previews"].items():
            with Path(preview).open("rb") as file:
                await update.message.reply_video(video=file, caption=f"{scene['title']} · монтаж {lang.upper()}")
        note = "«ОК» закрепит монтаж и откроет следующую сцену. Для правки пришли новый текст сцены."
    await update.message.reply_text(note, reply_markup=keyboard(session))


async def handle_text(update, session, text):
    data = session[KEY]
    stage = data["stage"]
    low = text.strip().casefold()
    scene = data["scenes"][data["index"]]
    try:
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
        elif low == "показать сцену" and stage in {"music", "done"}:
            data["stage"] = "scene"
        elif low == "добавить музыку" and stage == "done":
            data["stage"] = "music"
        elif low in {"показать сцену", "прислать ролик"}:
            pass
        elif low == "без музыки" and stage == "music":
            await finish(update, session, None)
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
                elif stage == "video":
                    await _video(update, data, scene)
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
            elif stage in {"image", "boss_image"}:
                if not _clean_opening(scene):
                    raise ValueError("Этот кадр был сделан по старому промту. Сначала нажми «Перегенерировать».")
                if stage == "image" and frame_actions(scene)[1]:
                    if not scene.get("boss_image"):
                        await _boss_image(data, scene)
                    data["stage"] = "boss_image"
                else:
                    if not scene.get("video"):
                        await _video(update, data, scene)
                    scene["video_duration"] = _duration(scene)
                    data["stage"] = "video"
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


async def finish(update, session, music_bytes):
    from ..artifact_store import persist_post_production_artifacts
    from ..generation_store import ensure_generation_run_for_session, mark_generation_run_ready
    from ..post_production import _has_audio_stream

    data = session[KEY]
    if not all(scene.get("approved") for scene in data["scenes"]):
        raise ValueError("Сначала утверди монтаж каждой сцены.")
    target = _directory(data)
    music = None
    if music_bytes:
        music = target / f"music-{uuid4().hex}.audio"
        music.write_bytes(music_bytes)
        if not await asyncio.to_thread(_has_audio_stream, music):
            raise ValueError("Этот файл не распознан как аудио.")
    batch = uuid4().hex[:10]
    masters = {}
    for lang in data["languages"]:
        destination = target / f"final-{batch}-{lang}.mp4"
        clips = [Path(scene["previews"][lang]) for scene in data["scenes"]]
        await asyncio.to_thread(render_master, clips, music, destination)
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
