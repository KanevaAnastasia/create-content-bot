"""Validate complete spoken sentences; never trim a sentence to fit a limit."""
import json
import re

from .llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, extract_json_object, gemini_generate_sync


def validate_scene_speech(scenes: list[dict], brief: dict) -> None:
    minimum = int(brief.get("speech_min_chars") or 0)
    maximum = int(brief.get("speech_max_chars") or 105)
    if not minimum:
        return
    for _attempt in range(5):
        invalid = []
        for index, scene in enumerate(scenes):
            text = " ".join(str(scene.get("speech_ru") or "").split())
            scene["speech_ru"] = text
            if not minimum <= len(text) <= maximum or text[-1:] not in {".", "!", "?", "…"}:
                invalid.append({"index": index, "text": text, "length": len(text), "action": scene.get("action")})
        if not invalid:
            return
        prompt = (
            f"Отредактируй рекламные реплики бабушки. Длина КАЖДОЙ реплики: {minimum}–{maximum} символов "
            "с пробелами и пунктуацией. Живая разговорная речь, законченные предложения; заверши точкой, ! или ?. "
            "Без обрывков, воды и набивки длины, без служебного текста, без выдуманных свойств и отзывов. "
            "Сохрани смысл сцены, связь с товаром и естественное произношение. Верни JSON {\"phrases\": "
            "[{\"index\": 0, \"text\": \"...\"}]}. Индексы должны совпадать с исходными.\n"
            + json.dumps({"product": brief, "phrases": invalid}, ensure_ascii=False)
        )
        data = extract_json_object(gemini_generate_sync(prompt, model_profile=GEMINI_MODEL_PROFILE_CREATIVE))
        allowed = {item["index"] for item in invalid}
        for item in data.get("phrases", []):
            if isinstance(item, dict) and item.get("index") in allowed:
                scenes[item["index"]]["speech_ru"] = str(item.get("text") or "")
    # Check the last rewrite too.
    for scene in scenes:
        text = " ".join(str(scene.get("speech_ru") or "").split())
        if not minimum <= len(text) <= maximum or text[-1:] not in {".", "!", "?", "…"}:
            raise ValueError(f"Модель не подготовила законченные реплики длиной {minimum}–{maximum} символов. Повторите подготовку.")
        scene["speech_ru"] = text


def replace_spoken_phrase(video: str, phrase: str) -> str:
    """Keep approved speech verbatim, even when the prompt model rewrites it."""
    head = re.split(r"CHARACTERS? SPEAKS?\s*:", video, maxsplit=1, flags=re.I)[0].strip()
    return f'{head}\nCHARACTERS SPEAKS: "{phrase}" Accurate lip sync. No subtitles, no added text, no background music.'
