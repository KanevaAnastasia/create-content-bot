"""
Текстовые шаги пайплайна: единый контракт «предыдущий ответ + фидбек → Gemini».

Промпты сгруппированы по шагам; генерация кадров и видео остаётся в других модулях (Vertex и т.д.).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .assets import ASSETS
from .llm_utils import gemini_generate_sync


FINAL_SCENE_SUBSCRIBE_CTA = "Подписывайтесь на бабушку, тут много вкусных и простых рецептов"
FINAL_SCENE_SUBSCRIBE_CTA_VEO3 = (
    "подписЫвайтесь на бАбушку [пауза] тут мнОго вкУсных и простЫх рецЕптов"
)


async def gemini_text(prompt: str) -> str:
    """Вызывает синхронный Gemini в thread pool, чтобы не блокировать event loop бота."""
    return (await asyncio.to_thread(gemini_generate_sync, prompt) or "").strip()


def allowed_locations_strict() -> list[str]:
    """Legacy hook: сюжетные локации больше не передаются в LLM-контракт."""
    return []


def _character_assets_only() -> dict[str, Any]:
    """Возвращает только character assets; location assets остаются в проекте, но не уходят в LLM."""
    return {"characters": ASSETS.get("characters") or {}}


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
        "Ты сценарный редактор уютного food/cartoon-блога с бабушкой, котом и домашними рецептами.\n"
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
        "payoff_type, brand_integration_plan, analysis.\n\n"
        "strongest_first_phrase — самая важная строка ролика. Правила:\n"
        "- 6-12 слов, максимум 14 слов\n"
        "- первая фраза должна сработать за первые 1-2 секунды\n"
        "- обязательно назови конкретное блюдо или узнаваемую часть блюда\n"
        "- сразу дай open loop или payoff: секрет, разрез, хруст, тянущийся сыр, ложка, нежный крем, кот реагирует, "
        "домашнее детство, «нельзя терять», «в конце покажу»\n"
        "- не начинай с обычного вступления «сегодня готовим», «мы приготовим», «давайте сделаем», «в этом видео»\n"
        "- не обещай то, чего нет в рецепте или финале\n"
        "- тон мягкий и уютный, без агрессивного кликбейта\n\n"
        "first_frame_type выбери одно значение из: ready_dish_closeup, cake_cut, cheese_pull, spoon_shot, "
        "grandma_shows_dish, cat_smells, unexpected_phrase, before_after, secret_tease.\n"
        "payoff_type выбери одно значение из: cut, spoon, crunch, cheese_pull, grandma_taste_smile, cat_reaction, "
        "final_plating, family_table, save_cta.\n"
        "hooks должен быть объектом: emotional 3 шт, intrigue 3 шт, useful 2 шт, cliffhanger 2 шт.\n"
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
            "4-6 scenes inclusive; scene 1 is hook-only with ready dish, then cover full recipe steps "
            "in order from scene 2 to final serving"
        ),
        "duration_sec": "veo3: 6/8; other models: >=2",
        "scene1_hook": True,
        "scene1_ready_dish_no_cooking": True,
        "last_scene_result": True,
        "cat_active_every_scene_new_emotion": True,
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
        "forbidden_shot_type": "never use low-angle / from-below camera",
        "scene_transition_rule": (
            "each next scene should progress recipe action and vary shot_type or continuity focus, "
            "but every scene must still obey frame_framing_rule (no distant wide shots)"
        ),
        "frame_framing_rule": (
            "EVERY scene, for start/end frame generation: composition is always close-up or medium-close on grandma, "
            "cat, and the hands-on food action (faces, paws, ingredients, utensils readable). "
            "Grandmother's full figure must remain visible (at least head through torso), never a face-only crop; "
            "every other character listed for the scene must also stay fully visible in frame. "
            "No wide/establishing shots where characters are small. "
            "Even if shot_type suggests top/overhead, the frame must still show grandma+cat+action large "
            "(e.g. hands and food dominant, characters not tiny). "
            "Between scenes vary shot type, camera angle or continuity focus while keeping recurring props consistent. "
            "For veo3_fast video_prompt (motion): require lip sync with the Russian line; camera angle or framing "
            "should evolve during the clip—not one locked static tripod shot for the whole take."
        ),
        "veo3_speech_text": (
            "if model is veo3 or veo3_fast, field text must be Veo3 TTS-ready: NO punctuation; "
            "mark stressed vowels with a CAPITAL letter inside each word; replace punctuation with token [пауза] "
            "only at logical phrase boundaries (do not put [пауза] after every word); "
            "do not use FULL CAPS words for emphasis"
        ),
        "speech_length_budget": (
            "max speakable text length ~= duration_sec * 14 chars excluding every [пауза] token (pauses not counted)"
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
            "Сцена 1: хук + явно что готовим (например смотрите / сегодня мы или я готовим …). "
            "Последняя сцена: презентация готового блюда от первого лица (подаю / смотрите как аппетитно и т.п.). "
            "Промежуточные сцены: короткая реплика я/мы про текущий шаг рецепта. "
            "Сервер не переписывает `text` ради лица и рода речи — сразу выдавай финальный вариант."
        ),
        "feedback_application_rule": (
            "If feedback is not empty, treat it as a patch list. Apply every requested change to the named scene(s) "
            "and keep unaffected scenes as close as possible to previous_scenes. Do not replace the user's concrete "
            "wording with generic equivalents."
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
) -> dict[str, Any]:
    _ = allowed_locations
    return {
        "concept": concept,
        "full_recipe_text": full_recipe_text,
        "feedback": feedback,
        "previous_scenes": previous_scenes,
        "assets": _character_assets_only(),
        "required_characters": ["grandma", "cat"],
        "requirements": scenes_plan_requirements(),
        "viral_concept_brief": viral_concept_brief or {},
        "sponsored_content": bool(sponsored_content),
        "brand_context": brand_context or "",
        "analytics_feedback": analytics_feedback or {},
    }


def build_scenes_plan_prompt(payload: dict[str, Any]) -> str:
    return (
        "Ты режиссер вирусного кулинарного Shorts. По ПОЛНОМУ тексту рецепта ниже построй сценарий из 4–6 сцен.\n"
        "Return ONLY valid JSON array of scenes. No markdown.\n"
        "КРИТИЧЕСКОЕ ПРАВИЛО: ровно 4–6 сцен; по порядку покрой весь рецепт от подготовки/старта до подачи; "
        "не выдумывай другое блюдо и не опускай ключевые этапы из текста рецепта.\n"
        "В КАЖДОЙ сцене: конкретное действие с едой; понятное объяснение шага; реакция бабушки; кот в кадре по смыслу.\n"
        "В КАЖДОЙ сцене обязательно непустое поле text — готовая реплика озвучки на этом этапе.\n"
        "Реплика text: только от первого лица бабушки (я/мы) к зрителю; запрет на третье лицо («бабушка …» как рассказчик). "
        "Первая сцена — хук + что именно готовим; последняя — подача готового от первого лица; промежуточные — я/мы про шаг. "
        "Пиши реплики максимально живыми: как настоящая бабушка в кадре, тёпло и разговорно; без сухих формул "
        "«выполняет следующий шаг», «добавляем ингредиенты», «готовое блюдо» там, где можно сказать естественно.\n"
        "Код не правит лицо и формулировки реплик — всё сразу в ответе модели (см. requirements.speech_first_person_pov).\n"
        "В КАЖДОЙ сцене явно укажи characters (всегда grandma и cat), но НЕ добавляй поле location.\n"
        "В КАЖДОЙ сцене добавь continuity_anchors: 3-6 конкретных предметов/опор кадра для консистентности "
        "(например та же кастрюля, миска, тарелка, разделочная доска, плита/конфорка как предмет, ложка, венчик, "
        "ингредиенты, поверхность стола, поза кота). Добавь continuity_state: текущее состояние еды и посуды.\n"
        "СЦЕНА 1: только сильный хук и аппетитная витрина — крупно бабушка, кот и уже готовое блюдо; "
        "короткая hook-first реплика (не описание блюда, а причина остановиться). "
        "FIRST PHRASE HARD RULES: первая реплика сцены 1 должна быть 6-12 слов максимум 14 слов; "
        "начинай сразу с интриги/эмоции/payoff, а не с приветствия и не с «сегодня готовим». "
        "В первой фразе обязательно есть конкретное блюдо + причина досмотреть: секрет, разрез, хруст, тянущийся сыр, "
        "ложка, нежный крем, кот реагирует, домашнее детство или «нельзя терять». "
        "Если viral_concept_brief.strongest_first_phrase подходит, используй её дословно или сделай сильнее, но не слабее. "
        "Запрещены первые фразы: «сегодня мы готовим...», «давайте приготовим...», «в этом видео...», "
        "«это вкусное блюдо...», «простой рецепт...», если дальше нет немедленного payoff/open loop. "
        "В сцене 1 НЕ начинай процесс готовки. "
        "Если в viral_concept_brief есть strongest_first_frame/first_frame_type — следуй им. "
        "Первая сцена должна содержать одно из: готовое блюдо крупным планом, разрез торта, тянущийся сыр, "
        "ложка с супом/кремом, бабушка показывает готовое блюдо, кот реагирует на запах, неожиданная фраза, "
        "контраст было/стало, интрига «в конце покажу секрет».\n"
        "Готовка по рецепту (действия типа режем/мешаем/жарим) начинается строго со СЦЕНЫ 2.\n"
        "ПОСЛЕДНЯЯ СЦЕНА: презентация/подача готового блюда, аппетит, реплика от первого лица. "
        "Финал обязан иметь сильный visual payoff: разрез, ложка, хруст, тянущийся сыр, бабушка пробует и улыбается, "
        "кот смотрит на блюдо, финальная сервировка, семейный стол или «сохрани, чтобы не потерять». "
        "После основной фразы про блюдо ОБЯЗАТЕЛЬНО закончи text подписочной фразой: "
        f"{FINAL_SCENE_SUBSCRIBE_CTA}. Для veo3/veo3_fast в самом конце text используй TTS-форму без пунктуации: "
        f"{FINAL_SCENE_SUBSCRIBE_CTA_VEO3}.\n"
        "RETENTION LAYER: у каждой сцены добавь retention_function — одно из hook, visual_proof, recipe_progress, "
        "grandma_secret, cat_humor, texture_shot, transformation, suspense, payoff, cta_save_reason, brand_integration. "
        "Добавь micro_events: 2-4 коротких события каждые 2-3 секунды (смена крупности, ингредиент, реакция бабушки, "
        "реакция кота, изменение текстуры, food close-up, мини-шутка, подсказка, переход, трансформация). "
        "Если одно действие может длиться слишком долго, укажи retention_suggestion: монтажное ускорение, нарезка или замена.\n"
        "Для sponsored_content=true встрои бренд только нативно в одну подходящую сцену; не делай отдельный рекламный блок.\n"
        "При нарезке — явно стиль нарезки (кубик соломка ломтики и т.д.).\n"
        "ЗАПРЕЩЕН ракурс снизу (low-angle/from-below).\n"
        "КАДРИРОВАНИЕ: в каждой сцене крупный или средне-крупный план; бабушка целиком видна (не только лицо), все персонажи сцены полностью в кадре, действие с едой читаемо; без дальних планов.\n"
        "Для veo3_fast: в video_prompt обязательны липсинк с репликой и смена ракурса/кадра в течение ролика (не один зафиксированный ракурс на всю длительность).\n"
        "Соседние сцены меняют shot_type/ракурс или фокус действия, но повторяют важные continuity_anchors "
        "из предыдущих сцен, если те предметы ещё должны быть в кадре.\n"
        "Для финала с блюдом — dish_description с ингредиентами.\n"
        "Поле text для veo3/veo3_fast: без знаков препинания; [пауза] между смысловыми фразами; ударные гласные ЗАГЛАВНЫЕ внутри слова; без слов, написанных ПОЛНОСТЬЮ КАПСОМ; "
        "длина речи без всех подстрок [пауза] не больше duration_sec * 14 символов.\n"
        "Если в payload есть непустой feedback или previous_scenes — это режим РЕДАКТИРОВАНИЯ, а не генерации с нуля. "
        "Сначала прочитай previous_scenes, затем примени feedback как точные правки по указанным сценам: "
        "если пользователь пишет «в сцене 2 добавляем слово...», «в сцене 3 сразу говорим...», «пауза не нужна...» — "
        "измени именно эти scene.text/action/visual_description и не игнорируй ни один пункт. "
        "Сцены без упомянутых правок сохраняй максимально близко к previous_scenes, сохранив блюдо и логику рецепта.\n"
        "ЗАПРЕЩЕНО сухое «готовит» без деталей. voice_needed и lip sync; модель veo3_fast предпочтительна.\n\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
