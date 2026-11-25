"""Shorten scene speech for language-specific timing budgets and sync `video_prompt`."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .llm_utils import TEXT_MODEL_PROFILE_SHORT, extract_json_object, gemini_generate_sync
from .media_costs import estimate_gemini_text_cost_usd
from .pipeline_llm_steps import FINAL_SCENE_SUBSCRIBE_CTA, RUSSIAN_YO_RULE
from .scene_speech_budget import (
    PAUSE_TOKEN,
    max_speech_chars_for_duration,
    normalize_speech_phrase_for_voice,
    speech_chars_excluding_pauses,
)

logger = logging.getLogger("bot")

SPEECH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _replace_speech_in_video_prompt_if_substring(previous_vp: str, new_speech: str, previous_speech: str) -> str | None:
    """Replace only the exact speech after its marker; preserve all other bytes."""
    vp = previous_vp or ""
    old = (previous_speech or "").strip()
    neu = (new_speech or "").strip()
    markers = list(re.finditer(r"CHARACTERS SPEAKS:\s*", vp, flags=re.IGNORECASE))
    if not old or len(markers) != 1:
        return None
    start = markers[0].end()
    if vp.startswith(old, start):
        end = start + len(old)
        if old[-1].isalnum() and end < len(vp) and vp[end].isalnum():
            return None
        return vp[:start] + neu + vp[end:]
    return None


def _video_prompt_has_exact_speech_after_marker(video_prompt: str, text: str) -> bool:
    """Проверяет, что сразу после ``CHARACTERS SPEAKS:`` идёт нужная реплика."""
    m = re.search(r"(?is)CHARACTERS SPEAKS:\s*", video_prompt or "")
    if not m:
        return False
    rest = (video_prompt or "")[m.end() :].strip()
    if rest.startswith(text):
        return True
    first_line = rest.split("\n", 1)[0].strip()
    return first_line == text


def _speech_key(text: str) -> str:
    text = re.sub(r"\[\s*пауза\s*\]", " ", text or "", flags=re.IGNORECASE)
    text = re.sub(r"[.,!?;:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _has_final_subscribe_cta(text: str) -> bool:
    key = _speech_key(text)
    return bool(key) and key.endswith(_speech_key(FINAL_SCENE_SUBSCRIBE_CTA))


def shorten_scene_speech_and_sync_video_prompt_sync(
    scene: dict[str, Any],
    duration_sec: int,
    *,
    language: str = "ru",
    current_text: str | None = None,
    current_video_prompt: str | None = None,
    max_llm_attempts: int = 2,
    rate_limit_max_attempts: int | None = None,
) -> dict[str, str] | None:
    """
    Сокращает только реплику через LLM; промпт согласует локально.

    Returns:
        ``{"text": ..., "video_prompt": ...}`` при успехе, ``None`` если не удалось.
    """
    lang = str(language or "ru").strip().lower() or "ru"
    max_chars = max_speech_chars_for_duration(duration_sec, lang)
    prev_text = str(current_text if current_text is not None else scene.get("text", "") or "").strip()
    prev_vp = str(current_video_prompt if current_video_prompt is not None else scene.get("video_prompt", "") or "")
    must_keep_final_cta = lang == "ru" and _has_final_subscribe_cta(prev_text)
    if speech_chars_excluding_pauses(prev_text) <= max_chars:
        return None
    # Do not pay for an edit which cannot be merged unambiguously.
    if _replace_speech_in_video_prompt_if_substring(prev_vp, prev_text, prev_text) is None:
        return None

    payload = {
        "max_speech_chars_excluding_pauses": max_chars,
        "current_scene_text": prev_text,
        "must_keep_final_subscribe_cta": must_keep_final_cta,
        "language": lang,
    }
    if lang == "ru":
        text_rules = (
            "Поле text:\n"
            "- русский, от первого лица бабушки к зрителю (я/мы);\n"
            "- не используй «моя бабушка», «у моей бабушки», «как у моей бабушки», «бабушка научила», "
            "«рецепт моей бабушки»; бабушка говорит сама: я/мы/смотрите;\n"
            f"- {RUSSIAN_YO_RULE}\n"
            "- без ударений и без специальных акцентных меток;\n"
            "- сохраняй смысловую пунктуацию из исходной фразы; если финального . ! ? или … нет, добавь точку;\n"
            "- без дефиса и тире в самой фразе озвучки: не используй символы -, – или —; для паузы используй только [пауза];\n"
            f"- допускается {PAUSE_TOKEN} только между смысловыми фразами;\n"
            f"- длина речи **без** всех вхождений {PAUSE_TOKEN} — строго не больше max_speech_chars_excluding_pauses.\n"
            "- сохраняй все ингредиенты, действия и их порядок; если смысл не помещается, верни пустой text.\n"
            f"- если must_keep_final_subscribe_cta=true, text обязан заканчиваться этой фразой дословно: {FINAL_SCENE_SUBSCRIBE_CTA}; "
            "сокращай только основную фразу перед ней.\n"
        )
    else:
        text_rules = (
            "Поле text:\n"
            "- natural spoken English for short dubbing;\n"
            "- concise, easy to say, no stress markers or pronunciation annotations;\n"
            "- preserve meaningful punctuation from the source line; if there is no final . ! ? or …, add a period;\n"
            "- no hyphen-minus or dash characters in the spoken phrase: do not use -, – or —; use words or [пауза] instead;\n"
            f"- optional {PAUSE_TOKEN} only between major phrases;\n"
            "- rewrite as a complete grammatical sentence; never truncate a sentence or ingredient list;\n"
            "- preserve all essential ingredients, cooking actions and their order; remove redundant introductions first;\n"
            "- never end on a conjunction, preposition or unfinished enumeration;\n"
            "- if the essential meaning cannot fit, return an empty text instead of losing instructions;\n"
            f"- длина речи **без** всех вхождений {PAUSE_TOKEN} — строго не больше max_speech_chars_excluding_pauses.\n"
        )
    prompt = (
        "Shorten this spoken line to the character budget, preserving meaning and language.\n"
        'Верни только JSON {"text":"новая реплика"}.\n'
        + text_rules
        + "\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    attempts = max(1, int(max_llm_attempts or 1))
    for attempt in range(1, attempts + 1):
        extra = "" if attempt == 1 else "\n\nПредыдущий ответ нарушил лимит или формат. Переформулируй короче, сохранив смысл и законченность предложения."
        full_prompt = prompt + extra
        raw = gemini_generate_sync(
            full_prompt,
            rate_limit_max_attempts=rate_limit_max_attempts,
            model_profile=TEXT_MODEL_PROFILE_SHORT,
            response_schema=SPEECH_RESPONSE_SCHEMA,
            max_output_tokens=2048,
        )
        try:
            obj = extract_json_object(raw)
        except Exception as e:
            logger.warning("scene_speech_shorten json parse failed: %s", e)
            continue
        t = obj.get("text") if isinstance(obj, dict) else None
        if not isinstance(t, str) or not t.strip():
            continue
        raw_t = t.strip()
        t = normalize_speech_phrase_for_voice(raw_t)
        if not t:
            continue
        if lang == "en" and re.search(
            r"\b(?:and|or|but|with|the|a|an|to|of|for|then)\W*$", t, flags=re.IGNORECASE
        ):
            continue
        vp = _replace_speech_in_video_prompt_if_substring(prev_vp, t, prev_text)
        if vp is None:
            continue
        if speech_chars_excluding_pauses(t) > max_chars:
            continue
        if must_keep_final_cta and not _has_final_subscribe_cta(t):
            continue
        if not _video_prompt_has_exact_speech_after_marker(vp, t):
            continue
        cost = estimate_gemini_text_cost_usd(
            model="gemini-2.5-flash",
            prompt_text=full_prompt,
            response_text=raw,
        )
        return {"text": t, "video_prompt": vp, "estimated_gemini_cost_usd": cost["cost_usd"], "cost_event": cost}
    return None
