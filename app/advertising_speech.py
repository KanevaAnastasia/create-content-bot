"""Check that draft speech is developed enough, without an upper length limit."""
import json

from .llm_utils import TEXT_MODEL_PROFILE_SHORT, extract_json_object, gemini_generate_sync


class SpeechLengthError(ValueError):
    """The model did not supply a sufficiently developed spoken phrase."""


def validate_scene_speech(scenes: list[dict], brief: dict) -> None:
    minimum = int(brief.get("speech_min_chars") or 0)
    if not minimum:
        return
    for attempt in range(3):
        invalid = []
        for index, scene in enumerate(scenes):
            text = " ".join(str(scene.get("speech_ru") or "").split())
            scene["speech_ru"] = text
            if len(text) < minimum:
                invalid.append({"index": index, "text": text, "actual_length": len(text), "action": scene.get("action")})
        if not invalid:
            return
        prompt = (
            f"Разверни короткие рекламные реплики бабушки: каждая должна содержать не менее {minimum} символов "
            "с пробелами. Верхнего лимита на этом этапе НЕТ. Не подгоняй под точное число символов. "
            "Нужны живые законченные фразы по смыслу сцены, без воды, обрывков, выдуманных свойств и отзывов. "
            "Сокращение под длительность видео будет позже. Для каждого индекса предложи три варианта. "
            "Верни JSON {\"phrases\": [{\"index\": 0, \"candidates\": [\"...\", \"...\", \"...\"]}]}.\n"
            + json.dumps({"product": brief, "phrases": invalid}, ensure_ascii=False)
        )
        from .advertising_generation import advertising_editorial_rules
        prompt = advertising_editorial_rules(brief) + prompt
        data = extract_json_object(gemini_generate_sync(prompt, model_profile=TEXT_MODEL_PROFILE_SHORT))
        allowed = {item["index"] for item in invalid}
        for item in data.get("phrases", []):
            if not isinstance(item, dict):
                continue
            raw_index = item.get("index")
            if isinstance(raw_index, bool) or not str(raw_index).isdigit():
                continue
            index = int(raw_index)
            if index not in allowed:
                continue
            candidates = item.get("candidates")
            candidates = candidates if isinstance(candidates, list) else [item.get("text")]
            for value in candidates:
                if not isinstance(value, str):
                    continue
                text = " ".join(value.split())
                if len(text) >= minimum:
                    scenes[index]["speech_ru"] = text
                    allowed.remove(index)
                    break
    if any(len(scene["speech_ru"]) < minimum for scene in scenes):
        raise SpeechLengthError("Нейросеть вернула слишком короткие реплики. Данные товара исправлять не нужно.")
