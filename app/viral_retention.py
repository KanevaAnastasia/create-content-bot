"""Viral retention helpers for cozy short-form cooking videos.

The module is intentionally pure: it normalizes LLM output and adds a soft
retention review to scene dictionaries without calling external services.
"""

from __future__ import annotations

import re
from typing import Any


RETENTION_FUNCTIONS = {
    "hook",
    "visual_proof",
    "recipe_progress",
    "grandma_secret",
    "cat_humor",
    "texture_shot",
    "transformation",
    "suspense",
    "payoff",
    "cta_save_reason",
    "brand_integration",
}

DISH_AUDIENCE_TYPES = {"child", "male", "neutral"}

FIRST_FRAME_TYPES = {
    "ready_dish_closeup",
    "cake_cut",
    "cheese_pull",
    "spoon_shot",
    "grandma_shows_dish",
    "cat_smells",
    "unexpected_phrase",
    "before_after",
    "secret_tease",
}

PAYOFF_TYPES = {
    "cut",
    "spoon",
    "crunch",
    "cheese_pull",
    "grandma_taste_smile",
    "cat_reaction",
    "final_plating",
    "family_table",
    "save_cta",
}

DEFAULT_ANALYSIS = {
    "hook_strength": 7,
    "first_frame_strength": 7,
    "retention_score": 7,
    "visual_payoff_score": 7,
    "emotional_warmth_score": 9,
    "recipe_clarity_score": 8,
    "saveability_score": 7,
    "brand_integration_safety_score": 8,
    "risk_of_boring_scene": 3,
    "expected_rewatch_reason": "пересмотреть красивый финальный кадр и секрет бабушки",
    "expected_comment_reason": "поделиться воспоминанием о домашней еде",
    "expected_save_reason": "сохранить рецепт и повторить дома",
}

FIRST_PHRASE_MAX_WORDS = 14
FIRST_PHRASE_MAX_CHARS = 115
FIRST_PHRASE_GENERIC_BAD_PATTERNS = (
    "сегодня готов",
    "мы готовим",
    "я готовлю",
    "давайте приготов",
    "начинаем готов",
    "в этом видео",
    "это блюдо",
    "вкусное блюдо",
    "рецепт пользователя",
    "простой рецепт",
)
FIRST_PHRASE_POWER_MARKERS = (
    "в конце",
    "разрез",
    "ложк",
    "хруст",
    "тян",
    "сыр",
    "крем",
    "нежн",
    "пахнет",
    "детство",
    "покупной",
    "нельзя терять",
    "сохрани",
    "слишком красив",
    "кот",
    "сравни",
    "не теря",
)
FIRST_SCENE_VISUAL_HOOK_MARKERS = (
    "готов",
    "разрез",
    "ложк",
    "хруст",
    "тян",
    "сыр",
    "крем",
    "показывает",
    "нюха",
    "запах",
    "было",
    "стало",
    "секрет",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(_text(v) for v in value).strip()
    return str(value).strip()


def _clamp_score(value: Any, default: int = 7) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        n = default
    return max(1, min(10, n))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _fallback_hooks(concept: str) -> dict[str, list[str]]:
    dish = concept.strip() or "это блюдо"
    return {
        "emotional": [
            f"Этот {dish} пахнет так, будто дома снова детство.",
            f"Бабушка сказала, что такой {dish} нельзя терять.",
            f"Когда на столе {dish}, дома сразу становится теплее.",
        ],
        "intrigue": [
            f"Все думали, что {dish} покупной, но бабушка сделала его дома.",
            f"Покажу разрез {dish}, ради него хочется досмотреть до конца.",
            f"Сначала кажется просто, но финал у этого блюда слишком красивый.",
        ],
        "useful": [
            f"Сохрани этот способ, чтобы {dish} получался ровнее и вкуснее.",
            f"Не теряй этот приём, с ним {dish} получается заметно лучше.",
        ],
        "cliffhanger": [
            "Досмотрите до разреза, он получился слишком красивым.",
            "В конце кот проверит запах, и сразу станет понятно, удалось ли.",
        ],
    }


def normalize_hooks(raw_hooks: Any, concept: str) -> dict[str, list[str]]:
    """Return hook groups with at least 3/3/2/2 items."""
    fallback = _fallback_hooks(concept)
    source = raw_hooks if isinstance(raw_hooks, dict) else {}
    limits = {"emotional": 3, "intrigue": 3, "useful": 2, "cliffhanger": 2}
    out: dict[str, list[str]] = {}
    for key, min_count in limits.items():
        values = _string_list(source.get(key))
        values.extend(x for x in fallback[key] if x not in values)
        out[key] = values[:max(min_count, len(values))]
    return out


def flatten_hooks(hooks: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for key in ("emotional", "intrigue", "useful", "cliffhanger"):
        for hook in hooks.get(key, []):
            if hook and hook not in out:
                out.append(hook)
    return out


def _word_count(text: str) -> int:
    return len(re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", text or ""))


def _normalize_first_phrase(raw: str, *, fallback: str) -> str:
    phrase = re.sub(r"\s+", " ", (raw or "").strip())
    if not phrase:
        phrase = fallback.strip()
    if _word_count(phrase) <= FIRST_PHRASE_MAX_WORDS and len(phrase) <= FIRST_PHRASE_MAX_CHARS:
        return phrase
    words = re.findall(r"\S+", phrase)
    shortened = " ".join(words[:FIRST_PHRASE_MAX_WORDS]).strip(" ,.;:!?")
    return shortened or fallback.strip()


def _normalize_dish_audience_type(value: Any) -> str:
    text = _text(value).lower()
    if text in DISH_AUDIENCE_TYPES:
        return text
    return "neutral"


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    return text in {"1", "true", "yes", "да", "нужен", "нужно"}


def review_first_phrase(
    phrase: str,
    *,
    scene: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scores whether the first spoken line is strong enough for a short-form hook."""
    text = _text(phrase)
    low = text.lower()
    issues: list[str] = []
    positives: list[str] = []
    score = 10
    words = _word_count(text)
    if not text:
        issues.append("первая фраза пустая")
        score -= 6
    if words > FIRST_PHRASE_MAX_WORDS or len(text) > FIRST_PHRASE_MAX_CHARS:
        issues.append("первая фраза слишком длинная для первых секунд")
        score -= 2
    if any(p in low for p in FIRST_PHRASE_GENERIC_BAD_PATTERNS):
        issues.append("начинается как обычное вступление вместо hook-first фразы")
        score -= 3
    if not any(p in low for p in FIRST_PHRASE_POWER_MARKERS):
        issues.append("нет открытой петли, payoff-слова или эмоционального повода досмотреть")
        score -= 2
    else:
        positives.append("есть интрига, эмоция или payoff-маркер")
    if scene is not None:
        scene_blob = _blob(scene)
        if not any(p in scene_blob for p in FIRST_SCENE_VISUAL_HOOK_MARKERS):
            issues.append("первая сцена визуально не подтверждает hook")
            score -= 2
        else:
            positives.append("первый кадр поддерживает фразу визуальным доказательством")
    if brief is not None:
        first_frame = _text(brief.get("strongest_first_frame"))
        if first_frame:
            positives.append("есть рекомендованный первый кадр из viral brief")
    suggestion = ""
    if issues:
        if isinstance(brief, dict):
            suggestion = _text(brief.get("strongest_first_phrase")) or _text(brief.get("main_hook"))
        suggestion = suggestion or "Не теряйте это блюдо, сейчас покажу самый аппетитный момент."
    return {
        "score": max(1, min(10, score)),
        "issues": issues,
        "positive_notes": positives,
        "suggestion": suggestion,
    }


def normalize_viral_concept_brief(
    data: dict[str, Any] | None,
    *,
    concept: str,
    sponsored_content: bool = False,
    brand_context: str | None = None,
) -> dict[str, Any]:
    """Normalize LLM JSON for the Viral Concept Brief contract."""
    raw = data if isinstance(data, dict) else {}
    hooks = normalize_hooks(raw.get("hooks"), concept)
    main_hook = _text(raw.get("main_hook")) or flatten_hooks(hooks)[0]
    final_phrases = _string_list(raw.get("final_phrases"))
    if len(final_phrases) < 3:
        final_phrases.extend(
            [
                "Сохрани, чтобы не потерять этот домашний рецепт.",
                "Подаю к столу, а вы сохраняйте на уютный вечер.",
                "Подписывайтесь на бабушку, тут много вкусных и простых рецептов.",
            ]
        )
    first_frame_type = _text(raw.get("first_frame_type")) or "ready_dish_closeup"
    if first_frame_type not in FIRST_FRAME_TYPES:
        first_frame_type = "ready_dish_closeup"
    payoff_type = _text(raw.get("payoff_type")) or "cut"
    if payoff_type not in PAYOFF_TYPES:
        payoff_type = "cut"
    brand_plan = _text(raw.get("brand_integration_plan"))
    if sponsored_content and not brand_plan:
        product = (brand_context or "продукт бренда").strip()
        brand_plan = f"Встроить {product} как естественный ингредиент или бабушкин совет внутри рецепта."
    analysis = dict(DEFAULT_ANALYSIS)
    raw_analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
    for key, default in DEFAULT_ANALYSIS.items():
        if isinstance(default, int):
            analysis[key] = _clamp_score(raw_analysis.get(key), default)
        else:
            analysis[key] = _text(raw_analysis.get(key)) or default
    strongest_first_phrase = _normalize_first_phrase(
        _text(raw.get("strongest_first_phrase")),
        fallback=main_hook,
    )
    phrase_review = review_first_phrase(strongest_first_phrase)
    if phrase_review["score"] < 7:
        strongest_first_phrase = _normalize_first_phrase(main_hook, fallback=flatten_hooks(hooks)[0])
        phrase_review = review_first_phrase(strongest_first_phrase)
    dish_audience_type = _normalize_dish_audience_type(raw.get("dish_audience_type"))
    family_cast_plan = _text(raw.get("family_cast_plan"))
    if not family_cast_plan:
        if dish_audience_type == "child":
            family_cast_plan = "В первой и последней сцене добавить одного из внуков; в середине оставить только базовых персонажей."
        elif dish_audience_type == "male":
            family_cast_plan = "В первой и последней сцене добавить дедушку; в середине оставить только базовых персонажей."
        else:
            family_cast_plan = "Дополнительных семейных персонажей не добавлять."
    return {
        "main_hook": main_hook,
        "viewer_promise": _text(raw.get("viewer_promise")) or "уютный домашний рецепт с понятным результатом",
        "reason_to_watch_end": _text(raw.get("reason_to_watch_end")) or "в финале будет красивый визуальный payoff",
        "main_visual_payoff": _text(raw.get("main_visual_payoff")) or "крупный финальный кадр готового блюда",
        "emotional_tone": _text(raw.get("emotional_tone")) or "уютно, тепло, семейно, с мягким юмором",
        "series_format": _text(raw.get("series_format")) or "",
        "strongest_first_phrase": strongest_first_phrase,
        "first_phrase_review": phrase_review,
        "strongest_first_frame": _text(raw.get("strongest_first_frame"))
        or "крупный план готового блюда, бабушка рядом, кот реагирует на запах",
        "first_frame_type": first_frame_type,
        "hooks": hooks,
        "alternative_hooks": flatten_hooks(hooks)[:3],
        "final_phrases": final_phrases[:3],
        "payoff_type": payoff_type,
        "brand_integration_plan": brand_plan,
        "analysis": analysis,
        "sponsored_content": bool(sponsored_content),
        "dish_audience_type": dish_audience_type,
        "family_cast_plan": family_cast_plan,
        "first_scene_dialogue_hint": _boolish(raw.get("first_scene_dialogue_hint")),
    }


def _blob(scene: dict[str, Any]) -> str:
    return " ".join(
        _text(scene.get(k))
        for k in (
            "goal",
            "action",
            "visual_description",
            "text",
            "recipe_step",
            "dish_description",
            "continuity_state",
        )
    ).lower()


def infer_scene_function(scene: dict[str, Any], *, index: int, total: int, sponsored_content: bool = False) -> str:
    blob = _blob(scene)
    if index == 1:
        return "hook"
    if index == total:
        return "payoff"
    if sponsored_content and any(x in blob for x in ("бренд", "товар", "упаков", "продукт")):
        return "brand_integration"
    if any(x in blob for x in ("секрет", "хитрост", "фишк", "совет")):
        return "grandma_secret"
    if any(x in blob for x in ("кот", "лап", "нюха", "смотрит", "улыб")):
        return "cat_humor"
    if any(x in blob for x in ("тян", "крем", "хруст", "нежн", "густ", "сочный", "текстур")):
        return "texture_shot"
    if any(x in blob for x in ("было", "стало", "превращ", "румян", "поднима", "застыва")):
        return "transformation"
    if any(x in blob for x in ("досмотр", "в конце", "сейчас покаж", "интриг")):
        return "suspense"
    if any(x in blob for x in ("добав", "смеш", "нарез", "жар", "печ", "вар", "взб", "формир")):
        return "recipe_progress"
    return "visual_proof"


def micro_events_for_scene(scene: dict[str, Any], scene_function: str) -> list[str]:
    blob = _blob(scene)
    events: list[str] = []
    if scene_function == "hook":
        events.extend(["сильный первый кадр", "реакция бабушки или кота"])
    if "кот" in blob:
        events.append("реакция кота")
    if any(x in blob for x in ("добав", "сып", "льет", "клад")):
        events.append("добавление ингредиента")
    if any(x in blob for x in ("крем", "сыр", "тесто", "соус", "суп", "хруст")):
        events.append("изменение текстуры")
    if any(x in blob for x in ("круп", "close", "детал")):
        events.append("food close-up")
    if scene_function in {"transformation", "payoff"}:
        events.append("визуальная трансформация")
    if len(events) < 2:
        events.append("смена крупности")
    if len(events) < 3:
        events.append("монтажный переход к следующему этапу")
    return events[:4]


def boring_scene_risk(scene: dict[str, Any], scene_function: str) -> int:
    blob = _blob(scene)
    risk = 4
    if scene_function in {"hook", "payoff", "texture_shot", "transformation", "grandma_secret"}:
        risk -= 2
    if any(x in blob for x in ("просто", "готовит", "делает", "следующий шаг", "перемешивает")):
        risk += 3
    if any(x in blob for x in ("круп", "секрет", "кот", "нежн", "хруст", "разрез", "ложк", "сыр")):
        risk -= 1
    duration = scene.get("duration_sec")
    if isinstance(duration, int) and duration >= 8 and scene_function == "recipe_progress":
        risk += 1
    return max(1, min(10, risk))


def retention_suggestion(scene_function: str, risk: int) -> str:
    if risk <= 4:
        return ""
    suggestions = {
        "recipe_progress": "сократить действие, добавить нарезку крупностей или реакцию бабушки/кота",
        "visual_proof": "заменить на texture shot или явную визуальную трансформацию",
        "brand_integration": "сделать товар частью рецепта, а не отдельной рекламной вставкой",
        "payoff": "усилить финал разрезом, ложкой, хрустом, тянущимся сыром или улыбкой бабушки",
        "hook": "начать с результата крупным планом и короткой hook-first фразы",
    }
    return suggestions.get(scene_function, "добавить движение, эмоцию или визуальный акцент каждые 2-3 секунды")


def enrich_scenes_with_retention(
    scenes: list[dict[str, Any]],
    *,
    viral_concept_brief: dict[str, Any] | None = None,
    sponsored_content: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach retention fields to scenes and return a compact review."""
    total = len(scenes)
    brief = viral_concept_brief if isinstance(viral_concept_brief, dict) else {}
    out: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for idx, scene in enumerate(scenes, start=1):
        item = dict(scene)
        fn = _text(item.get("retention_function"))
        if fn not in RETENTION_FUNCTIONS:
            fn = infer_scene_function(item, index=idx, total=total, sponsored_content=sponsored_content)
        events = _string_list(item.get("micro_events")) or micro_events_for_scene(item, fn)
        risk = boring_scene_risk(item, fn)
        suggestion = _text(item.get("retention_suggestion")) or retention_suggestion(fn, risk)
        item["retention_function"] = fn
        item["micro_events"] = events
        item["boring_scene_risk"] = risk
        if suggestion:
            item["retention_suggestion"] = suggestion
        if idx == 1:
            item["recommended_first_frame_type"] = brief.get("first_frame_type") or "ready_dish_closeup"
            first_phrase_review = review_first_phrase(
                _text(item.get("text")),
                scene=item,
                brief=brief,
            )
            item["first_phrase_score"] = first_phrase_review["score"]
            item["first_phrase_review"] = first_phrase_review
            if first_phrase_review["suggestion"]:
                item["first_phrase_suggestion"] = first_phrase_review["suggestion"]
        if idx == total:
            item["payoff_type"] = _text(item.get("payoff_type")) or _text(brief.get("payoff_type")) or "cut"
            item.setdefault("save_reason", "сохранить рецепт, чтобы повторить дома")
        out.append(item)
        items.append(
            {
                "scene": item.get("scene", idx),
                "function": fn,
                "boring_scene_risk": risk,
                "micro_events": events,
                "suggestion": suggestion,
                "first_phrase_score": item.get("first_phrase_score") if idx == 1 else None,
            }
        )
    max_risk = max((int(x["boring_scene_risk"]) for x in items), default=1)
    weak = [x for x in items if int(x["boring_scene_risk"]) >= 7]
    review = {
        "overall_retention_score": max(1, min(10, 11 - max_risk)),
        "scene_reviews": items,
        "weak_scenes": weak,
        "first_frame_type": brief.get("first_frame_type") or "ready_dish_closeup",
        "payoff_type": brief.get("payoff_type") or "cut",
        "first_scene_review": out[0].get("first_phrase_review") if out else {},
    }
    return out, review
