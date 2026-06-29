"""
Пайплайн генерации контента: концепты → цельный рецепт (апрув) → сцены (до 6) → production (промпты).

Текстовые шаги (концепт, рецепт, сценарий) вызывают Gemini через ``pipeline_llm_steps.gemini_text``
(внутри — ``asyncio.to_thread(gemini_generate_sync, ...)``). Остальные вызовы LLM в этом модуле
используют тот же ``gemini_generate_sync`` с ``safe_llm_call`` (rate limit → пауза 60 с, до 3 попыток).

Порядок текстовых шагов и контракт сцен для нескольких проектов: ``constants.TEXT_PIPELINE_STEPS``,
``app.pipeline_flow``. Машина состояний в Telegram — ``handlers``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable

from .cache import (
    get_concepts,
    get_recipe_full,
    get_recipe_steps,
    get_scenes_plan,
    get_viral_concept_brief,
    set_concepts,
    set_recipe_full,
    set_recipe_steps,
    set_scenes_plan,
    set_viral_concept_brief,
)
from .assets import ASSETS
from .character_catalog import GRANDMA_KITCHEN_PROJECT_SLUG, sanitize_character_names_and_keys
from .constants import ELEVENLABS_VOICE_ID_ENV_NAME
from .formatting import (
    build_human_readable_plan,
    format_concepts,
    format_frame_prompts_for_approval,
    format_romantic_drama_status,
)
from .llm_utils import extract_json_array, extract_json_object, gemini_generate_sync
from .multilingual import language_name, target_languages_from_env
from . import pipeline_llm_steps as llm_steps
from .scene_speech_budget import (
    append_prompt_timing_hints,
    build_default_speech_plan,
    build_speech_cues_from_plan,
    normalize_speech_phrase_for_voice,
    speech_chars_excluding_pauses,
    speech_budget_for_duration,
)
from .scene_utils import (
    extract_recipe_steps_text,
    normalize_recipe_steps,
    normalize_scenes_for_pipeline,
)
from .viral_retention import enrich_scenes_with_retention, normalize_viral_concept_brief
from .visual_props import append_visual_prop_descriptions

logger = logging.getLogger("bot")


FINAL_SCENE_SUBSCRIBE_CTA = llm_steps.FINAL_SCENE_SUBSCRIBE_CTA
FINAL_SCENE_SUBSCRIBE_CTA_VEO3 = llm_steps.FINAL_SCENE_SUBSCRIBE_CTA_VEO3


GRANDMA_VOICE_LOCK_FULL = (
    "female voice Russian language elderly grandmother age approximately seventy to seventy five years; "
    "tone very warm soft caring; emotional state calm nurturing stable; pitch low to medium stable; "
    "speech rate approximately 1.1; articulation clear but soft; neutral Russian accent."
)

VEO3_FAST_VIDEO_FRAMING_RULE = (
    "veo3_framing: close-up to medium-close shot; grandmother's full figure always fully visible in frame "
    "(at least head through torso, never a tight face-only crop); every other character in the scene stays "
    "fully visible in frame; accurate lip sync with the spoken Russian speech; camera angle and framing "
    "evolve during the clip (subtle reframing, angle shifts, or gentle camera movement) — avoid one locked "
    "static tripod angle for the entire take."
)
VEO3_COOKING_CAST_RULE = (
    "veo3_cast_rule: for Grandma Kitchen project, grandmother and the orange cat are mandatory in frame and "
    "fully visible in every shot. If the white cat or black-and-white cat are listed in scene.characters or mentioned in the prompt, "
    "they must also stay fully visible."
)
VEO3_CARTOON_CHARACTER_RULE = (
    "veo3_cartoon_character_rule: visual style is explicitly cartoon; grandmother is a cartoon grandmother, "
    "the cat is a cartoon cat; include the exact phrase \"cartoon cat\"; never render grandma or cat as realistic, "
    "photorealistic, or live-action characters."
)
VIDEO_KITCHEN_PHYSICS_RULE = (
    "kitchen_physics_rule: cookware and electric appliances must be physically plausible and functional. "
    "If a pot, saucepan, cauldron, or pan is cooking, it must sit on a visible stovetop burner/hob, not float on a bare table. "
    "If a blender is used, show a complete blender with jar locked onto its motor base; before blending the jar is closed with a lid, "
    "then grandma turns it on from the base/control area. If a hand mixer or stand mixer is used, show beaters attached, a bowl underneath, "
    "and the mixer connected to/held by its motor body; no impossible floating appliance parts."
)
PROJECT_SCOPE_ENV_NAME = "CONTENT_FABRIC_PROJECT_SCOPE"
PROJECT_SCOPE_GRANDMA_CAT_COOKING = GRANDMA_KITCHEN_PROJECT_SLUG
_IMAGE_PLAN_TOKENS = (
    "крупный план",
    "крупно",
    "средний план",
    "средне-крупный",
    "общий план",
    "дальний план",
    "close-up",
    "medium close",
    "medium shot",
    "wide shot",
)
_IMAGE_CLOSEUP_TOKENS = ("крупный план", "крупно", "close-up", "close up")
_IMAGE_NON_CLOSEUP_TOKENS = (
    "средний план",
    "средне-крупный",
    "общий план",
    "дальний план",
    "medium shot",
    "wide shot",
    "establishing shot",
)
_FRAME_PLAN_EXCEPTION_LOCATIONS = {"fridge", "холодильник", "kamin", "камин", "fireplace"}
START_FRAME_RECIPE_VISUAL_DETAIL_RULE = (
    "Для стартового кадра обязательно подробно опиши: способ нарезки продуктов, если есть нарезка "
    "(кубик, соломка, ломтики, кольца, тёрка и т.д.); описание блюда или текущего состояния блюда; "
    "подробное описание видимых ингредиентов, их форму, цвет, фактуру и состояние обработки."
)
TITLE_SAFE_FIRST_SCENE_START_FRAME_RULE = (
    "Композиция под заголовок: в первой сцене верхние примерно 30% кадра должны оставаться свободными "
    "от персонажей, лиц, рук, посуды, еды и крупных деталей; важное действие размещай ниже этой зоны. "
    "Не обрезай кадр и не делай пустую искусственную полосу сверху: верхняя зона должна выглядеть как естественное "
    "продолжение сцены с тем же фоном, светом, столешницей или интерьером, только без крупных объектов."
)
TITLE_SAFE_FIRST_SCENE_VIDEO_RULE = (
    "title_safe_top_30: for scene 1, keep roughly the top 30% of the frame clear of characters, faces, "
    "hands, cookware, food, and large visual details for an added title overlay; keep the main action below that zone. "
    "Do not crop the frame or create an artificial blank band at the top; the top area must remain a natural continuation "
    "of the same scene with background, lighting, tabletop or interior texture, just without large objects."
)
FRUIT_ONLY_FIRST_MOMENT_RULE = (
    "If the user explicitly asks scene 1 to start with a close-up of anthropomorphic banana and strawberry talking "
    "or says the start frame is only banana/strawberry and without grandma/cats, the START frame must show only that "
    "first moment. Do not add grandma, cats, grandson/granddaughter, blender reveal, or later payoff into the start "
    "frame; place those later beats in video_prompt_en or requested end_frame_prompt_ru."
)


def _fruit_only_closeup_prompt(prompt: str) -> bool:
    low = str(prompt or "").lower()
    has_banana = "banana" in low or "банан" in low
    has_strawberry = "strawberry" in low or "клубник" in low
    if not (has_banana and has_strawberry):
        return False
    blender_reveal = any(
        x in low
        for x in (
            "внутри прозрачной чаш",
            "чаша-кувшин",
            "моторная база",
            "кнопки теперь хорошо видны",
            "раскрыл",
        )
    )
    if blender_reveal:
        return False
    explicit_only = any(x in low for x in ("только банан", "только клубник", "only banana", "only strawberry"))
    explicit_without_cast = any(
        x in low
        for x in (
            "без бабуш",
            "без кот",
            "ни бабуш",
            "ни кот",
            "no grandma",
            "no grandmother",
            "no cat",
        )
    )
    closeup = any(x in low for x in ("крупный план", "крупно", "close-up", "close up"))
    talking_food = any(x in low for x in ("говор", "разговарива", "шепч", "talk", "speak", "whisper"))
    office_bit = "офис" in low or "office" in low
    return explicit_without_cast or explicit_only or (closeup and talking_food and office_bit)


def _strip_location_context_for_llm(value: Any) -> Any:
    """
    Возвращает копию payload без сюжетных location-полей/ассетов.

    Location data может храниться в проекте для совместимости, но в LLM-запросы
    больше не уходит: консистентность держится на предметных continuity anchors.
    """
    if isinstance(value, list):
        return [_strip_location_context_for_llm(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, raw in value.items():
        k = str(key)
        if k in {"location", "location_top_view", "allowed_locations_strict"}:
            continue
        if k in {"character_descriptions", "visual_prop_descriptions"}:
            continue
        if k == "assets" and isinstance(raw, dict):
            out[k] = {"characters": raw.get("characters") or {}}
            continue
        if k == "references" and isinstance(raw, dict):
            out[k] = {
                rk: rv
                for rk, rv in raw.items()
                if str(rk) not in {"location", "location_top_view"}
            }
            continue
        out[k] = _strip_location_context_for_llm(raw)
    return out


def _is_grandma_cat_cooking_project_scope() -> bool:
    """Определяет профиль проекта, где для veo3 обязателен кадр с бабушкой и котом."""
    raw = (os.getenv(PROJECT_SCOPE_ENV_NAME) or PROJECT_SCOPE_GRANDMA_CAT_COOKING).strip().lower()
    return raw in {PROJECT_SCOPE_GRANDMA_CAT_COOKING, "grandma_cat_cooking"}


def _romantic_subplot_from_scenes(
    base: dict[str, Any] | None,
    scenes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge the code-level drama decision with the LLM-invented episode case."""
    subplot = dict(base or {})
    episode: dict[str, Any] = {}
    scene_characters: set[str] = set()
    for scene in scenes:
        for ch in scene.get("characters") or []:
            if isinstance(ch, str) and ch.strip():
                scene_characters.add(ch.strip().lower())
        raw = scene.get("romantic_drama_episode")
        if isinstance(raw, dict) and raw:
            episode = raw
            break
    if not subplot.get("enabled") and (episode or "zaya" in scene_characters or "markiz" in scene_characters):
        subplot["enabled"] = True
        subplot["characters"] = [
            ch for ch in ("zaya", "markiz") if ch in scene_characters or ch in (subplot.get("characters") or [])
        ] or ["zaya"]
        subplot.setdefault("decision_scope", "episode")
        subplot.setdefault(
            "series_rule",
            "The orange cat and the white cat are a pair; the black-and-white cat is the white cat's admirer only in selected episodes.",
        )
    if not subplot.get("enabled"):
        return subplot
    for key in ("brief", "zaya_timosha_arc", "markiz_arc", "happy_end"):
        value = episode.get(key)
        if isinstance(value, str) and value.strip():
            subplot[key] = _replace_character_names_with_short_descriptions(value.strip())
    if not subplot.get("characters"):
        subplot["characters"] = ["zaya"]
    return subplot


def _replace_character_names_with_short_descriptions(text: str) -> str:
    return sanitize_character_names_and_keys(text)


def _scene_romantic_drama_beat(scene: dict[str, Any]) -> str:
    return _replace_character_names_with_short_descriptions(str(scene.get("romantic_drama_beat") or "").strip())


def _append_romantic_drama_to_start_prompt(prompt: str, scene: dict[str, Any]) -> str:
    beat = _scene_romantic_drama_beat(scene)
    text = sanitize_character_names_and_keys(prompt).strip()
    if not beat:
        return text
    marker = "Фоновая драма этой сцены:"
    if marker.lower() in text.lower() or beat.lower() in text.lower():
        return text
    return f"{text}\n\n{marker} {beat}. Драма должна быть видна и понятна зрителю в этом кадре.".strip()


def _append_romantic_drama_to_video_prompt(prompt: str, scene: dict[str, Any]) -> str:
    beat = _scene_romantic_drama_beat(scene)
    text = sanitize_character_names_and_keys(prompt).strip()
    if not beat:
        return text
    marker = "background romantic drama beat:"
    if marker in text.lower() or beat.lower() in text.lower():
        return text
    return (
        f"{text} {marker} {beat}. The subplot must be visible and understandable as background action throughout this clip."
    ).strip()


def _enforce_romantic_subplot_characters(scenes: list[dict[str, Any]], subplot: dict[str, Any] | None) -> None:
    """Keep approved white-cat/black-and-white-cat subplot visible through the generated scenes."""
    if not isinstance(subplot, dict) or not subplot.get("enabled"):
        return
    required = [str(ch).strip().lower() for ch in (subplot.get("characters") or ["zaya"]) if str(ch).strip()]
    if "zaya" not in required:
        required.insert(0, "zaya")
    for scene in scenes:
        chars = scene.get("characters")
        if not isinstance(chars, list):
            chars = []
        normalized = [str(ch).strip().lower() for ch in chars if str(ch).strip()]
        for ch in required:
            if ch not in normalized:
                chars.append(ch)
                normalized.append(ch)
        scene["characters"] = chars
        if not str(scene.get("romantic_drama_beat") or "").strip():
            if "markiz" in required:
                scene["romantic_drama_beat"] = (
                    "Белая кошка напряженно следит за рыжим котом во время готовки, а черно-белый кот виден снаружи окна."
                )
            else:
                scene["romantic_drama_beat"] = "Белая кошка напряженно следит за рыжим котом во время готовки."


def _ensure_image_prompt_framing_requirements(
    prompt: str,
    location: str | None = None,
    *,
    model: str | None = None,
    title_safe_top: bool = False,
) -> str:
    """
    Добавляет обязательные маркеры для image prompt:
    - по умолчанию `Крупный план`;
    - исключение по плану (`Средний план` / `Общий план`) только для сцен у `fridge` / `kamin`;
    - ракурс не дописывается локально: его формирует нейросеть.
    """
    p = (prompt or "").strip()
    if p.lower() == "пусто":
        return "пусто"
    low = p.lower()
    loc = (location or "").strip().lower()
    exception_by_location = loc in _FRAME_PLAN_EXCEPTION_LOCATIONS
    parts: list[str] = [p] if p else []
    has_plan = any(t in low for t in _IMAGE_PLAN_TOKENS)
    has_closeup = any(t in low for t in _IMAGE_CLOSEUP_TOKENS)
    has_non_closeup = any(t in low for t in _IMAGE_NON_CLOSEUP_TOKENS)
    if exception_by_location:
        if not has_non_closeup:
            parts.append("Средний план.")
    else:
        # По умолчанию фиксируем крупный план, даже если модель предложила средний/общий.
        if not has_closeup:
            parts.append("Крупный план.")
    model_name = (model or "").strip().lower()
    if model_name.startswith("veo3") and not _fruit_only_closeup_prompt(p):
        cast_rule = "Бабушка и кот должны быть видны полностью в кадре."
        joined = " ".join(parts).lower()
        if "бабушка и кот должны быть видны полностью" not in joined:
            parts.append(cast_rule)
    if title_safe_top:
        joined = " ".join(parts).lower()
        if "верхние примерно 30%" not in joined and "title_safe_top_30" not in joined:
            parts.append(TITLE_SAFE_FIRST_SCENE_START_FRAME_RULE)
    return " ".join(x.strip() for x in parts if x.strip()).strip()


def _append_first_scene_title_safe_video_rule(video_prompt: str, *, scene_index: int) -> str:
    out = str(video_prompt or "").strip()
    return out


def append_veo3_video_framing_constraints(
    video_prompt: str,
    scene: dict[str, Any] | None = None,
    *,
    model_fallback: str = "veo3_fast",
) -> str:
    """
    Для моделей ``veo3*`` добавляет в конец промпта правила кадра/ракурса (см. ``VEO3_FAST_VIDEO_FRAMING_RULE``).

    Идемпотентно: если подстрока ``veo3_framing:`` уже есть, повторно не добавляет.
    """
    vp = (video_prompt or "").strip()
    model = model_fallback
    if scene is not None:
        model = str(scene.get("model", model_fallback) or model_fallback).lower().strip()
    if not model.startswith("veo3"):
        return vp
    out = vp
    low = out.lower()
    if "veo3_framing:" not in low:
        out = f"{out} {VEO3_FAST_VIDEO_FRAMING_RULE}".strip()
        low = out.lower()
    if _is_grandma_cat_cooking_project_scope() and "veo3_cast_rule:" not in low:
        out = f"{out} {VEO3_COOKING_CAST_RULE}".strip()
        low = out.lower()
    if _is_grandma_cat_cooking_project_scope() and "veo3_cat_count_rule:" not in low:
        cat_count_rule = _build_veo3_cat_count_rule(scene)
        if cat_count_rule:
            out = f"{out} {cat_count_rule}".strip()
    return out


def _scene_has_character(scene: dict[str, Any] | None, character_key: str) -> bool:
    if not isinstance(scene, dict):
        return False
    chars = scene.get("characters")
    if not isinstance(chars, list):
        return False
    needle = str(character_key).strip().lower()
    return any(str(ch).strip().lower() == needle for ch in chars if str(ch).strip())


def _scene_start_frame_mentions_white_cat(scene: dict[str, Any] | None) -> bool:
    if not isinstance(scene, dict):
        return False
    text = " ".join(
        str(scene.get(key, "")).strip().lower()
        for key in ("start_frame_prompt", "start_frame_prompt_ru")
    )
    return any(token in text for token in ("zaya", "зая", "заю", "заи", "белая кошка", "белый кот", "white cat"))


def _scene_start_frame_mentions_markiz(scene: dict[str, Any] | None) -> bool:
    if not isinstance(scene, dict):
        return False
    text = " ".join(
        str(scene.get(key, "")).strip().lower()
        for key in ("start_frame_prompt", "start_frame_prompt_ru")
    )
    return any(
        token in text
        for token in ("markiz", "маркиз", "маркиза", "черно-белый кот", "чёрно-белый кот", "black-and-white cat")
    )


def _build_veo3_cat_count_rule(scene: dict[str, Any] | None) -> str:
    """Builds strict anti-duplication rule for cat identities in Grandma Kitchen videos."""
    rules = [
        "veo3_cat_count_rule: exactly one orange cat in frame; never show two orange cats or duplicate the orange cat identity."
    ]
    if _scene_has_character(scene, "zaya") or _scene_start_frame_mentions_white_cat(scene):
        rules.append("Exactly one white cat in frame; never show two white cats or duplicate the white cat identity.")
    if _scene_has_character(scene, "markiz") or _scene_start_frame_mentions_markiz(scene):
        rules.append(
            "Exactly one black-and-white cat in frame; never show two black-and-white cats or duplicate the black-and-white cat identity."
        )
    return " ".join(rules)


_LLM_VIDEO_CHARACTERS_SPEAKS_RULE = (
    "CHARACTERS SPEAKS (обязательно, если у сцены непустое поле `text`): внутри `video_prompt_en` должна быть "
    "подстрока ровно в формате `CHARACTERS SPEAKS: ` сразу за двоеточием и пробелом — далее **дословно** "
    "то же значение, что в `text` (те же символы, регистр, токены [пауза], язык). Не перефразировать. "
    "В самом `text` озвучки не должно быть дефиса или тире: символы -, – и — запрещены. "
    "Обычно размещайте эту подстроку ближе к концу промпта. "
    "Python-код **не** дописывает `CHARACTERS SPEAKS` — если опустить, озвучка по сценарию потеряется."
)


def _should_apply_voice_lock(scene: dict[str, Any]) -> bool:
    """
    Нужно ли добавлять `voice_lock_full` в video prompt.

    Правило:
    - всегда для `veo3*`;
    - также для сцен, где явно нужна озвучка (`voice_needed` или непустой `text`).
    """
    model = str(scene.get("model", "")).lower().strip()
    if model.startswith("veo3"):
        return True
    if bool(scene.get("voice_needed")):
        return True
    return bool(str(scene.get("text", "")).strip())


def _enforce_voice_lock_if_needed(video_prompt: str, scene: dict[str, Any]) -> str:
    """
    Добавляет `voice_lock_full` в prompt только когда это требуется политикой сцены.
    """
    vp = (video_prompt or "").strip()
    if not _should_apply_voice_lock(scene):
        return vp
    if GRANDMA_VOICE_LOCK_FULL.lower() in vp.lower():
        return vp
    return f"{vp} voice_lock_full: {GRANDMA_VOICE_LOCK_FULL}".strip()


def _finalize_scene_video_prompt(video_prompt_en: str, scene: dict[str, Any]) -> str:
    """
    Добавляет ``voice_lock_full`` при необходимости и runtime-критичные правила кадра Veo 3 fast
    (липсинк, полная бабушка, все персонажи в кадре, крупный план, смена ракурса).

    Контентные правила вроде cartoon grandma/cartoon cat и kitchen physics должны попадать в
    композицию prompt на этапе LLM-генерации, а не насильно приклеиваться к каждому готовому
    ``video_prompt`` на пост-обработке. ``CHARACTERS SPEAKS`` задаёт только модель.
    """
    vp = _append_romantic_drama_to_video_prompt(str(video_prompt_en).strip(), scene)
    vp = _enforce_voice_lock_if_needed(vp, scene)
    return append_veo3_video_framing_constraints(vp, scene)


def _replace_characters_speaks(video_prompt: str, speech_text: str) -> str:
    """Sync CHARACTERS SPEAKS section with provided phrase."""
    marker = "CHARACTERS SPEAKS:"
    prompt = str(video_prompt or "").strip()
    phrase = str(speech_text or "").strip()
    if not prompt:
        return f"{marker} {phrase}".strip()
    low = prompt.lower()
    marker_low = marker.lower()
    idx = low.find(marker_low)
    if idx < 0:
        return f"{prompt}\n{marker} {phrase}".strip()
    head = prompt[: idx + len(marker)]
    tail = prompt[idx + len(marker) :]
    tail_lines = tail.splitlines()
    tail_rest = "\n".join(tail_lines[1:]).strip() if len(tail_lines) > 1 else ""
    merged = f"{head} {phrase}".strip()
    if tail_rest:
        merged = f"{merged}\n{tail_rest}"
    return merged.strip()


_DIALOGUE_SPEAKER_ALIASES = {
    "grandma": "grandma",
    "бабушка": "grandma",
    "grandfather": "grandfather",
    "grandpa": "grandfather",
    "дедушка": "grandfather",
    "grandson": "grandson",
    "внук": "grandson",
    "granddaughter": "granddaughter",
    "внучка": "granddaughter",
    "daughter": "daughter",
    "дочь": "daughter",
    "дочка": "daughter",
    "banana": "banana",
    "банан": "banana",
    "strawberry": "strawberry",
    "клубника": "strawberry",
}


def _normalize_dialogue_speaker(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _DIALOGUE_SPEAKER_ALIASES.get(text, text)


def _aggregate_dialogue_text(dialogue: list[dict[str, str]]) -> str:
    parts = [str(item.get("text") or "").strip() for item in dialogue if str(item.get("text") or "").strip()]
    return " [пауза] ".join(parts).strip()


def _normalize_scene_dialogue(scene: dict[str, Any], *, scene_index: int) -> None:
    raw_mode = str(scene.get("dialogue_mode") or "").strip().lower()
    raw_dialogue = scene.get("dialogue")
    normalized: list[dict[str, str]] = []
    if isinstance(raw_dialogue, list):
        for item in raw_dialogue:
            if not isinstance(item, dict):
                continue
            speaker = _normalize_dialogue_speaker(item.get("speaker"))
            text = str(item.get("text") or "").strip()
            if not speaker or not text:
                continue
            normalized.append({"speaker": speaker, "text": text})
    if scene_index != 1:
        scene["dialogue_mode"] = "single"
        scene.pop("dialogue", None)
        return
    if raw_mode == "multi" or len(normalized) >= 2:
        scene["dialogue_mode"] = "multi"
        scene["dialogue"] = normalized
        aggregated = _aggregate_dialogue_text(normalized)
        if aggregated:
            scene["text"] = aggregated
    else:
        scene["dialogue_mode"] = "single"
        if normalized:
            scene["dialogue"] = normalized[:1]
        elif scene.get("dialogue") is not None:
            scene["dialogue"] = []


def _dialogue_by_lang_for_scene(scene: dict[str, Any], speech_by_lang: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    mode = str(scene.get("dialogue_mode") or "").strip().lower()
    if mode != "multi":
        return {}
    raw = scene.get("dialogue")
    if not isinstance(raw, list):
        return {}
    ru_items: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        speaker = _normalize_dialogue_speaker(item.get("speaker"))
        text = str(item.get("text") or "").strip()
        if speaker and text:
            ru_items.append({"speaker": speaker, "text": text})
    return {"ru": ru_items} if ru_items and "ru" in speech_by_lang else {}


async def _build_scene_dialogue_by_lang(
    scene: dict[str, Any],
    target_languages: list[str],
) -> dict[str, list[dict[str, str]]]:
    """Translate structured dialogue per language while keeping speaker identities intact."""
    mode = str(scene.get("dialogue_mode") or "").strip().lower()
    if mode != "multi":
        return {}
    raw = scene.get("dialogue")
    if not isinstance(raw, list):
        return {}
    ru_items: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        speaker = _normalize_dialogue_speaker(item.get("speaker"))
        text = str(item.get("text") or "").strip()
        if speaker and text:
            ru_items.append({"speaker": speaker, "text": text})
    if len(ru_items) < 2:
        return {}

    languages = target_languages or target_languages_from_env()
    out: dict[str, list[dict[str, str]]] = {"ru": ru_items}
    for lang in languages:
        lang_code = str(lang or "").strip().lower()
        if not lang_code or lang_code == "ru":
            continue
        translated_items: list[dict[str, str]] = []
        for item in ru_items:
            try:
                translated_text = await _translate_scene_text(item["text"], lang_code)
            except Exception:
                logger.warning("dialogue translation failed for lang=%s speaker=%s", lang_code, item["speaker"])
                translated_text = item["text"]
            translated_items.append({"speaker": item["speaker"], "text": translated_text or item["text"]})
        out[lang_code] = translated_items
    return out


def _speech_by_lang_from_dialogue(dialogue_by_lang: dict[str, list[dict[str, str]]]) -> dict[str, str]:
    """Aggregate dialogue lines into the legacy single-string speech map."""
    out: dict[str, str] = {}
    for lang, items in dialogue_by_lang.items():
        if not isinstance(items, list):
            continue
        aggregated = _aggregate_dialogue_text(items)
        if aggregated:
            out[str(lang).strip().lower()] = aggregated
    return out


async def _translate_scene_text(text: str, target_language: str) -> str:
    """Translate scene phrase to requested language using LLM."""
    source = (text or "").strip()
    if not source:
        return ""
    lang_code = (target_language or "").strip().lower()
    if lang_code == "ru":
        return source
    lang_label = language_name(lang_code)
    prompt = (
        "Translate the phrase for short video dubbing.\n"
        f"Target language: {lang_label} ({lang_code}).\n"
        "Do not use hyphen-minus, en dash, or em dash in the spoken phrase; use words or [пауза] instead.\n"
        "Return only translated phrase without comments.\n"
        f"SOURCE:\n{source}\n"
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt)
    translated = normalize_speech_phrase_for_voice(raw or "")
    return translated or normalize_speech_phrase_for_voice(source)


async def _build_scene_speech_by_lang(text: str, target_languages: list[str]) -> dict[str, str]:
    """Return phrase map for all target languages."""
    languages = target_languages or target_languages_from_env()
    out: dict[str, str] = {}
    for lang in languages:
        try:
            out[lang] = await _translate_scene_text(text, lang)
        except Exception:
            logger.warning("speech translation failed for lang=%s", lang)
            out[lang] = (text or "").strip()
    if "ru" not in out:
        out["ru"] = (text or "").strip()
    return out


def _build_scene_speech_plan_by_lang(
    speech_by_lang: dict[str, str],
    dialogue_by_lang: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Build default phrase plans and provisional cues per language."""
    plans: dict[str, list[dict[str, Any]]] = {}
    cues: dict[str, list[dict[str, Any]]] = {}
    dialogue_map = dialogue_by_lang if isinstance(dialogue_by_lang, dict) else {}
    for lang, text in speech_by_lang.items():
        dialogue_items = dialogue_map.get(lang) if isinstance(dialogue_map.get(lang), list) else None
        if dialogue_items:
            plan = [{"speaker": item["speaker"], "text": item["text"]} for item in dialogue_items if item.get("text")]
        else:
            plan = build_default_speech_plan(text, lang)
        plans[lang] = plan
        cues[lang] = build_speech_cues_from_plan(plan, duration_sec=0.0)
    return plans, cues


def _speech_budget_by_lang(
    speech_by_lang: dict[str, str],
    duration_sec: int | float | None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for lang, text in speech_by_lang.items():
        meta = dict(speech_budget_for_duration(int(duration_sec or 6), lang))
        meta["current_chars"] = speech_chars_excluding_pauses(str(text or ""))
        meta["status"] = "ok" if meta["current_chars"] <= meta["max_chars"] else "overflow"
        meta["auto_shortened"] = False
        out[lang] = meta
    return out


def _speaker_voice_params(character: str) -> dict[str, str]:
    """
    Базовые голосовые параметры для разных персонажей.
    """
    c = (character or "").lower()
    if c == "grandma":
        return {
            "voice_profile": "grandma_ru_warm_70yo",
            "voice_lock_full": GRANDMA_VOICE_LOCK_FULL,
        }
    if c in {"grandson"}:
        return {
            "voice_profile": "male_ru_child_bright",
            "voice_lock_full": (
                "male child voice Russian language around eight to ten years old; "
                "tone bright excited; pitch medium to high; speech rate 1.0; clear articulation."
            ),
        }
    if c in {"daughter", "granddaughter"}:
        return {
            "voice_profile": "female_ru_child_soft" if c == "granddaughter" else "female_ru_adult_soft",
            "voice_lock_full": (
                "female child voice Russian language around eight to ten years old; "
                "tone bright warm; pitch medium to high; speech rate normal 1.0; clear articulation."
                if c == "granddaughter"
                else "female voice Russian language adult around thirty years old; "
                "tone warm lively; pitch medium; speech rate normal 0.95; clear articulation."
            ),
        }
    if c in {"husband", "grandpa", "grandfather"}:
        return {
            "voice_profile": "male_ru_adult_calm",
            "voice_lock_full": (
                "male voice Russian language adult around sixty years old; "
                "tone calm friendly; pitch low to medium; speech rate 0.9."
            ),
        }
    return {
        "voice_profile": "ru_neutral_default",
        "voice_lock_full": "neutral Russian voice, medium pitch, calm delivery",
    }


def _tts_payload_for_scene(scene: dict[str, Any]) -> dict[str, Any]:
    """
    Явный контракт озвучки для downstream-генерации (чтобы не «угадывать» голос).
    """
    provider = str(scene.get("voice_provider", "native"))
    text_ru = str(scene.get("text", "")).strip()
    payload = {
        "provider": provider,
        "language": "ru",
        "speaker": "grandma",
        "text_ru": text_ru,
    }
    dialogue_mode = str(scene.get("dialogue_mode") or "single").strip().lower()
    dialogue = scene.get("dialogue") if isinstance(scene.get("dialogue"), list) else []
    if dialogue_mode == "multi" and dialogue:
        payload["dialogue_mode"] = "multi"
        payload["dialogue_ru"] = [
            {"speaker": _normalize_dialogue_speaker(item.get("speaker")), "text": str(item.get("text") or "").strip()}
            for item in dialogue
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        speaking = []
        for item in payload["dialogue_ru"]:
            sp = item.get("speaker")
            if sp and sp not in speaking:
                speaking.append(sp)
        payload["speaking_characters_order"] = speaking
        if speaking:
            payload["speaker"] = speaking[0]
    if provider == "elevenlabs":
        voice_id = (os.getenv(ELEVENLABS_VOICE_ID_ENV_NAME) or "").strip()
        payload["voice_profile"] = "grandma_ru_warm_70yo"
        payload["voice_id"] = voice_id or "SET_ELEVENLABS_VOICE_ID"
        payload["voice_mode"] = "voice_id" if voice_id else "voice_lock_only"
        payload["voice_lock_full"] = GRANDMA_VOICE_LOCK_FULL
        characters = [str(x).lower() for x in (scene.get("characters") or []) if str(x).strip()]
        speaker_profiles: dict[str, dict[str, str]] = {}
        for ch in ["grandma"] + [c for c in characters if c not in {"grandma", "cat"}]:
            speaker_profiles[ch] = _speaker_voice_params(ch)
        payload["speaker_profiles"] = speaker_profiles
        payload["non_speaking_characters"] = ["cat"]
        payload["multi_speaker_note"] = (
            "Если в сцене говорят несколько персонажей, используй speaker_profiles и разделяй реплики "
            "по ролям (пример: GRANDMA:, GRANDFATHER:, GRANDSON:, GRANDDAUGHTER:). "
            "CAT никогда не говорит."
        )
    else:
        payload["voice_profile"] = "native_scene_voice"
    return payload


_LLM_RECIPE_STEPS_COUNT_RULE = (
    "КРИТИЧЕСКИ: в ответе ровно от 4 до 6 строк включительно (ни 3, ни 7+). "
    "Длину массива выдерживает только модель — в коде список не дополняют и не обрезают."
)

_LLM_RECIPE_STEPS_QUALITY_RULE = (
    "Качество формулировок — только от модели: у каждого шага минимум 5 слов, конкретные действия с едой; "
    "не использовать пустые обороты «готовит», «делает», «что-то», «простое действие»; "
    "код не валидирует шаги и не запускает второй проход только из-за «коротко/слишком общо»."
)

_LLM_VIDEO_STAGES_JSON_RULE = (
    "Верни ТОЛЬКО JSON-массив строк (без markdown и без пояснений до/после). "
    "Каждый элемент — один этап съёмки: конкретное действие с едой на русском, обычно 5–14 слов."
)

_RUSSIAN_YO_RULE = llm_steps.RUSSIAN_YO_RULE

def _parse_video_stages_from_llm(raw: str) -> list[str]:
    """
    Разбирает ответ модели с **внутренними** этапами для сцен (не пользовательский рецепт).

    Сначала пробует JSON-массив строк; если не вышло — строки вида «ШАГ N:» (legacy).
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = extract_json_array(text)
    except (ValueError, json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, list) and parsed and all(isinstance(x, str) for x in parsed):
        steps = [x.strip() for x in parsed if x.strip()]
        if steps:
            return normalize_recipe_steps(steps)
    return normalize_recipe_steps(extract_recipe_steps_text(text))


async def _apply_llm_stress_for_veo3(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Дополнительная пост-обработка реплик для veo3/veo3_fast через LLM:
    ударения заглавной буквой + паузы [пауза], без знаков препинания и без слов в полном CAPS.
    При ошибке возвращает исходные сцены без падения пайплайна.
    """
    targets: list[tuple[int, str]] = []
    for i, s in enumerate(scenes):
        model = str(s.get("model", "")).lower()
        if model.startswith("veo3"):
            txt = str(s.get("text", "")).strip()
            if txt:
                targets.append((i, txt))
    if not targets:
        return scenes

    payload = [{"scene_index": i, "text": t} for i, t in targets]
    prompt = (
        "Преобразуй реплики для синтеза речи Veo3.\n"
        "Требования:\n"
        "- сохраняй смысл фразы\n"
        "- ударение помечай заглавной буквой в слове\n"
        "- не используй слова, написанные полностью заглавными буквами\n"
        "- паузы обозначай только токеном [пауза] и используй его как замену знаков препинания\n"
        "- ставь [пауза] только в логических местах между смысловыми фразами, не после каждого слова\n"
        "- не используй дефис и тире в фразах озвучки: запрещены символы -, – и —\n"
        f"- {_RUSSIAN_YO_RULE}\n"
        "- не используй знаки препинания\n"
        "- верни ТОЛЬКО JSON объекта формата {\"items\":[{\"scene_index\":0,\"text\":\"...\"}]}\n\n"
        "INPUT:\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        raw = await asyncio.to_thread(gemini_generate_sync, prompt)
        obj = extract_json_object(raw)
        items = obj.get("items")
        if not isinstance(items, list):
            return scenes
        out = [dict(s) for s in scenes]
        for it in items:
            if not isinstance(it, dict):
                continue
            idx = it.get("scene_index")
            txt = it.get("text")
            if isinstance(idx, int) and 0 <= idx < len(out) and isinstance(txt, str) and txt.strip():
                out[idx]["text"] = txt.strip()
        return out
    except Exception as e:
        logger.warning("LLM stress formatting skipped: %s", e)
        return scenes


async def generate_recipe_concepts(
    user_input: str,
    user_feedback: str | None = None,
    previous_concepts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Шаг 1 диалога: генерирует 3–5 вариантов вирусных концептов блюда под идею пользователя.

    Параметры:
        user_input: исходная идея / запрос.
        user_feedback: если не None — перегенерация с учётом комментария (итерация).
        previous_concepts: прошлый список концептов (чтобы модель не повторялась).

    Возвращает:
        ``{"concepts": [...], "text_plan": "..."}`` — сырой список и готовый текст для Telegram.

    Уточнения и правки формулируются в промпте (см. ``pipeline_llm_steps``); без доработки списка в Python.
    """
    idea = user_input.strip()
    if not idea:
        raise ValueError("Empty concept request.")
    feedback = (user_feedback or "").strip()
    previous = previous_concepts or []

    cached = get_concepts(idea, feedback or None, previous)
    if cached is not None:
        return cached

    prompt = llm_steps.build_concepts_prompt(idea, feedback, previous)
    raw = await llm_steps.gemini_text(prompt)
    try:
        parsed_obj = extract_json_object(raw)
    except Exception:
        repair_prompt = llm_steps.build_concepts_json_repair_prompt(raw)
        repaired = await llm_steps.gemini_text(repair_prompt)
        parsed_obj = extract_json_object(repaired)
    concepts = parsed_obj.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        raise RuntimeError("Gemini не вернул список концептов.")
    concepts = [c for c in concepts if isinstance(c, dict)][:5]
    out = {"concepts": concepts, "text_plan": format_concepts(concepts)}
    set_concepts(idea, feedback or None, previous, out)
    return out


async def generate_recipe_steps(
    concept: str,
    dish_name: str | None = None,
    user_feedback: str | None = None,
    previous_steps: list[str] | None = None,
) -> list[str]:
    """
    Генерирует «сухой» рецепт без креатива: 4–6 шагов с конкретными глаголами.

    Поток:
    1. Запрос к LLM: JSON-массив строк (этапы съёмки), в промпте жёстко 4–6 элементов.
    2. Парсинг → ``normalize_recipe_steps``.
    3. Если шагов < 4 — ``expand_recipe_steps``; если > 6 — ``merge_recipe_steps`` (снова LLM).
    4. Повторная нормализация; при необходимости второй проход expand/merge.

    Возвращает:
        Список строк шагов (уже без префиксов «ШАГ» в элементах списка — только текст шага).
    """
    concept = concept.strip()
    if not concept:
        raise ValueError("Empty concept.")
    feedback = (user_feedback or "").strip()
    prev = previous_steps or []

    cached = get_recipe_steps(concept, feedback or None, prev)
    if cached is not None:
        return cached

    prompt = (
        "Ты создаешь ЧЕТКИЙ рецепт.\n\n"
        f"КОНЦЕПТ:\n{concept}\n\n"
        f"ЦЕЛЕВОЕ БЛЮДО (нельзя менять):\n{(dish_name or concept).strip()}\n\n"
        f"ПРЕДЫДУЩИЕ ШАГИ:\n{json.dumps(prev, ensure_ascii=False)}\n\n"
        f"ФИДБЕК:\n{feedback or '-'}\n\n"
        "ЗАДАЧА:\n"
        "- разложить блюдо на 4-6 простых шагов\n"
        "- рецепт должен быть строго для ЦЕЛЕВОГО БЛЮДА, без подмены на другое блюдо\n"
        "- без креатива\n- без кота\n- без истории\n"
        "- только действия с едой\n"
        "- конкретные глаголы: режет, мешает, жарит, формирует\n"
        "- каждый шаг развернутый: минимум 5-7 слов, с деталями действия\n"
        "- никаких 'готовит' и 'делает что-то'\n\n"
        + _RUSSIAN_YO_RULE
        + "\n"
        + _LLM_RECIPE_STEPS_QUALITY_RULE
        + "\n"
        + _LLM_RECIPE_STEPS_COUNT_RULE
        + "\n"
        "Верни JSON-массив из 4–6 строк (по одному этапу съёмки на элемент, без дубликатов-заглушек только ради длины).\n"
        + _LLM_VIDEO_STAGES_JSON_RULE
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt)
    recipe_steps = _parse_video_stages_from_llm(raw)
    if len(recipe_steps) < 4:
        recipe_steps = await expand_recipe_steps(recipe_steps)
    elif len(recipe_steps) > 6:
        recipe_steps = await merge_recipe_steps(recipe_steps)

    recipe_steps = normalize_recipe_steps(recipe_steps)
    if len(recipe_steps) < 4:
        recipe_steps = await expand_recipe_steps(recipe_steps)
        recipe_steps = normalize_recipe_steps(recipe_steps)
    if len(recipe_steps) > 6:
        recipe_steps = await merge_recipe_steps(recipe_steps)
        recipe_steps = normalize_recipe_steps(recipe_steps)

    set_recipe_steps(concept, feedback or None, prev, recipe_steps)
    return recipe_steps


async def generate_recipe_full_text(
    concept: str,
    dish_name: str | None = None,
    user_feedback: str | None = None,
    previous_text: str | None = None,
) -> str:
    """
    Генерирует рецепт одним связным текстом (ингредиенты и ход приготовления), без нумерованных «ШАГ 1…N».

    Показывается пользователю на этапе утверждения до генерации сцен. После «ОК» сценарий строит
    ``generate_scenes_plan`` по этому тексту одним запросом к Gemini.
    """
    concept = concept.strip()
    if not concept:
        raise ValueError("Empty concept.")
    feedback = (user_feedback or "").strip()
    prev = (previous_text or "").strip()

    cached = get_recipe_full(concept, feedback or None, prev or None)
    if isinstance(cached, str) and cached.strip():
        return cached.strip()

    dish = (dish_name or concept).strip()
    prompt = llm_steps.build_recipe_full_prompt(concept, dish, feedback, prev)
    raw = await llm_steps.gemini_text(prompt)
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError("Модель не вернула текст рецепта.")
    set_recipe_full(concept, feedback or None, prev or None, text)
    return text


async def generate_recipe_from_source_text(
    source_text: str,
    *,
    source_url: str | None = None,
) -> str:
    """
    Извлекает цельный рецепт из сырого текста страницы через LLM.

    Используется для URL-источников: сначала скачиваем текст страницы, затем просим модель
    вернуть только чистый рецепт для пользовательского апрува.
    """
    src = (source_text or "").strip()
    if not src:
        raise ValueError("Пустой source_text для извлечения рецепта.")
    prompt = llm_steps.build_recipe_from_source_text_prompt(src, source_url=source_url)
    raw = await llm_steps.gemini_text(prompt)
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError("Модель не вернула рецепт из текста источника.")
    if _looks_like_recipe_extraction_failure(text):
        raise RuntimeError(
            "В загруженном тексте не найден рецепт. Возможно, сайт отдал страницу-заглушку вместо содержимого."
        )
    return text


def _looks_like_recipe_extraction_failure(text: str) -> bool:
    """Rejects model refusals/apologies so they are not shown as an approved recipe."""
    low = " ".join((text or "").lower().split())
    if not low:
        return False
    failure_markers = (
        "не могу извлечь рецепт",
        "не удалось извлечь рецепт",
        "не получил фактическое содержимое",
        "сообщение об ошибке",
        "необходимость включить поддержку javascript",
        "включить поддержку javascript",
        "enable javascript",
        "javascript is required",
    )
    if not any(marker in low for marker in failure_markers):
        return False
    recipe_markers = ("ингредиенты", "продукты", "приготовление", "способ приготовления")
    return not any(marker in low for marker in recipe_markers)


async def generate_viral_concept_brief(
    concept: str,
    *,
    full_recipe_text: str,
    sponsored_content: bool = False,
    brand_context: str | None = None,
    analytics_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Генерирует Viral Concept Brief перед сценарным планом.

    Это мягкий слой: если LLM вернула битый JSON, пайплайн не падает, а
    использует нормализованный fallback с хуками, first frame и payoff.
    """
    concept = (concept or "").strip()
    body = (full_recipe_text or "").strip()
    if not concept:
        raise ValueError("Empty concept for viral brief.")
    if not body:
        raise ValueError("Empty full_recipe_text for viral brief.")
    cached = get_viral_concept_brief(
        concept,
        body,
        sponsored_content,
        brand_context,
        analytics_feedback,
    )
    if isinstance(cached, dict):
        return cached
    prompt = llm_steps.build_viral_concept_brief_prompt(
        concept=concept,
        full_recipe_text=body,
        sponsored_content=sponsored_content,
        brand_context=brand_context,
        analytics_feedback=analytics_feedback,
    )
    try:
        raw = await llm_steps.gemini_text(prompt)
        parsed = extract_json_object(raw)
    except Exception:
        logger.exception("viral concept brief generation failed; using fallback brief")
        parsed = {}
    out = normalize_viral_concept_brief(
        parsed,
        concept=concept,
        sponsored_content=sponsored_content,
        brand_context=brand_context,
    )
    set_viral_concept_brief(
        concept,
        body,
        sponsored_content,
        brand_context,
        analytics_feedback,
        out,
    )
    return out


async def expand_recipe_steps(recipe_steps: list[str]) -> list[str]:
    """
    Увеличивает число шагов до допустимого диапазона, дробя крупные шаги (без новых ингредиентов).

    Вызывается, когда после парсинга шагов меньше 4.
    """
    prompt = (
        "Тебе дан рецепт с недостаточным количеством шагов.\n\n"
        f"ТЕКУЩИЕ ШАГИ:\n{json.dumps(recipe_steps, ensure_ascii=False)}\n\n"
        "ЗАДАЧА:\n"
        "- увеличить количество шагов до 4-6\n"
        "- разбить крупные шаги на более мелкие\n"
        "- сохранить логику рецепта\n\n"
        "ПРАВИЛА:\n"
        "- не добавлять новые ингредиенты\n"
        "- не менять блюдо\n"
        "- просто детализировать\n\n"
        + _RUSSIAN_YO_RULE
        + "\n"
        + _LLM_RECIPE_STEPS_QUALITY_RULE
        + "\n"
        + _LLM_RECIPE_STEPS_COUNT_RULE
        + "\n"
        "Верни JSON-массив из 4–6 строк.\n"
        + _LLM_VIDEO_STAGES_JSON_RULE
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt)
    return _parse_video_stages_from_llm(raw)


async def merge_recipe_steps(recipe_steps: list[str]) -> list[str]:
    """
    Сокращает число шагов до 4–6, объединяя близкие действия без потери ключевых этапов.

    Вызывается, когда шагов больше 6.
    """
    prompt = (
        "Тебе дан рецепт с избыточным количеством шагов.\n\n"
        f"ТЕКУЩИЕ ШАГИ:\n{json.dumps(recipe_steps, ensure_ascii=False)}\n\n"
        "ЗАДАЧА:\n"
        "- сократить до 4-6 шагов\n"
        "- объединить логически близкие действия\n\n"
        "ПРАВИЛА:\n"
        "- не терять ключевые этапы\n"
        "- сохранить понятность\n\n"
        + _RUSSIAN_YO_RULE
        + "\n"
        + _LLM_RECIPE_STEPS_QUALITY_RULE
        + "\n"
        + _LLM_RECIPE_STEPS_COUNT_RULE
        + "\n"
        "Верни JSON-массив из 4–6 строк.\n"
        + _LLM_VIDEO_STAGES_JSON_RULE
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt)
    return _parse_video_stages_from_llm(raw)


def _ensure_scene_block_len(
    scenes: list[dict[str, Any]],
    *,
    min_n: int = 3,
    max_n: int = 6,
) -> list[dict[str, Any]]:
    """Подгоняет число сцен к допустимому диапазону (как правило 3–6)."""
    if not scenes:
        return scenes
    out = [dict(s) for s in scenes]
    while len(out) < min_n:
        out.append(dict(out[-1]))
    return out[:max_n]


def _coerce_llm_scene_items_to_objects(raw_items: list[Any], *, concept: str) -> list[dict[str, Any]]:
    """
    Adapts loose LLM scene arrays before strict pipeline normalization.

    Gemini sometimes regresses from the scene-object contract to a JSON array of
    strings. Treat those strings as minimal scene drafts instead of surfacing a
    technical validation error to the user.
    """
    out: list[dict[str, Any]] = []
    coerced: list[str] = []
    for idx, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            out.append(dict(item))
            continue
        if isinstance(item, list):
            text = " ".join(str(part).strip() for part in item if str(part).strip())
        else:
            text = str(item or "").strip()
        if not text:
            text = f"{concept}: кулинарный шаг {idx}".strip(": ")
        out.append(
            {
                "scene": idx,
                "shot_type": "closeup",
                "characters": ["grandma", "cat"],
                "action": text,
                "text": text,
                "recipe_step": text,
                "dialogue_mode": "single",
                "dialogue": [],
            }
        )
        coerced.append(f"#{idx}: {type(item).__name__}")
    if coerced:
        logger.warning(
            "coerced non-object scene items from LLM output: %s",
            ", ".join(coerced[:10]),
        )
    return out


def _bind_recipe_steps(scenes: list[dict[str, Any]], recipe_steps: list[str]) -> list[dict[str, Any]]:
    """
    Привязывает канонический шаг к сцене по индексу.

    Args:
        scenes: Сцены после генерации/нормализации.
        recipe_steps: Шаги рецепта в порядке сцен.
    Returns:
        Новый список сцен, где у каждой сцены (если индекс доступен) заполнено поле `recipe_step`.
    """
    out = [dict(s) for s in scenes]
    for i, step in enumerate(recipe_steps):
        if i < len(out):
            out[i]["recipe_step"] = step
    return out


def _postprocess_scenes_output(
    scenes: list[dict[str, Any]],
    concept: str,
    *,
    viral_concept_brief: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Минимальная стыковка с ботом: ``recipe_step`` дублирует реплику ``text`` из ответа модели.
    """
    _ = concept
    out = [dict(s) for s in scenes]
    for idx, scene in enumerate(out, start=1):
        _normalize_scene_dialogue(scene, scene_index=idx)
        t = normalize_speech_phrase_for_voice(str(scene.get("text", "")).strip())
        if t:
            scene["text"] = t
            scene["recipe_step"] = t
    _apply_family_cast_rules(out, viral_concept_brief=viral_concept_brief)
    _ensure_final_scene_subscribe_cta(out)
    return out


def _scene_optional_family_characters() -> tuple[str, ...]:
    return ("grandfather", "grandson", "granddaughter")


def _family_character_references(scene: dict[str, Any]) -> None:
    refs_raw = scene.get("references")
    refs = dict(refs_raw) if isinstance(refs_raw, dict) else {}
    chars = scene.get("characters") if isinstance(scene.get("characters"), list) else []
    allowed = {str(ch).strip().lower() for ch in chars if str(ch).strip()}
    for key in _scene_optional_family_characters():
        if key not in allowed:
            refs.pop(key, None)
    for key in allowed:
        base = ((ASSETS.get("characters") or {}).get(key) or {}).get("base")
        if isinstance(base, str) and base.strip():
            refs.setdefault(key, base.strip())
    scene["references"] = refs


def _pick_family_child_character(
    scenes: list[dict[str, Any]],
    brief: dict[str, Any],
) -> str:
    for scene in scenes:
        chars = scene.get("characters") if isinstance(scene.get("characters"), list) else []
        normalized = [str(ch).strip().lower() for ch in chars if str(ch).strip()]
        for key in ("grandson", "granddaughter"):
            if key in normalized:
                return key
        dialogue = scene.get("dialogue") if isinstance(scene.get("dialogue"), list) else []
        for item in dialogue:
            if not isinstance(item, dict):
                continue
            speaker = _normalize_dialogue_speaker(item.get("speaker"))
            if speaker in {"grandson", "granddaughter"}:
                return speaker
    family_plan = str(brief.get("family_cast_plan") or "").lower()
    if "внуч" in family_plan or "granddaughter" in family_plan:
        return "granddaughter"
    return "grandson"


def _apply_family_cast_rules(
    scenes: list[dict[str, Any]],
    *,
    viral_concept_brief: dict[str, Any] | None = None,
) -> None:
    if not scenes:
        return
    brief = viral_concept_brief if isinstance(viral_concept_brief, dict) else {}
    audience_type = str(brief.get("dish_audience_type") or "neutral").strip().lower()
    optional_family = set(_scene_optional_family_characters())
    selected_child = _pick_family_child_character(scenes, brief) if audience_type == "child" else ""
    total = len(scenes)
    for idx, scene in enumerate(scenes, start=1):
        scene["dish_audience_type"] = audience_type or "neutral"
        chars = scene.get("characters")
        if not isinstance(chars, list):
            chars = []
        normalized = [str(ch).strip().lower() for ch in chars if str(ch).strip()]
        keep = [ch for ch in normalized if ch not in optional_family]
        if "grandma" not in keep:
            keep.append("grandma")
        if "cat" not in keep:
            keep.append("cat")
        if audience_type == "child" and idx in {1, total} and selected_child:
            keep.append(selected_child)
        elif audience_type == "male" and idx in {1, total}:
            keep.append("grandfather")
        deduped: list[str] = []
        for ch in keep:
            if ch not in deduped:
                deduped.append(ch)
        scene["characters"] = deduped
        _family_character_references(scene)


def _speech_without_pause_and_punctuation(text: str) -> str:
    text = re.sub(r"\[\s*пауза\s*\]", " ", text or "", flags=re.IGNORECASE)
    text = re.sub(r"[.,!?;:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _ensure_final_scene_subscribe_cta(scenes: list[dict[str, Any]]) -> None:
    """Гарантирует подписочную фразу в конце последней сцены."""
    _ = scenes
    return


def _normalize_end_frame_prompt_for_stable_generation(_value: Any) -> str:
    """Preserve explicit end-frame deltas while normalizing empty placeholders."""
    value = str(_value or "").strip()
    if not value:
        return "пусто"
    if value.lower() == "пусто":
        return "пусто"
    return value


def _end_frame_prompt_requests_generation(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.lower() != "пусто"


def _ensure_final_scene_subscribe_cta_legacy(scenes: list[dict[str, Any]]) -> None:
    """Legacy final CTA injection kept for reference; not used in the stable flow."""
    if not scenes:
        return
    scene = scenes[-1]
    text = str(scene.get("text", "") or "").strip()
    model = str(scene.get("model", "") or "").lower()
    cta = FINAL_SCENE_SUBSCRIBE_CTA_VEO3 if model.startswith("veo3") else FINAL_SCENE_SUBSCRIBE_CTA
    cta_key = _speech_without_pause_and_punctuation(FINAL_SCENE_SUBSCRIBE_CTA)
    text_key = _speech_without_pause_and_punctuation(text)
    if cta_key and cta_key in text_key:
        if not _speech_without_pause_and_punctuation(text).endswith(cta_key):
            text = re.sub(re.escape(FINAL_SCENE_SUBSCRIBE_CTA), "", text, flags=re.IGNORECASE).strip()
            text = re.sub(re.escape(FINAL_SCENE_SUBSCRIBE_CTA_VEO3), "", text, flags=re.IGNORECASE).strip()
        else:
            scene["recipe_step"] = text
            return
    if text:
        sep = " [пауза] " if model.startswith("veo3") else " "
        text = f"{text}{sep}{cta}".strip()
    else:
        text = cta
    if model.startswith("veo3"):
        scene["duration_sec"] = 8
    scene["text"] = text
    scene["recipe_step"] = text


def _humanize_scene_errors(errors: list[str]) -> list[str]:
    mapping = {
        "banned_abstraction": "абстрактные формулировки в сценах",
        "step_not_reflected": "сцена не отражает шаг рецепта",
        "recipe_steps_count_mismatch": "число сцен не совпадает с числом шагов",
        "no_speech": "нет реплики бабушки",
        "no_step_explanation": "нет объяснения шага",
        "no_cat": "в сцене нет кота",
        "no_food": "не видно еды/ингредиентов",
        "invalid_duration_for_veo3": "неверная длительность для veo3",
        "invalid_duration_for_non_veo": "неверная длительность для non-veo",
        "stove_burner_missing": "для плиты не указана конфорка",
        "cut_style_missing": "для нарезки не указан тип нарезки",
        "dish_description_with_ingredients_missing": "нет подробной подачи блюда с ингредиентами",
        "low_angle_forbidden": "запрещен ракурс снизу",
        "no_location_or_shot_change": "новая сцена не меняет ракурс или предметный фокус",
        "veo3_text_has_punctuation": "в реплике veo3 есть знаки препинания",
        "veo3_text_has_non_vowel_uppercase": "в реплике veo3 заглавные не только на ударных гласных",
        "veo3_text_no_stress_marks": "в реплике veo3 нет ударных гласных",
        "veo3_text_no_pause_token": "в реплике veo3 нет [пауза]",
        "first_scene_hook_missing": "в первой сцене нет хука",
        "first_scene_dish_name_missing": "в первой сцене не названо, что готовим",
        "first_scene_boring_preparation": "первая сцена начинается со скучной подготовки",
        "final_presentation_missing": "в финальной сцене нет презентации блюда",
        "final_payoff_missing": "в финальной сцене нет сильного визуального payoff",
    }
    out: list[str] = []
    for e in errors:
        if ":" in e:
            head, tail = e.split(":", 1)
            code = tail.strip()
            if head.startswith("scene_"):
                try:
                    sn = int(head.replace("scene_", ""))
                    human = mapping.get(code, code)
                    out.append(f"сцена {sn}: {human}")
                    continue
                except ValueError:
                    pass
            out.append(mapping.get(code, e))
        else:
            out.append(mapping.get(e, e))
    uniq: list[str] = []
    for x in out:
        if x not in uniq:
            uniq.append(x)
    return uniq


async def generate_scenes_plan(
    concept: str,
    *,
    full_recipe_text: str,
    user_feedback: str | None = None,
    previous_scenes: list[dict[str, Any]] | None = None,
    on_attempt_failure: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    viral_concept_brief: dict[str, Any] | None = None,
    sponsored_content: bool = False,
    brand_context: str | None = None,
    analytics_feedback: dict[str, Any] | None = None,
    romantic_drama_subplot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Один запрос к Gemini: сценарий из 3–6 сцен по **полному тексту рецепта**, с репликами Veo3 в ``text``.

    Кэш: (concept, full_recipe_text, feedback). Правки — через ``user_feedback`` и ``previous_scenes`` в промпте
    (см. ``pipeline_llm_steps``), без циклов валидации/fix в Python.
    """
    body = (full_recipe_text or "").strip()
    if not body:
        raise ValueError("Пустой full_recipe_text для сценария.")
    concept = concept.strip()
    fb = (user_feedback or "").strip()
    has_previous_scenes = bool(previous_scenes)
    cached = None if has_previous_scenes else get_scenes_plan(concept, body, fb or None)
    if cached is not None:
        if isinstance(cached, dict):
            cached_plan = cached.get("json_plan")
            if isinstance(cached_plan, list):
                fixed = _postprocess_scenes_output(
                    cached_plan,
                    concept,
                    viral_concept_brief=viral_concept_brief,
                )
                fixed, retention_review = enrich_scenes_with_retention(
                    fixed,
                    viral_concept_brief=viral_concept_brief,
                    sponsored_content=sponsored_content,
                )
                romantic_subplot = _romantic_subplot_from_scenes(cached.get("romantic_drama_subplot"), fixed)
                _enforce_romantic_subplot_characters(fixed, romantic_subplot)
                return {
                    "text_plan": (
                        format_romantic_drama_status(romantic_subplot, fixed)
                        + "\n\n"
                        + build_human_readable_plan(fixed)
                    ),
                    "json_plan": fixed,
                    "viral_concept_brief": viral_concept_brief or cached.get("viral_concept_brief") or {},
                    "romantic_drama_subplot": romantic_subplot,
                    "retention_review": retention_review,
                    "regeneration_events": cached.get("regeneration_events") or [],
                }
        return cached

    payload = llm_steps.build_scenes_plan_payload(
        concept,
        body,
        fb,
        _strip_location_context_for_llm(list(previous_scenes or [])),
        viral_concept_brief=viral_concept_brief,
        sponsored_content=sponsored_content,
        brand_context=brand_context,
        analytics_feedback=analytics_feedback,
        romantic_drama_subplot=romantic_drama_subplot,
    )
    prompt = llm_steps.build_scenes_plan_prompt(payload)

    async def _emit_failure(payload_cb: dict[str, Any]) -> None:
        if on_attempt_failure is None:
            return
        try:
            await on_attempt_failure(payload_cb)
        except Exception as cb_err:
            logger.warning("on_attempt_failure callback raised: %s", cb_err)

    try:
        raw_once = await llm_steps.gemini_text(prompt)
    except Exception as e:
        logger.error("Ошибка при генерации сцен: %s", e)
        await _emit_failure(
            {
                "attempt": 1,
                "max_attempts": 1,
                "phase": "llm_error",
                "errors_human": [str(e).replace("\n", " ")[:700]],
                "errors_raw": [],
                "problem_scene": None,
                "continuing": False,
                "fix_exception": str(e),
            }
        )
        raise

    arr = normalize_scenes_for_pipeline(
        _coerce_llm_scene_items_to_objects(extract_json_array(raw_once), concept=concept)
    )
    arr = _ensure_scene_block_len(arr)
    raw_scenes = _postprocess_scenes_output(
        arr,
        concept,
        viral_concept_brief=viral_concept_brief,
    )
    raw_scenes, retention_review = enrich_scenes_with_retention(
        raw_scenes,
        viral_concept_brief=viral_concept_brief,
        sponsored_content=sponsored_content,
    )
    romantic_subplot = _romantic_subplot_from_scenes(payload.get("romantic_drama_subplot"), raw_scenes)
    _enforce_romantic_subplot_characters(raw_scenes, romantic_subplot)
    out_raw = {
        "text_plan": format_romantic_drama_status(romantic_subplot, raw_scenes)
        + "\n\n"
        + build_human_readable_plan(raw_scenes),
        "json_plan": raw_scenes,
        "viral_concept_brief": viral_concept_brief or {},
        "romantic_drama_subplot": romantic_subplot,
        "retention_review": retention_review,
        "regeneration_events": [],
    }
    if not has_previous_scenes:
        set_scenes_plan(concept, body, fb or None, out_raw)
    return out_raw


def _normalize_frame_prompts(
    scenes_plan: list[dict[str, Any]], raw: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Сводит ответ LLM по кадрам к единому списку объектов на апрув.

    Для каждой сцены из ``scenes_plan`` берёт пару ``start_frame_prompt_ru`` / ``end_frame_prompt_ru``
    из соответствующего элемента ``raw`` и дополняет метаданными сцены (номер, shot_type, model, duration, continuity).

    Args:
        scenes_plan: План сцен (источник полей сцены).
        raw: Массив объектов от модели в том же порядке, что и сцены.

    Returns:
        Список словарей, готовых для ``format_frame_prompts_for_approval`` / сохранения в сессии.

    Raises:
        RuntimeError: Если число элементов не совпадает с числом сцен или элемент не объект.
    """
    if len(raw) != len(scenes_plan):
        raise RuntimeError("Frame prompts count mismatch with scenes.")
    out: list[dict[str, Any]] = []
    for scene_index, (scene, item) in enumerate(zip(scenes_plan, raw), start=1):
        if not isinstance(item, dict):
            raise RuntimeError("Frame prompt item is not an object.")
        out.append(
            {
                "scene": scene.get("scene"),
                "shot_type": scene.get("shot_type", "medium"),
                "model": scene.get("model", "veo3_fast"),
                "duration_sec": scene.get("duration_sec", 4),
                "continuity_anchors": scene.get("continuity_anchors") or [],
                "continuity_state": scene.get("continuity_state") or "",
                "characters": scene.get("characters") or [],
                "romantic_drama_beat": scene.get("romantic_drama_beat") or "",
                "start_frame_prompt_ru": _ensure_image_prompt_framing_requirements(
                    append_visual_prop_descriptions(
                        _append_romantic_drama_to_start_prompt(
                            str(item.get("start_frame_prompt_ru", "пусто")).strip() or "пусто",
                            scene,
                        )
                    ),
                    model=str(scene.get("model", "")),
                    title_safe_top=scene_index == 1,
                ),
                "end_frame_prompt_ru": _ensure_image_prompt_framing_requirements(
                    append_visual_prop_descriptions(
                        sanitize_character_names_and_keys(
                            _normalize_end_frame_prompt_for_stable_generation(item.get("end_frame_prompt_ru"))
                        )
                    ),
                    model=str(scene.get("model", "")),
                ),
            }
        )
    return out


def _normalize_production_prompts(
    prompts: Any,
    *,
    scenes_count: int,
    require_frames: bool,
) -> list[dict[str, str]]:
    """
    Нормализует ответы модели для блока `prompts` в production.

    Поддерживает как ожидаемый формат (list[dict]), так и частый деградировавший
    формат list[str], где строка — это только `video_prompt_en`.
    """
    if not isinstance(prompts, list) or len(prompts) != scenes_count:
        raise RuntimeError("Модель вернула неверный формат production (prompts).")
    out: list[dict[str, str]] = []
    for item in prompts:
        if isinstance(item, dict):
            out.append(
                {
                    "start_frame_prompt_ru": str(item.get("start_frame_prompt_ru", "")),
                    "end_frame_prompt_ru": str(item.get("end_frame_prompt_ru", "")),
                    "video_prompt_en": str(item.get("video_prompt_en", "")),
                }
            )
            continue
        if isinstance(item, str):
            out.append(
                {
                    "start_frame_prompt_ru": "",
                    "end_frame_prompt_ru": "",
                    "video_prompt_en": item.strip(),
                }
            )
            continue
        raise RuntimeError("Элемент prompts не объект и не строка.")
    if require_frames:
        for p in out:
            if not p["start_frame_prompt_ru"].strip() and not p["end_frame_prompt_ru"].strip():
                # Не валим пайплайн: оставляем пусто, чтобы кадры можно было уточнить вручную.
                p["start_frame_prompt_ru"] = "пусто"
                p["end_frame_prompt_ru"] = "пусто"
    return out


def _production_prompts_from_container(value: Any) -> Any:
    """Возвращает массив production prompts из частых вариантов обёртки LLM-ответа."""
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return None
    direct = value.get("prompts")
    if isinstance(direct, list):
        return direct
    production = value.get("production")
    if isinstance(production, dict) and isinstance(production.get("prompts"), list):
        return production.get("prompts")
    bundle = value.get("production_bundle")
    if isinstance(bundle, dict) and isinstance(bundle.get("prompts"), list):
        return bundle.get("prompts")
    return None


def _extract_production_prompts(raw: str) -> Any:
    """
    Достаёт `prompts` из production-ответа модели.

    Основной контракт — объект `{"prompts": [...]}`, но LLM иногда возвращает массив
    напрямую или кладёт массив под `production.prompts`.
    """
    cleaned = re.sub(
        r"\s*```$",
        "",
        re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE),
    )
    try:
        parsed = json.loads(cleaned)
    except Exception:
        parsed = None
    prompts = _production_prompts_from_container(parsed)
    if prompts is not None:
        return prompts
    try:
        obj = extract_json_object(raw)
    except Exception:
        obj = None
    prompts = _production_prompts_from_container(obj)
    if prompts is not None:
        return prompts
    try:
        return extract_json_array(raw)
    except Exception as e:
        raise RuntimeError("Модель вернула неверный формат production (prompts).") from e


def _fallback_start_frame_prompt_from_scene(scene: dict[str, Any]) -> str:
    """Builds a minimal deterministic start-frame prompt when production JSON is invalid."""
    shot = str(scene.get("shot_type") or "medium-close").strip()
    action = sanitize_character_names_and_keys(
        str(scene.get("action") or scene.get("recipe_step") or scene.get("text") or "бабушка готовит блюдо").strip()
    )
    anchors = scene.get("continuity_anchors") if isinstance(scene.get("continuity_anchors"), list) else []
    anchors_text = ", ".join(
        sanitize_character_names_and_keys(str(item).strip())
        for item in anchors
        if str(item).strip()
    )
    continuity_state = sanitize_character_names_and_keys(str(scene.get("continuity_state") or "").strip())
    parts = [
        f"Крупный или средне-крупный план, ракурс {shot}.",
        "Бабушка и кот полностью видны в кадре.",
        action.rstrip(".") + ".",
    ]
    if anchors_text:
        parts.append(f"Сохранить опоры консистентности: {anchors_text}.")
    if continuity_state:
        parts.append(f"Текущее состояние сцены: {continuity_state}.")
    return " ".join(parts).strip()


def _fallback_video_prompt_from_scene(scene: dict[str, Any]) -> str:
    """Builds a minimal deterministic video prompt when production JSON is invalid."""
    duration = int(scene.get("duration_sec") or 4)
    action = sanitize_character_names_and_keys(
        str(scene.get("action") or scene.get("recipe_step") or scene.get("text") or "grandma cooks the dish").strip()
    )
    anchors = scene.get("continuity_anchors") if isinstance(scene.get("continuity_anchors"), list) else []
    anchors_text = ", ".join(
        sanitize_character_names_and_keys(str(item).strip())
        for item in anchors
        if str(item).strip()
    )
    continuity_state = sanitize_character_names_and_keys(str(scene.get("continuity_state") or "").strip())
    prompt = (
        f"{duration}-second cozy cartoon cooking clip, close-up to medium-close framing, "
        f"grandmother and cartoon cat fully visible, gentle camera evolution, accurate lip sync, "
        f"no on-screen text, no subtitles, no background music. Main action: {action}."
    )
    if anchors_text:
        prompt += f" Keep continuity with: {anchors_text}."
    if continuity_state:
        prompt += f" Food and props state: {continuity_state}."
    return prompt.strip()


def _fallback_production_prompts(
    scenes: list[dict[str, Any]],
    *,
    require_frames: bool,
) -> list[dict[str, str]]:
    """Returns a safe production fallback so invalid LLM format does not break the session."""
    out: list[dict[str, str]] = []
    for scene in scenes:
        out.append(
            {
                "start_frame_prompt_ru": _fallback_start_frame_prompt_from_scene(scene) if require_frames else "",
                "end_frame_prompt_ru": "пусто" if require_frames else "",
                "video_prompt_en": _fallback_video_prompt_from_scene(scene),
            }
        )
    return out


async def generate_frame_prompts_for_approval(
    scenes_plan: list[dict[str, Any]],
    user_feedback: str | None = None,
    previous_frame_prompts: list[dict[str, Any]] | None = None,
    requested_end_frame_scenes: list[int] | set[int] | tuple[int, ...] | None = None,
    reference_images: list[bytes] | None = None,
) -> dict[str, Any]:
    """
    Отдельный этап перед production: готовит и возвращает START/END frame промпты на апрув.
    """
    payload = {
        "scenes": _strip_location_context_for_llm(scenes_plan),
        "feedback": (user_feedback or "").strip(),
        "previous_frame_prompts": _strip_location_context_for_llm(previous_frame_prompts or []),
        "requested_end_frame_scenes": sorted(
            int(value)
            for value in (requested_end_frame_scenes or [])
            if str(value).isdigit() and 1 <= int(value) <= len(scenes_plan)
        ),
        "user_reference_images_count": len(
            [image for image in (reference_images or []) if isinstance(image, bytes) and image]
        ),
        "rules": {
            "start_frame_ru": (
                "детально и самодостаточно описывает кадр ДО движения; "
                "обязательно явно указывает угол/ракурс камеры (например: сверху, 3/4, уровень глаз, низкий ракурс); "
                "обязательно включает способ нарезки, описание блюда/текущего состояния блюда и подробные описания ингредиентов"
            ),
            "end_frame_ru": (
                "верни end_frame_prompt_ru только для сцен из requested_end_frame_scenes; "
                "для всех остальных сцен строго 'пусто'. Если не 'пусто': только ДЕЛЬТА относительно старта — что изменилось, кто вошёл в кадр, "
                "куда сдвинулись руки/еда; ЗАПРЕЩЕНО описывать весь кадр с нуля."
            ),
            "same_view_can_use_empty_start": True,
            "style": "style must follow provided references",
            "user_reference_images_rule": (
                "if user_reference_images_count > 0, use the attached user reference images as visual context "
                "for the frame prompts: preserve relevant objects, packaging, composition, texture, color palette "
                "or dish appearance requested by the user, while still obeying every scene requirement"
            ),
            "ingredients_rule": "если в кадре есть еда, явно перечисляй конкретные ингредиенты",
            "labels_rule": "если есть надписи в кадре, указывай: все надписи на русском",
            "title_safe_scene1_rule": TITLE_SAFE_FIRST_SCENE_START_FRAME_RULE,
            "fruit_only_first_moment_rule": FRUIT_ONLY_FIRST_MOMENT_RULE,
            "continuity_rule": (
                "repeat and update continuity_anchors in every start/video prompt: same cookware, plate, "
                "cutting board, stovetop surface, utensils, ingredient state, food texture and cat position "
                "whenever they still belong to the recipe moment"
            ),
            "romantic_drama_rule": (
                "If scene.romantic_drama_beat is non-empty, every start_frame_prompt_ru must explicitly describe "
                "that background drama beat as visible action in the frame, using concrete pose, distance, gaze, "
                "blocking or object interaction rather than abstract labels. Do not leave subplot characters only "
                "in scene.characters; if a subplot character is listed, they must be described in the frame."
            ),
            "no_text_overlay": True,
        },
    }
    prompt = (
        "Return ONLY JSON array. One item per scene, same order.\n"
        "Each item must have keys: start_frame_prompt_ru, end_frame_prompt_ru.\n"
        "START FRAME: detailed static pre-action frame in Russian.\n"
        + _RUSSIAN_YO_RULE
        + "\n"
        "START FRAME must explicitly include camera angle/viewpoint in words (required).\n"
        "END FRAME is opt-in only. Return end_frame_prompt_ru only for scene numbers in requested_end_frame_scenes; "
        "for every other scene return the Russian word 'пусто', even if the final beat differs visually. "
        "When end_frame_prompt_ru is used, write ONLY the delta vs start (what changed, who entered, what progressed) — never a full scene description from scratch.\n"
        "START FRAME must be self-contained and maximally specific: props, cookware, working surface, character pose, food details.\n"
        "START FRAME must include cutting/slicing method when relevant, dish/current dish-state description, and detailed ingredient descriptions: shape, color, texture, processing state.\n"
        f"START FRAME Russian mandatory rule: {START_FRAME_RECIPE_VISUAL_DETAIL_RULE}\n"
        f"SCENE 1 title-safe mandatory rule: {TITLE_SAFE_FIRST_SCENE_START_FRAME_RULE}\n"
        f"SCENE 1 fruit-only first-moment exception: {FRUIT_ONLY_FIRST_MOMENT_RULE}\n"
        "If food is present, explicitly list concrete ingredients.\n"
        "If any labels/signs/text appear in frame, explicitly state: all labels are in Russian.\n"
        "Use reference style; do not invent an explicit art style.\n"
        "If user reference images are attached, inspect them and fold their relevant visual details into "
        "start_frame_prompt_ru/end_frame_prompt_ru.\n"
        "Do not use location names or location references. Preserve continuity through continuity_anchors and continuity_state.\n"
        "Do not reference other scenes; each prompt must be independent.\n"
        "If a scene contains romantic_drama_beat, include it visibly and clearly in start_frame_prompt_ru for that exact scene.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    image_payloads = [image for image in (reference_images or []) if isinstance(image, bytes) and image]
    raw = await asyncio.to_thread(
        gemini_generate_sync,
        prompt,
        images=image_payloads or None,
    )
    arr = extract_json_array(raw)
    normalized = _normalize_frame_prompts(scenes_plan, arr)
    requested_end_frames = {
        int(value)
        for value in (requested_end_frame_scenes or [])
        if str(value).isdigit() and 1 <= int(value) <= len(normalized)
    }
    for scene_index, item in enumerate(normalized, start=1):
        if scene_index not in requested_end_frames:
            item["end_frame_prompt_ru"] = "пусто"
    return {"text_plan": format_frame_prompts_for_approval(normalized), "json_plan": normalized}


async def generate_full_production(
    scenes_plan: list[dict[str, Any]],
    approved_frame_prompts: list[dict[str, Any]] | None = None,
    *,
    user_feedback: str | None = None,
    previous_enriched_scenes: list[dict[str, Any]] | None = None,
    requested_end_frame_scenes: list[int] | set[int] | tuple[int, ...] | None = None,
    viral_concept_brief: dict[str, Any] | None = None,
    sponsored_content: bool = False,
    brand_context: str | None = None,
) -> dict[str, Any]:
    """
    Сборка промптов для медиа по сценам.

    Если ``approved_frame_prompts`` задан — start/end берутся оттуда, LLM генерирует только ``video_prompt_en``.
    Если ``None`` — один вызов LLM: ``start_frame_prompt_ru``, ``end_frame_prompt_ru`` и ``video_prompt_en`` сразу
    (без отдельного UX-апрува только кадров).

    При ``user_feedback`` и ``previous_enriched_scenes`` — перегенерация video prompts с учётом правок (ветка с
    зафиксированными кадрами).
    """
    scenes = [dict(scene) for scene in scenes_plan]
    requested_end_frames = {
        int(value)
        for value in (requested_end_frame_scenes or [])
        if str(value).isdigit() and 1 <= int(value) <= len(scenes)
    }
    viral_brief = viral_concept_brief if isinstance(viral_concept_brief, dict) else {}
    target_languages = target_languages_from_env()
    if approved_frame_prompts is not None:
        if len(approved_frame_prompts) != len(scenes):
            raise RuntimeError("Approved frame prompts count mismatch.")
        for i, scene in enumerate(scenes):
            p = approved_frame_prompts[i]
            scene_number = i + 1
            end_frame_prompt = (
                _normalize_end_frame_prompt_for_stable_generation(p.get("end_frame_prompt_ru"))
                if scene_number in requested_end_frames
                else "пусто"
            )
            scene["start_frame_prompt"] = _ensure_image_prompt_framing_requirements(
                append_visual_prop_descriptions(
                    _append_romantic_drama_to_start_prompt(str(p.get("start_frame_prompt_ru", "пусто")), scene)
                ),
                model=str(scene.get("model", "")),
                title_safe_top=scene_number == 1,
            )
            scene["end_frame_prompt"] = _ensure_image_prompt_framing_requirements(
                append_visual_prop_descriptions(
                    sanitize_character_names_and_keys(
                        end_frame_prompt
                    )
                ),
                model=str(scene.get("model", "")),
            )
            scene["end_frame_requested"] = _end_frame_prompt_requests_generation(end_frame_prompt)

        payload = {
            "scenes_in_order": _strip_location_context_for_llm(scenes),
            "instructions": {
                "video_prompt_en": (
                    "include exact clip duration, reference style, no on-screen text or subtitles; "
                    "no background music or soundtrack; "
                    "strictly non-photorealistic/non-realistic cartoon visuals; "
                    "grandmother must be a cartoon grandmother and the cat must be a cartoon cat; "
                    "include the exact phrase cartoon cat; "
                    "obey kitchen physics: cookware and appliances must be functional and physically plausible; "
                    "do not invent extra characters, extra animals, extra hands, or unrelated parallel actions; "
                    "close-up with full grandma + all characters visible, lip sync, camera angle evolves "
                    "(not a single fixed static angle for the whole clip)"
                ),
                "if_speech_add_lipsync": True,
                "characters_speaks_rule": _LLM_VIDEO_CHARACTERS_SPEAKS_RULE,
                "scene1_spoken_dish_name_rule": (
                    "For scene 1, video_prompt_en must place CHARACTERS SPEAKS early in the clip and the exact "
                    "spoken phrase from scene.text must explicitly name the dish from concept/full_recipe_text."
                ),
                "scene1_title_safe_rule": "Title-safe top area applies only to start_frame_prompt_ru, never to video_prompt_en.",
                "fruit_only_first_moment_rule": FRUIT_ONLY_FIRST_MOMENT_RULE,
                "veo3_framing_rule": VEO3_FAST_VIDEO_FRAMING_RULE,
                "continuity_rule": (
                    "video_prompt_en must preserve continuity_anchors and continuity_state: same cookware, "
                    "plate, cutting board, stovetop/working surface, utensils, ingredient state and food texture"
                ),
                "romantic_drama_rule": (
                    "If scene.romantic_drama_beat is non-empty, video_prompt_en must explicitly include that background "
                    "drama beat as visible action throughout the clip, using concrete staging and behavior rather than "
                    "abstract labels. Do not leave subplot characters only in scene.characters."
                ),
                "kitchen_physics_rule": VIDEO_KITCHEN_PHYSICS_RULE,
                "viral_retention_rule": (
                    "Respect retention_function and micro_events. Avoid empty long actions. "
                    "Scene 1 must be hook-first and readable from the first frame. "
                    "The final scene must strongly show payoff_type."
                ),
                "viral_concept_brief": viral_brief,
                "sponsored_content": bool(sponsored_content),
                "brand_context": brand_context or "",
                "end_frame_rule": (
                    "Return end_frame_prompt_ru only for scene numbers listed in requested_end_frame_scenes. "
                    "For every other scene return the Russian word 'пусто'. The end frame is a delta vs start."
                ),
            },
            "requested_end_frame_scenes": sorted(requested_end_frames),
        }
        fb = (user_feedback or "").strip()
        prev_list = previous_enriched_scenes or []
        feedback_block = ""
        if fb:
            feedback_block += (
                "\n\nФИДБЕК ПОЛЬЗОВАТЕЛЯ (перепиши video_prompt_en и, только если явно запрошено, end_frame_prompt_ru; порядок сцен не меняй):\n"
                f"{fb}\n"
            )
        if isinstance(prev_list, list) and prev_list and len(prev_list) == len(scenes):
            feedback_block += (
                "\n\nПРЕДЫДУЩИЕ video_prompt_en по сценам (доработай с учётом фидбека):\n"
                + json.dumps([str(s.get("video_prompt", "") or "") for s in prev_list], ensure_ascii=False)
            )
        prompt = (
            "Return ONLY JSON object with key \"prompts\" — array of objects, one per scene in order.\n"
            "Each object: video_prompt_en, end_frame_prompt_ru.\n"
            "END FRAME is opt-in only. Return a delta end_frame_prompt_ru only for scene numbers in "
            "requested_end_frame_scenes; return `пусто` for every other scene. Never infer an end frame from action, "
            "payoff, transformation, or a visually different final beat.\n"
            "VIDEO PROMPT must include clip duration, reference style, no on-screen text or subtitles, and no background music/soundtrack. "
            "VIDEO PROMPT must be action-first: describe characters' actions, speech/lip sync, voice lock, and useful motion beats; "
            "do not spend prompt budget on static layout, relative placement, room/background inventory, or title-safe composition zones. "
            "Visual style must be non-photorealistic/non-realistic cartoon (no realism). "
            "Grandmother must be a cartoon grandmother and the cat must be a cartoon cat; include the exact phrase cartoon cat. "
            "Kitchen physics must stay plausible: cookware belongs on a visible burner or hob when cooking; "
            "blenders must have jar, base, and lid; mixers need beaters, bowl, and motor body. "
            "Do not invent extra characters, duplicate animals, extra hands, or unrelated side actions. "
            "If scene has speech, require accurate lip sync with the Russian line. "
            "For every Russian word copied into video_prompt_en, especially after CHARACTERS SPEAKS, use `ё` when Russian spelling requires `ё`, not `е`. "
            "For scene 1, the spoken phrase in CHARACTERS SPEAKS must happen at the start of the clip and explicitly "
            "name the dish from the scenario; the viewer must hear the dish name in the first scene. "
            "Do not include title_safe_top_30 or top-30-percent overlay instructions in video_prompt_en; that rule is only for start_frame_prompt_ru. "
            f"Scene 1 fruit-only first-moment exception: {FRUIT_ONLY_FIRST_MOMENT_RULE} "
            "Framing: close-up / medium-close; grandmother's full figure and every other character in the scene "
            "must stay fully visible; camera angle or framing should evolve during the clip (no fixed static "
            "tripod for the entire take).\n"
            + _LLM_VIDEO_CHARACTERS_SPEAKS_RULE
            + "\n"
            + _RUSSIAN_YO_RULE
            + "\n"
            "If model is veo3* OR scene needs speech, include exact substring: voice_lock_full: "
            + GRANDMA_VOICE_LOCK_FULL
            + ".\n"
            "Also include this exact framing block verbatim: "
            + VEO3_FAST_VIDEO_FRAMING_RULE
            + "\n"
            "Use the cartoon-character and kitchen-physics rules when composing the prompt, but do not append bulky "
            "meta-rule labels like veo3_cartoon_character_rule or kitchen_physics_rule unless they naturally belong in the prompt.\n"
            "Do not mention location names or location references; preserve continuity through recurring props and food state.\n"
            "Respect the Viral Concept Brief and scene retention fields. First frame must not be boring preparation; "
            "final scene must deliver the visual payoff. If sponsored_content is true, integrate the brand naturally "
            "inside the cooking story, not as an ad break.\n"
            "If a scene contains romantic_drama_beat, video_prompt_en must show that beat as visible background action for that exact scene.\n"
            + feedback_block
            + "\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        raw = await asyncio.to_thread(gemini_generate_sync, prompt)
        try:
            prompts = _normalize_production_prompts(
                _extract_production_prompts(raw),
                scenes_count=len(scenes),
                require_frames=False,
            )
        except Exception:
            logger.exception("production prompts parse failed; using deterministic fallback", extra={"require_frames": False})
            prompts = _fallback_production_prompts(scenes, require_frames=False)
        enriched: list[dict[str, Any]] = []
        for scene_index, (scene, p) in enumerate(zip(scenes, prompts), start=1):
            scene_out = dict(scene)
            scene_out["model"] = "veo3_fast"
            scene_out["voice_provider"] = "none"
            if scene_index in requested_end_frames:
                candidate_end = _normalize_end_frame_prompt_for_stable_generation(p.get("end_frame_prompt_ru"))
                if candidate_end == "пусто":
                    candidate_end = _normalize_end_frame_prompt_for_stable_generation(
                        scene_out.get("end_frame_prompt")
                    )
                scene_out["end_frame_prompt"] = candidate_end
            else:
                scene_out["end_frame_prompt"] = "пусто"
            scene_out["end_frame_requested"] = _end_frame_prompt_requests_generation(
                scene_out["end_frame_prompt"]
            )
            dialogue_by_lang = await _build_scene_dialogue_by_lang(scene_out, target_languages)
            speech_by_lang = (
                _speech_by_lang_from_dialogue(dialogue_by_lang)
                if dialogue_by_lang
                else await _build_scene_speech_by_lang(str(scene_out.get("text", "")).strip(), target_languages)
            )
            speech_plan_by_lang, speech_cues_by_lang = _build_scene_speech_plan_by_lang(
                speech_by_lang,
                dialogue_by_lang=dialogue_by_lang or _dialogue_by_lang_for_scene(scene_out, speech_by_lang),
            )
            base_prompt = _finalize_scene_video_prompt(
                _append_first_scene_title_safe_video_rule(str(p.get("video_prompt_en", "")), scene_index=scene_index),
                scene_out,
            )
            scene_out["speech_by_lang"] = speech_by_lang
            scene_out["dialogue_by_lang"] = dialogue_by_lang
            scene_out["speech_plan_by_lang"] = speech_plan_by_lang
            scene_out["speech_cues_by_lang"] = speech_cues_by_lang
            scene_out["speech_budget_by_lang"] = _speech_budget_by_lang(speech_by_lang, scene_out.get("duration_sec"))
            scene_out["video_prompt_by_lang"] = {
                lang: append_prompt_timing_hints(
                    _replace_characters_speaks(base_prompt, speech),
                    speech_plan_by_lang.get(lang),
                    duration_sec=float(scene_out.get("duration_sec") or 0.0),
                )
                for lang, speech in speech_by_lang.items()
            }
            scene_out["video_results_by_lang"] = {}
            scene_out["video_approvals_by_lang"] = {lang: False for lang in speech_by_lang}
            scene_out["video_prompt"] = scene_out["video_prompt_by_lang"].get("ru") or base_prompt
            scene_out["text"] = speech_by_lang.get("ru") or str(scene_out.get("text", "")).strip()
            scene_out["tts"] = {
                "provider": "none",
                "note": "image-only iteration (no separate voice)",
            }
            enriched.append(scene_out)
        return {"json_plan": enriched}

    payload = {
            "scenes_in_order": _strip_location_context_for_llm(scenes),
        "instructions": {
            "start_frame_ru": (
                "очень детальный статичный кадр ДО действия, без движения; "
                "обязательно явно укажи угол/ракурс камеры; "
                "обязательно включи способ нарезки, описание блюда/текущего состояния блюда и подробные описания ингредиентов"
            ),
            "end_frame_ru": (
                "END frame is opt-in only. Return a delta vs start only for scene numbers listed in "
                "requested_end_frame_scenes; for every other scene return 'пусто'. Never infer an END frame "
                "from a final beat, payoff, transformation, or visible action."
            ),
            "video_prompt_en": (
                "include exact clip duration, reference style, no on-screen text or subtitles; "
                "no background music or soundtrack; "
                "strictly non-photorealistic/non-realistic cartoon visuals; "
                "grandmother must be a cartoon grandmother and the cat must be a cartoon cat; "
                "include the exact phrase cartoon cat; "
                "obey kitchen physics: cookware and appliances must be functional and physically plausible; "
                "do not invent extra characters, extra animals, extra hands, or unrelated parallel actions; "
                "close-up with full grandma + all characters visible, lip sync, camera angle evolves; "
                "spoken delivery should feel natural but efficient, and the final phrase should finish slightly before the clip ends "
                "(not a single fixed static angle for the whole clip)"
            ),
            "if_speech_add_lipsync": True,
            "characters_speaks_rule": _LLM_VIDEO_CHARACTERS_SPEAKS_RULE,
            "scene1_spoken_dish_name_rule": (
                "For scene 1, video_prompt_en must place CHARACTERS SPEAKS early in the clip and the exact "
                "spoken phrase from scene.text must explicitly name the dish from concept/full_recipe_text."
            ),
            "scene1_title_safe_rule": {
                "start_frame_ru": TITLE_SAFE_FIRST_SCENE_START_FRAME_RULE,
                "video_prompt_en": "Do not copy title-safe/top-30-percent overlay rules to video_prompt_en.",
            },
            "fruit_only_first_moment_rule": FRUIT_ONLY_FIRST_MOMENT_RULE,
            "veo3_framing_rule": VEO3_FAST_VIDEO_FRAMING_RULE,
                "continuity_rule": (
                    "start_frame_ru and video_prompt_en must preserve continuity_anchors and continuity_state: "
                    "same cookware, plate, cutting board, stovetop/working surface, utensils, ingredient state "
                    "and food texture"
            ),
            "romantic_drama_rule": (
                "If scene.romantic_drama_beat is non-empty, both start_frame_ru and video_prompt_en must explicitly "
                "include that background drama beat as visible action, using concrete staging and behavior rather than "
                "abstract labels. Do not leave subplot characters only in scene.characters."
            ),
            "kitchen_physics_rule": VIDEO_KITCHEN_PHYSICS_RULE,
            "viral_retention_rule": (
                "Respect retention_function and micro_events. Avoid empty long actions. "
                "Scene 1 must be hook-first and readable from the first frame. "
                "The final scene must strongly show payoff_type."
            ),
            "viral_concept_brief": viral_brief,
            "sponsored_content": bool(sponsored_content),
            "brand_context": brand_context or "",
            "requested_end_frame_scenes": sorted(requested_end_frames),
        },
    }
    prompt = (
        "Return ONLY JSON object with key \"prompts\" — array of objects, one per scene in order.\n"
        "Each object: start_frame_prompt_ru, end_frame_prompt_ru, video_prompt_en.\n"
        + _RUSSIAN_YO_RULE
        + "\n"
        "START FRAME must be static and highly detailed, and explicitly include camera angle/viewpoint. "
        "START FRAME must include cutting/slicing method when relevant, dish/current dish-state description, and detailed ingredient descriptions: shape, color, texture, processing state. "
        f"START FRAME Russian mandatory rule: {START_FRAME_RECIPE_VISUAL_DETAIL_RULE} "
        f"SCENE 1 title-safe mandatory rule: {TITLE_SAFE_FIRST_SCENE_START_FRAME_RULE} "
        f"SCENE 1 fruit-only first-moment exception: {FRUIT_ONLY_FIRST_MOMENT_RULE} "
        "VIDEO PROMPT must include clip duration, reference style, "
        "no on-screen text or subtitles, and no background music/soundtrack. "
        "VIDEO PROMPT must be action-first: describe characters' actions, speech/lip sync, voice lock, and useful motion beats; "
        "avoid static layout, relative placement, room/background inventory, and title-safe composition zones. "
        "Visual style must be non-photorealistic/non-realistic cartoon (no realism). "
        "Grandmother must be a cartoon grandmother and the cat must be a cartoon cat; include the exact phrase cartoon cat. "
        "Kitchen physics must stay plausible: cookware belongs on a visible burner or hob when cooking; "
        "blenders must have jar, base, and lid; mixers need beaters, bowl, and motor body. "
        "Do not invent extra characters, duplicate animals, extra hands, or unrelated side actions. "
        "If scene has speech, require accurate lip sync with the Russian line. "
        "For every Russian word copied into video_prompt_en, especially after CHARACTERS SPEAKS, use `ё` when Russian spelling requires `ё`, not `е`. "
        "For scene 1, the spoken phrase in CHARACTERS SPEAKS must happen at the start of the clip and explicitly "
        "name the dish from the scenario; the viewer must hear the dish name in the first scene. "
        "Do not include title_safe_top_30 or top-30-percent overlay instructions in video_prompt_en; that rule is only for START FRAME. "
        f"Scene 1 fruit-only first-moment exception: {FRUIT_ONLY_FIRST_MOMENT_RULE} "
        "Framing: close-up / medium-close; grandmother's full figure and every other character in the scene "
        "must stay fully visible; camera angle or framing should evolve during the clip (no fixed static "
        "tripod for the entire take).\n"
        + _LLM_VIDEO_CHARACTERS_SPEAKS_RULE
        + "\n"
        + _RUSSIAN_YO_RULE
        + "\n"
        "If model is veo3* OR scene needs speech, include exact substring: voice_lock_full: "
        + GRANDMA_VOICE_LOCK_FULL
        + ".\n"
        "Also include this exact framing block verbatim: "
        + VEO3_FAST_VIDEO_FRAMING_RULE
        + "\n"
        "Use the cartoon-character and kitchen-physics rules when composing the prompt, but do not append bulky "
        "meta-rule labels like veo3_cartoon_character_rule or kitchen_physics_rule unless they naturally belong in the prompt.\n"
        "Do not mention location names or location references; preserve continuity through recurring props and food state.\n"
        "END FRAME is opt-in only. Keep a non-empty end_frame_prompt_ru only for scene numbers listed in "
        "requested_end_frame_scenes. For every other scene return `пусто`, even when start and finish differ, "
        "the scene has a payoff, or the final beat is visually notable.\n"
        "Respect the Viral Concept Brief and scene retention fields. First frame must not be boring preparation; "
        "final scene must deliver the visual payoff. If sponsored_content is true, integrate the brand naturally "
        "inside the cooking story, not as an ad break.\n"
        "If a scene contains romantic_drama_beat, both start_frame_prompt_ru and video_prompt_en must show that beat as visible background action for that exact scene.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt)
    try:
        prompts = _normalize_production_prompts(
            _extract_production_prompts(raw),
            scenes_count=len(scenes),
            require_frames=True,
        )
    except Exception:
        logger.exception("production prompts parse failed; using deterministic fallback", extra={"require_frames": True})
        prompts = _fallback_production_prompts(scenes, require_frames=True)
    enriched: list[dict[str, Any]] = []
    for scene_index, (scene, p) in enumerate(zip(scenes, prompts), start=1):
        scene_out = dict(scene)
        scene_out["model"] = "veo3_fast"
        scene_out["voice_provider"] = "none"
        end_frame_prompt = (
            _normalize_end_frame_prompt_for_stable_generation(p.get("end_frame_prompt_ru"))
            if scene_index in requested_end_frames
            else "пусто"
        )
        scene_out["start_frame_prompt"] = _ensure_image_prompt_framing_requirements(
            append_visual_prop_descriptions(_append_romantic_drama_to_start_prompt(p.get("start_frame_prompt_ru", ""), scene_out)),
            model=str(scene_out.get("model", "")),
            title_safe_top=scene_index == 1,
        )
        scene_out["end_frame_prompt"] = _ensure_image_prompt_framing_requirements(
            append_visual_prop_descriptions(
                sanitize_character_names_and_keys(
                    end_frame_prompt
                )
            ),
            model=str(scene_out.get("model", "")),
        )
        scene_out["end_frame_requested"] = _end_frame_prompt_requests_generation(end_frame_prompt)
        dialogue_by_lang = await _build_scene_dialogue_by_lang(scene_out, target_languages)
        speech_by_lang = (
            _speech_by_lang_from_dialogue(dialogue_by_lang)
            if dialogue_by_lang
            else await _build_scene_speech_by_lang(str(scene_out.get("text", "")).strip(), target_languages)
        )
        speech_plan_by_lang, speech_cues_by_lang = _build_scene_speech_plan_by_lang(
            speech_by_lang,
            dialogue_by_lang=dialogue_by_lang or _dialogue_by_lang_for_scene(scene_out, speech_by_lang),
        )
        base_prompt = _finalize_scene_video_prompt(
            _append_first_scene_title_safe_video_rule(str(p.get("video_prompt_en", "")), scene_index=scene_index),
            scene_out,
        )
        scene_out["speech_by_lang"] = speech_by_lang
        scene_out["dialogue_by_lang"] = dialogue_by_lang
        scene_out["speech_plan_by_lang"] = speech_plan_by_lang
        scene_out["speech_cues_by_lang"] = speech_cues_by_lang
        scene_out["speech_budget_by_lang"] = _speech_budget_by_lang(speech_by_lang, scene_out.get("duration_sec"))
        scene_out["video_prompt_by_lang"] = {
            lang: append_prompt_timing_hints(
                _replace_characters_speaks(base_prompt, speech),
                speech_plan_by_lang.get(lang),
                duration_sec=float(scene_out.get("duration_sec") or 0.0),
            )
            for lang, speech in speech_by_lang.items()
        }
        scene_out["video_results_by_lang"] = {}
        scene_out["video_approvals_by_lang"] = {lang: False for lang in speech_by_lang}
        scene_out["video_prompt"] = scene_out["video_prompt_by_lang"].get("ru") or base_prompt
        scene_out["text"] = speech_by_lang.get("ru") or str(scene_out.get("text", "")).strip()
        scene_out["tts"] = {
            "provider": "none",
            "note": "image-only iteration (no separate voice)",
        }
        enriched.append(scene_out)
    return {"json_plan": enriched}


async def generate_script(text: str) -> dict[str, Any]:
    """
    Упрощённый одношаговый сценарий (без выбора концепта): идея → цельный рецепт → сцены.

    Оставлен для совместимости / отладки; основной UX идёт через handlers.
    """
    full = await generate_recipe_full_text(text, dish_name=text)
    return await generate_scenes_plan(text.strip(), full_recipe_text=full)
