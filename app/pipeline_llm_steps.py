"""
Текстовые шаги пайплайна: единый контракт «предыдущий ответ + фидбек → Gemini».

Промпты сгруппированы по шагам; генерация кадров и видео остаётся в других модулях (Vertex и т.д.).
"""

from __future__ import annotations

from .grandma_editorial import GRANDMA_STORY_RULES, GRANDMA_OBSERVED_EXAMPLES, GRANDMA_RETENTION_REVIEW_RULES

import asyncio
import json
import re
from typing import Any

from .assets import ASSETS
from .character_catalog import character_prompt_catalog, maybe_choose_romantic_subplot
from .llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, gemini_generate_sync


FINAL_SCENE_SUBSCRIBE_CTA = "Подписывайтесь на бабушку, тут много вкусных и простых рецептов"
FINAL_SCENE_SUBSCRIBE_CTA_VEO3 = FINAL_SCENE_SUBSCRIBE_CTA
RUSSIAN_YO_RULE = (
    "Правило русской буквы ё для ОЗВУЧКИ: в voiceover-полях `text`, `dialogue.text`, `speech` "
    "и `CHARACTERS SPEAKS` используй нормативную русскую орфографию. Ставь `ё` только в словах, "
    "где действительно нужна `ё` по смыслу и произношению (например: всё, ещё, берёт, идёт, тёртый, "
    "свёкла, мёд, рёбра), и оставляй обычную `е` там, где нужна `е` (например: все люди, теперь, "
    "делаем, нежный, перемешиваем). Не добавляй лишние `ё` ради звучания."
)
FRUIT_ONLY_FIRST_MOMENT_RULE = (
    "If the user explicitly asks scene 1 to start with a close-up of anthropomorphic banana and strawberry talking "
    "or says the start frame is only banana/strawberry and without grandma/cats, keep the START frame to that first "
    "moment only. Do not add grandma, cats, grandson/granddaughter, blender reveal, or later payoff into the start "
    "frame; put those later beats into action/micro_events/video/end frame."
)


async def gemini_text(prompt: str, *, model_profile: str = GEMINI_MODEL_PROFILE_CREATIVE) -> str:
    """Вызывает синхронный Gemini в thread pool, чтобы не блокировать event loop бота."""
    return (
        await asyncio.to_thread(
            gemini_generate_sync,
            prompt,
            model_profile=model_profile,
        )
        or ""
    ).strip()


def allowed_locations_strict() -> list[str]:
    """Legacy hook: сюжетные локации больше не передаются в LLM-контракт."""
    return []


def _character_assets_only() -> dict[str, Any]:
    """Возвращает только character assets; location assets остаются в проекте, но не уходят в LLM."""
    return {"characters": ASSETS.get("characters") or {}}


def _character_short_descriptions() -> dict[str, str]:
    """DB-first short labels for non-dialogue scenario fields."""
    catalog = character_prompt_catalog()
    return {
        key: str(item.get("short_description") or item.get("name") or key)
        for key, item in catalog.items()
        if isinstance(item, dict)
    }


def _quoted_variants(text: str) -> list[str]:
    out: list[str] = []
    for pattern in (r"«([^»]+)»", r'"([^"]+)"'):
        for match in re.findall(pattern, text or ""):
            value = str(match).strip()
            if value and value not in out:
                out.append(value)
    return out


_DIALOGUE_ROLE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("grandfather", (r"дедушк[аиуе]?", r"grandfather", r"grandpa")),
    ("grandma", (r"бабушк[аиуе]?", r"grandma")),
    ("grandson", (r"внук[ауе]?", r"grandson")),
    ("granddaughter", (r"внучк[аиуе]?", r"granddaughter")),
    ("banana", (r"банан\w*", r"banana")),
    ("strawberry", (r"клубник\w*", r"strawberry")),
)


def _dialogue_speaker_from_prefix(prefix: str) -> str:
    for canonical, patterns in _DIALOGUE_ROLE_PATTERNS:
        if any(re.search(pattern, prefix, flags=re.IGNORECASE) for pattern in patterns):
            return canonical
    return ""


def _clean_dialogue_colon_line(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    cut_markers = (
        " и дальше ",
        " дальше ",
        " и потом ",
        " потом ",
        " и включает ",
        " включает ",
        " и отдаляется ",
        " отдаляется ",
    )
    low = text.lower()
    cut_positions = [low.find(marker) for marker in cut_markers if low.find(marker) > 0]
    if cut_positions:
        text = text[: min(cut_positions)].strip()
    return text.strip(" .")


def _colon_dialogue_variants(text: str) -> list[dict[str, str]]:
    raw = str(text or "")
    if ":" not in raw:
        return []
    role_union = "|".join(pattern for _canonical, patterns in _DIALOGUE_ROLE_PATTERNS for pattern in patterns)
    colon_pattern = re.compile(
        rf"(?P<prefix>(?:{role_union})[^:\n]{{0,90}}?):",
        flags=re.IGNORECASE,
    )
    matches = list(colon_pattern.finditer(raw))
    out: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        speaker = _dialogue_speaker_from_prefix(match.group("prefix"))
        if not speaker:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        phrase = _clean_dialogue_colon_line(raw[start:end])
        if phrase:
            out.append({"speaker": speaker, "text": phrase})
    return out


def _dialogue_feedback_hint(feedback: str) -> dict[str, Any]:
    raw = (feedback or "").strip()
    if not raw:
        return {}
    low = raw.lower()
    if "убери диалог" in low or "без диалога" in low:
        return {"scene": 1, "dialogue_mode": "single", "dialogue": []}
    if "первая сцен" not in low and "сцена 1" not in low and "в первой" not in low:
        return {}
    if "диалог" not in low and "говорит" not in low and "разговарива" not in low:
        return {}
    quotes = _quoted_variants(raw)
    if not quotes:
        colon_dialogue = _colon_dialogue_variants(raw)
        if colon_dialogue:
            return {"scene": 1, "dialogue_mode": "multi", "dialogue": colon_dialogue}
    if not quotes:
        return {"scene": 1, "dialogue_mode": "multi", "dialogue": []}
    dialogue: list[dict[str, str]] = []
    for quote in quotes:
        quote_low = quote.lower()
        speaker = ""
        for canonical, patterns in _DIALOGUE_ROLE_PATTERNS:
            if any(re.search(p + r".{0,40}$", raw[: raw.lower().find(quote_low)] if quote_low in low else "", flags=re.IGNORECASE) for p in patterns):
                speaker = canonical
                break
        if not speaker:
            idx = low.find(quote_low)
            prefix = raw[:idx] if idx >= 0 else raw
            for canonical, patterns in _DIALOGUE_ROLE_PATTERNS:
                if any(re.search(p + r"[^«\"]{0,60}$", prefix, flags=re.IGNORECASE) for p in patterns):
                    speaker = canonical
                    break
        if speaker:
            dialogue.append({"speaker": speaker, "text": quote})
    return {"scene": 1, "dialogue_mode": "multi", "dialogue": dialogue} if dialogue else {}


def build_concepts_prompt(idea: str, feedback: str, previous: list[Any]) -> str:
    return (
        "Ты создаешь вирусные концепты рецептов.\n\n"
        f"ИДЕЯ:\n{idea}\n\n"
        f"ФИДБЕК:\n{feedback or '-'}\n\n"
        f"ПРЕДЫДУЩИЕ ВАРИАНТЫ:\n{json.dumps(previous, ensure_ascii=False)}\n\n"
        "ЗАДАЧА:\n"
        "- предложить 4-6 НОВЫХ концептов\n"
        "- дать 2–3 концепта с максимально простым рецептом и максимально простыми, доступными ингредиентами\n"
        "- дать 2–3 максимально виральных концепта для коротких видео\n"
        "- учесть фидбек\n"
        "- не повторяться\n"
        "- блюда должны быть простые, визуально интересные и потенциально вирусные\n"
        "- если идея пользователя похожа на конкретное блюдо, обязательно включи его + 2-3 вариации\n\n"
        "ФОРМАТ КОНЦЕПТА:\n"
        "- каждый концепт должен быть коротким: name + why + hook суммарно до 200 символов\n"
        "- не превращай концепт в рецепт и не расписывай шаги приготовления\n\n"
        "Верни ТОЛЬКО JSON-объект формата:\n"
        '{"concepts":[{"name":"...","why":"...","hook":"..."}]}'
    )


def build_concepts_json_repair_prompt(raw: str) -> str:
    return (
        "Преобразуй ответ в строго валидный JSON без markdown и без комментариев.\n"
        "Сохрани только объект формата:\n"
        '{"concepts":[{"name":"...","why":"...","hook":"..."}]}\n\n'
        "НЕ меняй смысл, только исправь JSON-синтаксис.\n\n"
        "INPUT:\n"
        + raw
    )


def build_recipe_full_prompt(concept: str, dish_name: str, feedback: str, prev: str) -> str:
    return (
        "Ты составляешь домашний рецепт для короткого кулинарного видео.\n\n"
        f"КОНЦЕПТ БЛЮДА:\n{concept}\n\n"
        f"НАЗВАНИЕ/ФОКУС (не менять блюдо):\n{dish_name}\n\n"
        f"ПРЕДЫДУЩАЯ ВЕРСИЯ РЕЦЕПТА (если пусто — игнорируй):\n{prev or '-'}\n\n"
        f"ПРАВКИ ОТ ПОЛЬЗОВАТЕЛЯ:\n{feedback or '-'}\n\n"
        "ЗАДАЧА:\n"
        "- верни ОДИН связный текст на русском\n"
        "- структура: кратко ингредиенты (списком или абзацем), затем приготовление обычными абзацами\n"
        "- ЗАПРЕЩЕНО нумеровать этапы как «ШАГ 1», «1.», «2)» — никакой пошаговой нумерации\n"
        "- без кота, без сюжета, без обращения к зрителю; только рецепт\n"
        "- конкретные действия и продукты\n\n"
        "Верни только текст рецепта, без заголовков вроде «Рецепт:» и без markdown."
    )


def build_advice_full_prompt(
    selected_concept: str,
    source_text: str,
    feedback: str,
    prev: str,
) -> str:
    return (
        "Ты редактируешь текст короткого полезного ролика в формате «Совет» для русской страницы бабушки.\n\n"
        f"ВЫБРАННЫЙ ВОПРОС ДЛЯ НАЧАЛА РОЛИКА:\n{selected_concept}\n\n"
        f"ИСХОДНЫЙ МАТЕРИАЛ. ЭТО ГЛАВНЫЙ ИСТОЧНИК ФАКТОВ:\n{source_text}\n\n"
        f"ПРЕДЫДУЩАЯ ВЕРСИЯ ТЕКСТА СОВЕТА:\n{prev or '-'}\n\n"
        f"ПРАВКИ ОТ ПОЛЬЗОВАТЕЛЯ:\n{feedback or '-'}\n\n"
        "ЗАДАЧА:\n"
        "- верни текст совета, а не рецепт блюда и не список ингредиентов\n"
        "- начни с выбранного вопроса дочери; не заменяй его другим хуком\n"
        "- сохрани все конкретные советы, продукты, количества, сроки и оговорки из исходного материала\n"
        "- если источник содержит советы для нескольких блюд или продуктов, сохрани их все в исходном порядке; "
        "не сужай текст до одного блюда из выбранного вопроса\n"
        "- если пользователь прислал в правке полный готовый вариант, возьми его за основу целиком, "
        "сохрани порядок, конкретику и естественный ритм\n"
        "- не придумывай новые факты, ингредиенты, измерения или правила\n"
        "- допускаются короткие смысловые блоки с названиями продуктов, если так понятнее зрителю\n"
        "- без служебных комментариев и без заголовка «Рецепт»\n\n"
        "Верни только готовый русский текст совета."
    )


def build_recipe_from_source_text_prompt(source_text: str, source_url: str | None = None) -> str:
    """Промпт для извлечения цельного рецепта из сырого текста страницы/источника."""
    src = (source_text or "").strip()
    url = (source_url or "").strip() or "-"
    return (
        "Ты извлекаешь кулинарный рецепт из сырого текста страницы.\n\n"
        f"ИСТОЧНИК РЕЦЕПТА (URL):\n{url}\n\n"
        "ЗАДАЧА:\n"
        "- выдели только один основной рецепт блюда из текста\n"
        "- убери рекламу, навигацию, комментарии, посторонние блоки\n"
        "- верни один связный текст на русском\n"
        "- структура: сначала ингредиенты (списком или абзацем), затем приготовление абзацами\n"
        "- без нумерации шагов вида «ШАГ 1», «1.», «2)»\n"
        "- без markdown и без заголовков «Рецепт:»\n"
        "- если в тексте несколько рецептов, выбери самый полный и цельный\n\n"
        "Верни только текст рецепта.\n\n"
        "ТЕКСТ ИСТОЧНИКА:\n"
        f"{src}"
    )


def build_viral_concept_brief_prompt(
    *,
    concept: str,
    full_recipe_text: str,
    sponsored_content: bool = False,
    brand_context: str | None = None,
    analytics_feedback: dict[str, Any] | None = None,
) -> str:
    """Prompt for the pre-scenario Viral Concept Brief."""
    payload = {
        "concept": concept,
        "full_recipe_text": full_recipe_text,
        "sponsored_content": bool(sponsored_content),
        "brand_context": brand_context or "",
        "analytics_feedback": analytics_feedback or {},
    }
    return (
        GRANDMA_STORY_RULES + GRANDMA_OBSERVED_EXAMPLES + GRANDMA_RETENTION_REVIEW_RULES + "Ты сценарный редактор уютного food/cartoon-блога с бабушкой, котом и домашними рецептами.\n"
        "Нужно подготовить Viral Concept Brief ДО генерации сцен: ролик должен удерживать внимание, "
        "но не превращаться в агрессивный кликбейт.\n\n"
        "Стиль: уют, бабушка, кот, домашняя еда, семейность, тепло, мультяшность, аппетитность. "
        "Разрешён только мягкий кликбейт: интрига, эмоция, обещание результата, красивый payoff. "
        "Запрещены обманные обещания, токсичность, страх, шок-контент и неуместный экстрим.\n\n"
        "Если sponsored_content=true, встрои бренд естественно: как ингредиент, бабушкин совет, подарок к столу, "
        "семейный ритуал, решение проблемы, предмет реакции кота, красивая упаковка в кадре или способ улучшить результат. "
        "Запрещены сухая рекламная вставка, длинный список преимуществ, фраза «а теперь реклама», неестественная похвала.\n\n"
        "Верни строго JSON объект без markdown с ключами:\n"
        "main_hook, viewer_promise, reason_to_watch_end, main_visual_payoff, emotional_tone, series_format, "
        "strongest_first_phrase, strongest_first_frame, first_frame_type, hooks, alternative_hooks, final_phrases, "
        "payoff_type, brand_integration_plan, analysis, dish_audience_type, family_cast_plan, first_scene_dialogue_hint.\n\n"
        "dish_audience_type: выбери строго одно значение child, male или neutral.\n"
        "- child: детские блюда, напитки, снеки и фастфуд; ориентируйся на то, что обычно любят дети: фри, хот-доги, корн-доги, бургеры, наггетсы, сладкое, десерты, молочные коктейли, лимонады, какао, попкорн, KFC-style fast food и близкие по смыслу блюда\n"
        "- male: мужские мясные ужины и плотная сытная еда; ориентируйся на то, что часто любят мужчины: мясо, жаркое, гуляш, шашлыки, рёбра, стейки, колбаски, мясные закуски к столу, соленья, сытные горячие мясные блюда и близкие по смыслу блюда\n"
        "- neutral: все остальные блюда, которые не тянут явно ни в детское, ни в мужское\n"
        "- классифицируй не только по точному совпадению примеров, но и по смыслу, ассоциации и типичному восприятию блюда: что больше любят дети, а что больше ассоциируется с мужским мясным ужином\n"
        "family_cast_plan: одной короткой строкой опиши, кого добавлять в первую и последнюю сцену.\n"
        "first_scene_dialogue_hint: true, если для детского блюда уместен короткий живой обмен репликами или реакция в первой сцене/финале; иначе false.\n\n"
        "strongest_first_phrase — самая важная строка ролика. Правила:\n"
        "- короткая законченная мысль; утверждённый пользователем текст сохраняй дословно независимо от числа слов\n"
        "- первые слова дают узнаваемый повод смотреть; не требуй произнести всю реплику за 1-2 секунды\n"
        "- обязательно назови конкретное блюдо или узнаваемую часть блюда\n"
        "- дай конкретный повод смотреть: знакомая проблема, семейный повод или желаемый результат; визуальную деталь оставь в описании кадра, не заставляй озвучивать её как обещание\n"
        "- не начинай с обычного вступления «сегодня готовим», «мы приготовим», «давайте сделаем», «в этом видео»\n"
        "- не обещай то, чего нет в рецепте или финале\n"
        "- не добавляй в visual payoff ингредиенты, гарниры, топпинги или гарнирные элементы, которых нет "
        "в full_recipe_text, brand_context или явной просьбе пользователя\n"
        "- тон мягкий и уютный, без агрессивного кликбейта\n\n"
        "first_frame_type выбери одно значение из: ready_dish_closeup, cake_cut, cheese_pull, spoon_shot, "
        "grandma_shows_dish, cat_smells, unexpected_phrase, before_after, secret_tease.\n"
        "payoff_type выбери одно значение из: cut, spoon, crunch, cheese_pull, grandma_taste_smile, cat_reaction, "
        "final_plating, family_table, save_cta.\n"
        "hooks: emotional, intrigue, useful, cliffhanger; только содержательные варианты по рецепту, пустая группа допустима.\n"
        "Также верни attention_technique_audit, camera_technique_audit и editorial_review по правилам выше.\n"
        "final_phrases: ровно 3 финальные фразы.\n"
        "analysis должен содержать оценки 1-10: hook_strength, first_frame_strength, retention_score, "
        "visual_payoff_score, emotional_warmth_score, recipe_clarity_score, saveability_score, "
        "brand_integration_safety_score, risk_of_boring_scene; и строки expected_rewatch_reason, "
        "expected_comment_reason, expected_save_reason.\n\n"
        "INPUT:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def scenes_plan_requirements() -> dict[str, Any]:
    return {
        "scene_count": (
            "3-6 scenes inclusive, choose the minimum sufficient count; simple assembly usually 3-4, use 5-6 only for distinct essential transformations. Scene 1 introduces the dish, then cover full recipe steps "
            "in order from scene 2 to final serving"
        ),
        "duration_sec": "veo3: 6/8; other models: >=2",
        "scene1_hook": True,
        "scene1_ready_dish_no_cooking": True,
        "last_scene_result": True,
        "core_cast_rule": "grandmother and exactly one orange cat are present in every ordinary scene",
        "all_scenes_with_speech": True,
        "recipe_stages_progression": True,
        "no_generic_simple_action_text": True,
        "must_have_food_every_scene": True,
        "must_explain_recipe_step_every_scene": True,
        "continuity_anchors_required": (
            "every scene must include continuity_anchors: 3-6 concrete stable visual anchors that should remain "
            "consistent across adjacent frames, such as the same pot, pan, bowl, plate, cutting board, stovetop, "
            "wooden tabletop, spoon, whisk, ingredient pile, sauce texture, garnish bowl, or cat position"
        ),
        "continuity_state_required": (
            "every scene must include continuity_state: one short sentence describing the current state of food, "
            "cookware and props at this exact recipe moment"
        ),
        "no_location_contract": (
            "do not use a location field, allowed locations, location assets, or location references; preserve "
            "visual consistency through props, surfaces, cookware, ingredients and character sheets instead"
        ),
        "cutting_rule": (
            "if products are cut/sliced/chopped, explicitly specify cut style "
            "(e.g. small dice, thin strips, rings, slices)"
        ),
        "dish_rule": "if scene presents a dish/final serving, add detailed dish_description with visible ingredients",
        "ingredient_source_of_truth_rule": (
            "visible ingredients, toppings, sides, and garnishes must come from full_recipe_text, explicit user feedback, "
            "brand_context, or an explicit user reference. Do not add extra garnish, side dishes, eggs, herbs, potatoes, "
            "sauces, cheese, berries, nuts, or other ingredients just because they are common in some variants."
        ),
        "mandatory_recipe_coverage_rule": (
            "Все обязательные операции из рецепта должны быть видимо покрыты в ролике: готовка/варка, жарка/обжарка, "
            "духовка/запекание и другие ключевые технологические шаги нельзя пропускать или заменять общей фразой. "
            "Все важные ингредиенты из рецепта должны появляться в соответствующих сценах в правильном состоянии обработки; "
            "если ингредиент важен для вкуса, начинки, соуса, теста или финальной подачи, он должен быть явно виден или "
            "явно использован в кадре."
        ),
        "forbidden_shot_type": "never use low-angle / from-below camera",
        "scene_transition_rule": (
            "each next scene should progress recipe action and vary shot_type or continuity focus, "
            "but every scene must still obey frame_framing_rule (no distant wide shots)"
        ),
        "frame_framing_rule": (
            "EVERY scene, for start/end frame generation: composition is always tight food-first waist-up composition, on grandma, "
            "cat, and the hands-on food action (faces, paws, ingredients, utensils readable). "
            f"Exception: {FRUIT_ONLY_FIRST_MOMENT_RULE} "
            "Frame grandmother waist-up, with face and head fully inside the frame "
            "and hands and food large in the foreground; "
            "Other characters must be recognizable; their lower bodies may be outside the frame. No need to show floor, legs or furniture legs. "
            "No wide/establishing shots where characters are small. "
            "Even if shot_type suggests top/overhead, the frame must still show grandma+cat+action large "
            "(e.g. hands and food dominant, characters not tiny). "
            "Between scenes vary shot type, camera angle or continuity focus while keeping recurring props consistent. "
            "For veo3_fast use a single continuous shot with a locked camera: no cuts, zoom, "
            "pan, tracking, reframing or time jumps; one simple main action."
        ),
        "veo3_speech_text": (
            "if model is veo3 or veo3_fast, field text must be plain readable spoken text without stress markers, "
            "pronunciation annotations, ALL CAPS emphasis, hyphen-minus, en dash, or em dash; optional [пауза] "
            "may appear only at logical phrase boundaries"
        ),
        "speech_plan_rule": (
            "every scene must include speech_plan with 1-3 spoken chunks in order; each chunk should be a natural phrase "
            "from the line, ready for later subtitle timing"
        ),
        "speech_length_budget": (
            "max speakable text length ~= duration_sec * 14 chars for ru, up to 125 chars for 8s ru clips, "
            "and a stricter budget for en; "
            "count excludes every [пауза] token"
        ),
        "speech_style": (
            "viral Shorts: живой разговорный русский бабушки, короткие тёплые фразы, энергия без канцелярита; "
            "реплики должны звучать как человек в кадре, а не как инструкция из рецепта. "
            "Обращайся к зрителю, используй естественные связки вроде «смотрите», «теперь», «вот так», "
            "но без однообразного «я» в каждой фразе подряд"
        ),
        "speech_first_person_pov": (
            "Поле `text` — прямая реплика бабушки зрителю; пиши от первого лица (я/мы), НЕ повествование "
            "от третьего лица («бабушка говорит», «бабушка режет»). "
            "Не используй клише от лица зрителя или автора: «моя бабушка», «у моей бабушки», "
            "«как у моей бабушки», «бабушка научила», «рецепт моей бабушки», если такой факт явно не дан "
            "в full_recipe_text, feedback или generation_comment. Бабушка говорит сама, поэтому естественно: "
            "«я готовлю», «мы добавляем», «смотрите», а не «моя бабушка готовит». "
            "Сцена 1: хук + явно что готовим; в самом `text` первой сцены обязательно назови блюдо из concept "
            "или full_recipe_text (например смотрите / сегодня мы или я готовим сырники). Нельзя говорить "
            "служебные формулировки вроде «рецепт пользователя», «это блюдо», «вкусное блюдо» вместо названия. "
            "Последняя сцена: презентация готового блюда от первого лица (подаю / смотрите как аппетитно и т.п.). "
            "Промежуточные сцены: короткая реплика я/мы про текущий шаг рецепта. "
            "Сервер не переписывает `text` ради лица и рода речи — сразу выдавай финальный вариант."
        ),
        "feedback_application_rule": (
            "If feedback is not empty, treat it as a patch list. Apply every requested change to the named scene(s) "
            "and keep unaffected scenes as close as possible to previous_scenes. Do not replace the user's concrete "
            "wording with generic equivalents. If the user says that an amount, moment, reason or method is unclear, "
            "the revised spoken scene text must explicitly answer that missing point; merely restating the old text "
            "does not satisfy the feedback."
        ),
        "no_extra_characters_or_actions_rule": (
            "Do not invent extra characters, duplicate animals, background people, extra hands, or helper figures "
            "beyond scene.characters. Do not add parallel cooking actions that are not required for the current "
            "recipe step. Each scene should have one clear main cooking action plus only small supporting reactions "
            "from the listed characters."
        ),
        "video_prompt_composition_rule": (
            "Project-level rules like cartoon grandma/cartoon cat and kitchen physics must shape how you compose "
            "video_prompt_en and scene actions, but do not dump them into the result as bulky meta-rule labels "
            "unless explicitly requested."
        ),
        "final_scene_cta_rule": (
            "The LAST scene text must end with a subscribe call after the main food phrase. Required wording for the "
            f"viewer-facing phrase: {FINAL_SCENE_SUBSCRIBE_CTA}. For veo3/veo3_fast use the TTS-ready equivalent "
            f"at the very end: {FINAL_SCENE_SUBSCRIBE_CTA_VEO3}."
        ),
    }


def build_scenes_plan_payload(
    concept: str,
    full_recipe_text: str,
    feedback: str,
    previous_scenes: list[dict[str, Any]],
    allowed_locations: list[str] | None = None,
    viral_concept_brief: dict[str, Any] | None = None,
    sponsored_content: bool = False,
    brand_context: str | None = None,
    analytics_feedback: dict[str, Any] | None = None,
    romantic_drama_subplot: dict[str, Any] | None = None,
    generation_comment: str | None = None,
) -> dict[str, Any]:
    _ = allowed_locations
    if romantic_drama_subplot is not None:
        subplot = romantic_drama_subplot
    elif previous_scenes:
        subplot = {"enabled": False, "characters": [], "brief": "", "decision_scope": "episode"}
    else:
        subplot = maybe_choose_romantic_subplot()
    return {
        "concept": concept,
        "full_recipe_text": full_recipe_text,
        "feedback": feedback,
        "generation_comment": (generation_comment or "").strip(),
        "dialogue_feedback_hint": _dialogue_feedback_hint(feedback),
        "previous_scenes": previous_scenes,
        "assets": _character_assets_only(),
        "character_short_descriptions": _character_short_descriptions(),
        "required_characters": ["grandma", "cat"],
        "romantic_drama_subplot": subplot,
        "requirements": scenes_plan_requirements(),
        "viral_concept_brief": viral_concept_brief or {},
        "sponsored_content": bool(sponsored_content),
        "brand_context": brand_context or "",
        "analytics_feedback": analytics_feedback or {},
    }


def build_scenes_plan_prompt(payload: dict[str, Any]) -> str:
    return (
        GRANDMA_STORY_RULES + GRANDMA_OBSERVED_EXAMPLES + GRANDMA_RETENTION_REVIEW_RULES + "Ты режиссер вирусного кулинарного Shorts. По ПОЛНОМУ тексту рецепта ниже построй сценарий из 3–6 сцен.\n"
        "Return ONLY valid JSON array of scenes. No markdown.\n"
        "Для этого ответа аудит каталогов выполняй внутренне с учётом brief; не добавляй объект аудита вне массива. В каждом объекте сцены обязательно сохрани camera_design.\n"
        "КРИТИЧЕСКОЕ ПРАВИЛО: ровно 3–6 сцен; по порядку покрой весь рецепт от подготовки/старта до подачи; "
        "не выдумывай другое блюдо и не опускай ключевые этапы из текста рецепта.\n"
        "GENERATION COMMENT: если payload.generation_comment непустой, это обязательная правка ко всему ролику; "
        "учти её в сценах, репликах, хуке и production-подготовке, не ограничивай её только viral brief.\n"
        "INGREDIENT SOURCE OF TRUTH: все видимые ингредиенты, гарниры, топпинги, соусы и гарнирные элементы "
        "должны быть взяты из full_recipe_text, явного feedback, brand_context или пользовательских референсов. "
        "Не добавляй продукты «для красоты» или потому что они встречаются в отдельных вариантах блюда, если их нет "
        "в исходном рецепте/запросе. Это правило особенно важно для scene 1 ready dish и финальной подачи.\n"
        "MANDATORY RECIPE COVERAGE: все обязательные операции из рецепта должны быть видимо покрыты в ролике: "
        "готовка/варка, жарка/обжарка, духовка/запекание и другие ключевые технологические шаги нельзя пропускать "
        "или заменять общей фразой. Все важные ингредиенты должны появляться в соответствующих сценах в правильном "
        "состоянии обработки.\n"
        "В КАЖДОЙ сцене: конкретное действие с едой; понятное объяснение шага; реакция бабушки и рыжего кота. "
        "Рыжий кот является постоянным персонажем проекта и должен быть в characters каждой обычной сцены.\n"
        "В КАЖДОЙ сцене обязательно непустое поле text — готовая реплика озвучки на этом этапе.\n"
        "Для каждой сцены верни dialogue_mode и dialogue.\n"
        "- dialogue_mode: single или multi\n"
        "- dialogue: массив объектов {speaker, text}\n"
        "- Для single можно вернуть пустой dialogue или один объект с бабушкой.\n"
        "- Для multi dialogue обязателен, а text должен быть агрегированной spoken-версией этих реплик в порядке произнесения.\n"
        "Реплика text: по умолчанию от первого лица бабушки (я/мы) к зрителю; запрет на третье лицо («бабушка …» как рассказчик). "
        "Запрещены неподтвержденные ностальгические клише от лица автора/зрителя: «моя бабушка», «у моей бабушки», "
        "«как у моей бабушки», «бабушка научила», «рецепт моей бабушки», если это прямо не написано в исходном рецепте, "
        "feedback или generation_comment. "
        f"{RUSSIAN_YO_RULE} "
        "В реплике озвучки text не используй дефис и тире: запрещены символы -, – и —; паузы обозначай только [пауза]. "
        "Первая сцена — хук + что именно готовим; в поле text первой сцены обязательно произнеси название блюда "
        "из concept/full_recipe_text, а не только покажи его в action или dish_description. "
        "Не используй вместо названия служебные фразы «рецепт пользователя», «это блюдо», «вкусное блюдо» — "
        "выведи конкретное название из текста рецепта.\n"
        "Последняя — подача готового от первого лица; промежуточные — я/мы про шаг. "
        "Пиши реплики максимально живыми: как настоящая бабушка в кадре, тёпло и разговорно; без сухих формул "
        "«выполняет следующий шаг», «добавляем ингредиенты», «готовое блюдо» там, где можно сказать естественно.\n"
        "Код не правит лицо и формулировки реплик — всё сразу в ответе модели (см. requirements.speech_first_person_pov).\n"
        "В КАЖДОЙ обычной сцене явно укажи characters с `grandma` и `cat`: бабушка и ровно один рыжий кот всегда видны в кадре. "
        "Исключение только если пользователь явно просит первый кадр без бабушки/кота, например только говорящие фрукты. Поле location НЕ добавляй.\n"
        "FAMILY CAST RULE: смотри payload.viral_concept_brief.dish_audience_type и family_cast_plan.\n"
        "Явный состав в family_cast_plan и правки пользователя важнее автоматической категории блюда. Если только бабушка и кот, никого не добавляй даже при child/male.\n"
        "- Если dish_audience_type=child, в сцене 1 и в последней сцене добавь ровно одного из персонажей grandchild: `grandson` ИЛИ `granddaughter`; "
        "выбери одного и держи его консистентно в обеих сценах. В промежуточных сценах этого персонажа не добавляй, если пользователь прямо не просил.\n"
        "- Если dish_audience_type=male, в сцене 1 и в последней сцене добавь `grandfather`. В промежуточных сценах дедушки быть не должно, если пользователь прямо не просил.\n"
        "- Если dish_audience_type=neutral, не добавляй дедушку, внука или внучку без прямой просьбы пользователя.\n"
        "- Для child допустим короткий живой диалог или реакция выбранного внука/внучки в первой сцене и/или финале, но готовка всё равно остаётся у бабушки.\n"
        "- Для male в первой сцене допустим короткий диалог: дедушка пробует/реагирует, бабушка отвечает.\n"
        "ANTHROPOMORPHIC INGREDIENT DIALOGUE RULE: если пользователь явно просит, чтобы еда/ингредиенты говорили "
        "(например банан и клубника разговаривают в сцене 1), не заменяй эти реплики на бабушку/внука/внучку. "
        "Добавь таких говорящих ингредиентов в scene.characters как простые canonical keys (`banana`, `strawberry`) "
        "только для нужной сцены, сохрани их реплики в dialogue, а бабушка/внук/внучка могут говорить только свои "
        "отдельно заданные реплики.\n"
        f"FIRST MOMENT VISIBILITY RULE: {FRUIT_ONLY_FIRST_MOMENT_RULE}\n"
        "CHARACTER KEYS AND SHORT DESCRIPTIONS: canonical keys остаются служебными id, а в любых не-диалоговых "
        "полях используй только короткие описания из payload.character_short_descriptions: `cat` = рыжий кот, "
        "`zaya` = белая кошка, `markiz` = черно-белый кот, остальные аналогично. Имена персонажей можно использовать "
        "только внутри поля text, если это звучит как прямая речь. В action, visual_description, micro_events, "
        "romantic_drama_episode, romantic_drama_beat и любых prompt context пиши короткие описания, не имена. "
        "Если добавляешь белую кошку или черно-белого кота, обязательно добавь их canonical key в scene.characters. "
        "CAT COUNT RULE: один canonical key = ровно один кот. `cat` означает только рыжего кота, `zaya` только белую кошку, "
        "`markiz` только черно-белого кота. Не создавай дополнительных котов, котят, силуэты котов, безымянных котов или "
        "дубликаты того же кота. Запрещены два рыжих кота, две белые кошки или два черно-белых кота в одной сцене; "
        "если в сцене несколько котов, они обязаны быть разными canonical identities из characters и визуально разного окраса. "
        "В описании одного кота не смешивай признаки другого кота.\n"
        "PLACEMENT RULE FOR NEIGHBOR CATS: белая кошка не закреплена за одной точкой кадра и может находиться внутри кухни, "
        "у окна, в дверном проеме, рядом со столом или в другой позиции, если это помогает сцене и остается "
        "безопасным для кулинарного действия. Не пиши «на подоконнике» без прямой просьбы пользователя. "
        "Не привязывай белую кошку по умолчанию к позиции за окном. Черно-белый кот может "
        "быть ТОЛЬКО ЗА ОКНОМ, снаружи кухни. Черно-белый кот никогда не находится внутри кухни, на столе, на полу кухни или "
        "рядом с посудой внутри. Если `markiz` выбран, каждый его beat должен явно говорить, что он за окном/снаружи окна.\n"
        "Не добавляй подробные описания внешности персонажей в scene fields или video_prompt; внешний вид будет "
        "подставлен отдельно только на этапе генерации фреймов.\n"
        "ROMANTIC DRAMA SUBPLOT: решение принимается на уровне всего ролика в payload.romantic_drama_subplot. "
        "Если enabled=false, не добавляй белую кошку или черно-белого кота, если пользователь прямо не просит их в feedback. "
        "Если enabled=true, ТЫ САМ придумываешь конкретный кейс драмы под рецепт, viral brief и действия ролика. "
        "Не используй шаблонную драму: придумай свежую мини-арку, связанную с едой, предметами, выбором, ожиданием, "
        "ошибкой, потерей, подозрением, защитой, разлукой, примирением, неловкостью, маленькой тайной или другим "
        "понятным человеческим напряжением этого ролика. Драма не обязана быть романтической и не должна сводиться "
        "к ревности у окна. Драма должна быть ОСМЫСЛЕННОЙ: понятная завязка в первой сцене, развитие в середине и "
        "видимый понятный зрителю хэппи-энд в последней сцене. Не делай случайный конфликт без причины и без закрытия. "
        "В каждой сцене добавь одинаковый объект romantic_drama_episode с ключами: brief, zaya_timosha_arc, markiz_arc, happy_end "
        "(markiz_arc пустой, если markiz не выбран). В happy_end кратко опиши, как финальная сцена визуально закрывает драму "
        "с хорошим исходом. Также в каждой сцене добавь romantic_drama_beat — короткий beat "
        "именно этой сцены. Линия с белой кошкой должна идти через ВЕСЬ ролик: добавь `zaya` в characters "
        "каждой сцены, и в каждой сцене action или micro_events должен быть короткий фоновый beat из придуманной арки. "
        "Если в subplot.characters есть `markiz`, добавь `markiz` в characters каждой сцены и веди его линию через "
        "весь ролик по markiz_arc. В последней сцене romantic_drama_beat обязательно показывает happy_end: напряжение "
        "между персонажами получает ясное визуальное разрешение, а если выбран черно-белый кот, он принимает исход без "
        "злости или получает свой маленький утешительный смешной момент за окном. Рецепт, еда и бабушка остаются главными; драма не заменяет кулинарный шаг. "
        "Не превращай ролик в отдельную историю: это сквозная эмоциональная линия на фоне готовки.\n"
        "В КАЖДОЙ сцене добавь continuity_anchors: 3-6 конкретных предметов/опор кадра для консистентности "
        "(например та же кастрюля, миска, тарелка, разделочная доска, плита/конфорка как предмет, ложка, венчик, "
        "ингредиенты, поверхность стола, поза кота). Добавь continuity_state: текущее состояние еды и посуды.\n"
        "СЦЕНА 1: сильный хук с первого кадра. По умолчанию это аппетитная витрина с блюдом, "
        "НО если пользователь явно попросил другой первый кадр/другой хук для сцены 1, следуй запросу пользователя; "
        "короткая hook-first реплика с обязательным названием блюда в `text` (зритель должен услышать название в первом кадре). "
        "Только в сцене 1 можно использовать dialogue_mode=multi: короткий диалог 2-3 реплики максимум суммарно между бабушкой и дедушкой/ребёнком "
        "или явно указанными пользователем антропоморфными ингредиентами. "
        "Диалог должен усиливать hook, а не заменять блюдо и визуальный payoff. "
        "Если payload.dialogue_feedback_hint не пустой, перенеси указанные реплики и роли максимально дословно.\n"
        "Если concept выглядит служебно или обобщенно, возьми конкретное название блюда из full_recipe_text. "
        "ПЕРВАЯ РЕПЛИКА: начни с конкретного блюда и узнаваемого повода, проблемы или желания. "
        "Предпочитай короткую законченную мысль; утверждённую пользователем фразу не сокращай ради числа слов. "
        "Не требуй слов про ложку, разрез, секрет или конец ролика. Визуальный интерес не обязан проговариваться. "
        "viral_concept_brief предлагает направление, но feedback и generation_comment важнее. "
        "Удалённые пользователем мотивы не возвращай из brief в кадры или финал. "
        "Первая сцена должна содержать одно из: готовое блюдо крупным планом, разрез торта, тянущийся сыр, "
        "ложка с супом/кремом, бабушка показывает готовое блюдо, кот реагирует на запах, неожиданная фраза, "
        "контраст было/стало, ностальгический домашний момент, честная интрига про финальный кадр; либо другой явный пользовательский hook-first кадр.\n"
        "Готовка по рецепту (действия типа режем/мешаем/жарим) начинается строго со СЦЕНЫ 2.\n"
        "ПОСЛЕДНЯЯ СЦЕНА: завершение последнего кулинарного действия вместе с подачей и репликой от первого лица; не выделяй подачу в отдельную сцену, если её можно естественно завершить здесь. "
        "Финал обязан иметь сильный visual payoff: разрез, ложка, хруст, тянущийся сыр, бабушка пробует и улыбается, "
        "кот смотрит на блюдо, финальная сервировка, семейный стол или «сохрани, чтобы не потерять». "
        "После основной фразы про блюдо ОБЯЗАТЕЛЬНО закончи text подписочной фразой: "
        f"{FINAL_SCENE_SUBSCRIBE_CTA}. Для veo3/veo3_fast в самом конце text используй TTS-форму с финальной точкой: "
        f"{FINAL_SCENE_SUBSCRIBE_CTA_VEO3}.\n"
        "RETENTION LAYER: у каждой сцены добавь retention_function — одно из hook, visual_proof, recipe_progress, "
        "grandma_secret, cat_humor, texture_shot, transformation, suspense, payoff, cta_save_reason, brand_integration. "
        "Добавь micro_events: не более двух пунктов, одно главное простое действие и необязательная милая реакция кота в том же непрерывном кадре. Никаких вставок, смены крупности, склеек и движения камеры. "
        "Если действие слишком долгое, перенеси подготовку за кадр между сценами и начни сцену с подготовленного состояния. Никакого ускорения или монтажа внутри сцены.\n"
        "Для sponsored_content=true встрои бренд только нативно в одну подходящую сцену; не делай отдельный рекламный блок.\n"
        "При нарезке — явно стиль нарезки (кубик соломка ломтики и т.д.).\n"
        "Не добавляй лишних персонажей, дубли котов, фоновых людей, лишние руки или параллельные действия, "
        "которых нет в текущем шаге рецепта. Дубли котов означает именно повтор той же identity/того же окраса: "
        "два рыжих, две белые или два черно-белых кота запрещены; разные коты допустимы только если перечислены в characters. "
        "На сцену — одно главное кулинарное действие, остальное только "
        "как короткие поддерживающие реакции персонажей.\n"
        "Правила cartoon grandma/cartoon cat и кухонной физики должны влиять на формулировки scene/action/video_prompt, "
        "но не вставляй в результат громоздкие служебные метки или списки правил.\n"
        "ЗАПРЕЩЕН ракурс снизу (low-angle/from-below).\n"
        "КАДРИРОВАНИЕ: в каждой сцене только крупный план, не средний и не средне-крупный; бабушка целиком видна (не только лицо), все персонажи сцены полностью в кадре, действие с едой читаемо; без дальних планов.\n"
        "Для veo3_fast: в video_prompt обязательны липсинк с репликой и смена ракурса/кадра в течение ролика (не один зафиксированный ракурс на всю длительность).\n"
        "Соседние сцены меняют shot_type/ракурс или фокус действия, но повторяют важные continuity_anchors "
        "из предыдущих сцен, если те предметы ещё должны быть в кадре.\n"
        "Для финала с блюдом — dish_description с ингредиентами.\n"
        "Поле text для veo3/veo3_fast: обычный человекочитаемый текст без ударений и спецметок произношения; "
        "без дефиса и тире (символы -, – и — запрещены); [пауза] только между смысловыми фразами; "
        "длина речи без всех подстрок [пауза] не больше speech budget.\n"
        "Для каждой сцены верни speech_plan: 1-3 коротких spoken chunks в порядке произнесения, чтобы потом по ним можно было собрать тайминги субтитров.\n"
        "Если в payload есть непустой feedback или previous_scenes — это режим РЕДАКТИРОВАНИЯ, а не генерации с нуля. "
        "Сначала прочитай previous_scenes, затем примени feedback как точные правки по указанным сценам: "
        "если feedback содержит нумерованный список накопленных правок, выполни каждый пункт по порядку; "
        "новая правка отменяет старую только при явном противоречии, иначе нельзя терять ни один предыдущий пункт. "
        "если пользователь пишет «в сцене 2 добавляем слово...», «в сцене 3 сразу говорим...», «пауза не нужна...» — "
        "измени именно эти scene.text/action/visual_description/dialogue и не игнорируй ни один пункт. "
        "Сцены без упомянутых правок сохраняй максимально близко к previous_scenes, сохранив блюдо и логику рецепта.\n"
        "ЗАПРЕЩЕНО сухое «готовит» без деталей. voice_needed и lip sync; модель veo3_fast предпочтительна.\n\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
