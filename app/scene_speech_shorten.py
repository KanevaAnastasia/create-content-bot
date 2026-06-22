"""Shorten scene speech for language-specific timing budgets and sync `video_prompt`."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .llm_utils import extract_json_object, gemini_generate_sync
from .media_costs import estimate_gemini_text_cost_usd
from .pipeline_llm_steps import FINAL_SCENE_SUBSCRIBE_CTA, FINAL_SCENE_SUBSCRIBE_CTA_VEO3, RUSSIAN_YO_RULE
from .scene_speech_budget import (
    PAUSE_TOKEN,
    max_speech_chars_for_duration,
    normalize_speech_phrase_for_voice,
    speech_chars_excluding_pauses,
)

logger = logging.getLogger("bot")


def _replace_speech_in_video_prompt_if_substring(previous_vp: str, new_speech: str, previous_speech: str) -> str | None:
    """Если старая реплика встречается в промпте — заменить первое вхождение."""
    vp = (previous_vp or "").strip()
    old = (previous_speech or "").strip()
    neu = (new_speech or "").strip()
    if old and old in vp:
        return vp.replace(old, neu, 1)
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
) -> dict[str, str] | None:
    """
    Сокращает ``scene['text']`` и согласует ``scene['video_prompt']`` через Gemini.

    Returns:
        ``{"text": ..., "video_prompt": ...}`` при успехе, ``None`` если не удалось.
    """
    lang = str(language or "ru").strip().lower() or "ru"
    max_chars = max_speech_chars_for_duration(duration_sec, lang)
    prev_text = str(current_text if current_text is not None else scene.get("text", "") or "").strip()
    prev_vp = str(current_video_prompt if current_video_prompt is not None else scene.get("video_prompt", "") or "").strip()
    must_keep_final_cta = lang == "ru" and _has_final_subscribe_cta(prev_text)
    if speech_chars_excluding_pauses(prev_text) <= max_chars:
        return None

    measured = speech_chars_excluding_pauses(prev_text)
    err_human = (
        f"Реплика слишком длинная для длительности сцены: {measured} символов речи "
        f"(без токенов {PAUSE_TOKEN}) при лимите {max_chars}. "
        f"Сократи текст, сохранив смысл и язык {lang}."
    )
    payload = {
        "duration_sec": duration_sec,
        "max_speech_chars_excluding_pauses": max_chars,
        "validation_note": (
            "Лимит считается по длине строки БЕЗ всех подстрок [пауза] — паузы в лимит не входят."
        ),
        "current_scene_text": prev_text,
        "current_video_prompt": prev_vp,
        "system_feedback": err_human,
        "must_keep_final_subscribe_cta": must_keep_final_cta,
        "final_subscribe_cta_tts": FINAL_SCENE_SUBSCRIBE_CTA_VEO3,
        "language": lang,
    }
    if lang == "ru":
        text_rules = (
            "Поле text:\n"
            "- русский, от первого лица бабушки к зрителю (я/мы);\n"
            f"- {RUSSIAN_YO_RULE}\n"
            "- без ударений и без специальных акцентных меток;\n"
            "- без дефиса и тире в самой фразе озвучки: не используй символы -, – или —; для паузы используй только [пауза];\n"
            f"- допускается {PAUSE_TOKEN} только между смысловыми фразами;\n"
            f"- длина речи **без** всех вхождений {PAUSE_TOKEN} — строго не больше max_speech_chars_excluding_pauses.\n"
            f"- если must_keep_final_subscribe_cta=true, text обязан заканчиваться этой фразой дословно: {FINAL_SCENE_SUBSCRIBE_CTA}; "
            "сокращай только основную фразу перед ней.\n"
        )
    else:
        text_rules = (
            "Поле text:\n"
            "- natural spoken English for short dubbing;\n"
            "- concise, easy to say, no stress markers or pronunciation annotations;\n"
            "- no hyphen-minus or dash characters in the spoken phrase: do not use -, – or —; use words or [пауза] instead;\n"
            f"- optional {PAUSE_TOKEN} only between major phrases;\n"
            "- if the line is still risky, prefer making it shorter over preserving every detail;\n"
            f"- длина речи **без** всех вхождений {PAUSE_TOKEN} — строго не больше max_speech_chars_excluding_pauses.\n"
        )
    prompt = (
        "You shorten short-video speech to fit clip timing and update the full video prompt.\n"
        'Верни ТОЛЬКО JSON-объект вида {"text":"...","video_prompt":"..."} без markdown.\n'
        + text_rules
        + "Поле video_prompt:\n"
        "- полный готовый video prompt;\n"
        "- обязательна подстрока `CHARACTERS SPEAKS: ` (двоеточие и пробел) сразу затем **дословно** то же, что в text;\n"
        "- если language=en и фраза на грани лимита, усили естественный темп речи и раннее завершение фразы до конца клипа;\n"
        "- сохрани прочие части промпта (камера, стиль, voice_lock), если возможно.\n\n"
        "INPUT:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    for attempt in (1, 2):
        extra = "" if attempt == 1 else "\n\nПредыдущий ответ нарушил лимит или формат. Сократи агрессивнее."
        full_prompt = prompt + extra
        raw = gemini_generate_sync(full_prompt)
        try:
            obj = extract_json_object(raw)
        except Exception as e:
            logger.warning("scene_speech_shorten json parse failed: %s", e)
            continue
        t = obj.get("text")
        vp = obj.get("video_prompt")
        if not isinstance(t, str) or not t.strip():
            continue
        raw_t = t.strip()
        t = normalize_speech_phrase_for_voice(raw_t)
        if not t:
            continue
        if not isinstance(vp, str) or not vp.strip():
            merged = _replace_speech_in_video_prompt_if_substring(prev_vp, t, prev_text)
            if merged is None:
                continue
            vp = merged
        else:
            vp = vp.strip()
            if raw_t != t:
                vp = _replace_speech_in_video_prompt_if_substring(vp, t, raw_t) or vp
        if speech_chars_excluding_pauses(t) > max_chars:
            continue
        if must_keep_final_cta and not _has_final_subscribe_cta(t):
            continue
        if not _video_prompt_has_exact_speech_after_marker(vp, t):
            merged = _replace_speech_in_video_prompt_if_substring(vp, t, prev_text) or _replace_speech_in_video_prompt_if_substring(
                prev_vp, t, prev_text
            )
            if merged is not None:
                vp = merged
            if not _video_prompt_has_exact_speech_after_marker(vp, t):
                continue
        cost = estimate_gemini_text_cost_usd(
            model="gemini-2.5-flash",
            prompt_text=full_prompt,
            response_text=raw,
        )
        return {"text": t, "video_prompt": vp, "estimated_gemini_cost_usd": cost["cost_usd"], "cost_event": cost}
    return None
