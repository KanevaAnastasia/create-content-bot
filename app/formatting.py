"""
Форматирование текста для отправки пользователю в Telegram.

Использует HTML: JSON оборачивается в ``<pre>`` для моноширинного отображения.
Человекочитаемые планы (сцены, концепты, шаги рецепта) — обычный текст.
"""

import html
import json
from typing import Any


def _safe_text(value: Any) -> str:
    """
    Безопасно приводит значение к строке для UI-форматирования.

    Args:
        value: Любой объект (строка, список, число, None).
    Returns:
        Строка без исключений:
        - `None` -> `""`;
        - список -> рекурсивная склейка через пробел;
        - остальное -> `str(value)`.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_safe_text(v) for v in value)
    return str(value)


def format_json_for_telegram(data: Any) -> str:
    """
    Сериализует произвольные данные в красивый JSON и экранирует для HTML.

    Args:
        data: Любая сериализуемая JSON-структура.
    Returns:
        HTML-строка вида `<pre>...</pre>` с экранированным JSON.

    Подходит для ``reply_text(..., parse_mode="HTML")`` — символы ``<>&`` в данных не ломают разметку.
    """
    return f"<pre>{html.escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"


def cost_label(model: str) -> str:
    """
    Возвращает грубую оценку стоимости одной сцены для выбранной модели (порядок величины).

    Args:
        model: Идентификатор модели генерации видео.
    Returns:
        Строка-оценка стоимости одной сцены.

    Это не биллинг, а ориентир для пользователя в human-readable плане.
    """
    if model == "veo3_fast":
        return "~0.01-0.03$"
    if model == "kling":
        return "~0.005-0.01$"
    if model == "heygen":
        return "~0.03-0.06$"
    return "~n/a"


def human_model_reason(scene: dict[str, Any]) -> str:
    """
    Краткое объяснение на русском, почему для сцены выбрана та или иная модель.

    Должно быть согласовано с логикой ``pick_model`` в ``scene_utils``.
    """
    model = scene.get("model")
    if model == "heygen":
        return "сложный lip sync крупным планом"
    if model == "veo3_fast":
        return "есть речь/озвучка, нужна выразительность"
    return "без речи, экономичный вариант"


def build_human_readable_plan(scenes: list[dict[str, Any]]) -> str:
    """
    Собирает многострочный текст «план сцен» для апрува пользователем.

    Для каждой сцены выводит: ракурс, continuity anchors, длительность, модель, lip sync, действие, текст, озвучку.
    Сцены разделяются ``---``.
    """
    blocks: list[str] = []
    for scene in scenes:
        scene_no = scene.get("scene", scene.get("scene_id", "?"))
        shot = scene.get("shot_type", "medium")
        anchors = scene.get("continuity_anchors") if isinstance(scene.get("continuity_anchors"), list) else []
        anchors_text = ", ".join(_safe_text(x) for x in anchors if _safe_text(x)) or "—"
        duration = scene.get("duration_sec", 4)
        voice_needed = bool(scene.get("voice_needed")) or bool(_safe_text(scene.get("text")).strip())
        model = scene.get("model", "veo3_fast" if voice_needed else "kling")
        has_lipsync = bool(scene.get("has_lipsync")) or bool(scene.get("lip_sync"))
        voice_needed = bool(scene.get("voice_needed")) or bool(_safe_text(scene.get("text")).strip())
        action = scene.get("action") or scene.get(
            "recipe_step", "Бабушка выполняет конкретный шаг рецепта, кот активно взаимодействует с ингредиентом."
        )
        text_line = _safe_text(scene.get("text")) or "—"
        retention_function = _safe_text(scene.get("retention_function")) or "—"
        micro_events_raw = scene.get("micro_events") if isinstance(scene.get("micro_events"), list) else []
        micro_events = ", ".join(_safe_text(x) for x in micro_events_raw if _safe_text(x)) or "—"
        risk = scene.get("boring_scene_risk", "—")
        suggestion = _safe_text(scene.get("retention_suggestion")) or "—"
        first_phrase_score = scene.get("first_phrase_score")
        first_phrase_review = scene.get("first_phrase_review") if isinstance(scene.get("first_phrase_review"), dict) else {}
        first_phrase_issues = first_phrase_review.get("issues") if isinstance(first_phrase_review.get("issues"), list) else []
        first_phrase_issue_text = ", ".join(_safe_text(x) for x in first_phrase_issues if _safe_text(x)) or "—"
        first_phrase_suggestion = _safe_text(scene.get("first_phrase_suggestion")) or _safe_text(first_phrase_review.get("suggestion")) or "—"
        first_phrase_block = (
            "Проверка первой фразы:\n"
            f"Оценка: {first_phrase_score}/10\n"
            f"Замечания: {first_phrase_issue_text}\n"
            f"Предложение: {first_phrase_suggestion}\n\n"
            if first_phrase_score is not None
            else ""
        )
        voice_provider = scene.get(
            "voice_provider", "native" if str(model).lower().startswith("veo3") else "elevenlabs"
        )
        blocks.append(
            f"СЦЕНА {scene_no}\n"
            f"Вид/ракурс: {shot}\n"
            f"Опоры консистентности: {anchors_text}\n"
            "Персонажи: бабушка, кот\n"
            f"Длительность: {duration} сек\n"
            f"Нейросеть: {model}\n"
            f"Lip sync: {'да' if has_lipsync else 'нет'}\n\n"
            "Что происходит:\n"
            f"{action}\n\n"
            "Retention:\n"
            f"Функция: {retention_function}\n"
            f"Микрособытия: {micro_events}\n"
            f"Риск скучной сцены: {risk}/10\n"
            f"Рекомендация: {suggestion}\n\n"
            f"{first_phrase_block}"
            "Фраза бабушки (будет в сцене):\n"
            f"{text_line}\n\n"
            f"Нужна ли озвучка: {'да' if voice_needed else 'нет'}\n\n"
            f"Провайдер озвучки: {voice_provider}\n\n"
            "Почему эта модель:\n"
            f"{human_model_reason(scene)}\n\n"
            f"Примерная стоимость: {cost_label(model)}"
        )
    return "\n\n---\n\n".join(blocks)


def _hook_lines(hooks: dict[str, Any]) -> list[str]:
    labels = {
        "emotional": "эмоциональные",
        "intrigue": "интригующие",
        "useful": "полезные",
        "cliffhanger": "cliffhanger",
    }
    lines: list[str] = []
    for key, label in labels.items():
        values = hooks.get(key) if isinstance(hooks, dict) else []
        if not isinstance(values, list):
            continue
        clean = [_safe_text(x) for x in values if _safe_text(x)]
        if clean:
            lines.append(f"{label}: " + " | ".join(clean))
    return lines


def format_viral_concept_brief(brief: dict[str, Any] | None) -> str:
    """Formats the Viral Concept Brief for Telegram approval context."""
    if not isinstance(brief, dict) or not brief:
        return "VIRAL CONCEPT BRIEF:\n—"
    analysis = brief.get("analysis") if isinstance(brief.get("analysis"), dict) else {}
    hooks = brief.get("hooks") if isinstance(brief.get("hooks"), dict) else {}
    hook_text = "\n".join(_hook_lines(hooks)) or "—"
    brand_plan = _safe_text(brief.get("brand_integration_plan")) or "—"
    return (
        "VIRAL CONCEPT BRIEF\n\n"
        f"Главный хук: {_safe_text(brief.get('main_hook')) or '—'}\n"
        f"Обещание зрителю: {_safe_text(brief.get('viewer_promise')) or '—'}\n"
        f"Причина досмотреть: {_safe_text(brief.get('reason_to_watch_end')) or '—'}\n"
        f"Визуальный payoff: {_safe_text(brief.get('main_visual_payoff')) or '—'}\n"
        f"Тон: {_safe_text(brief.get('emotional_tone')) or '—'}\n"
        f"Первая фраза: {_safe_text(brief.get('strongest_first_phrase')) or '—'}\n"
        f"Первый кадр: {_safe_text(brief.get('strongest_first_frame')) or '—'}\n"
        f"Тип первого кадра: {_safe_text(brief.get('first_frame_type')) or '—'}\n"
        f"Тип финала: {_safe_text(brief.get('payoff_type')) or '—'}\n\n"
        "Хуки:\n"
        f"{hook_text}\n\n"
        "Финальные фразы:\n"
        + "\n".join(f"- {_safe_text(x)}" for x in (brief.get("final_phrases") or []) if _safe_text(x))
        + "\n\n"
        f"Реклама: {brand_plan}\n\n"
        "Оценки:\n"
        f"hook={analysis.get('hook_strength', '—')}/10, "
        f"first_frame={analysis.get('first_frame_strength', '—')}/10, "
        f"retention={analysis.get('retention_score', '—')}/10, "
        f"payoff={analysis.get('visual_payoff_score', '—')}/10, "
        f"save={analysis.get('saveability_score', '—')}/10"
    )


def format_viral_application_summary(
    brief: dict[str, Any] | None,
    scenes: list[dict[str, Any]] | None,
    retention_review: dict[str, Any] | None = None,
) -> str:
    """Shows how the Viral Retention Layer affected the actual scenario."""
    brief_data = brief if isinstance(brief, dict) else {}
    scene_list = [s for s in (scenes or []) if isinstance(s, dict)]
    review = retention_review if isinstance(retention_review, dict) else {}
    if not scene_list:
        return "VIRAL LAYER: КАК ПРИМЕНЕНО\n— сценарий ещё не собран"

    first_scene = scene_list[0]
    final_scene = scene_list[-1]
    first_phrase_review = (
        first_scene.get("first_phrase_review")
        if isinstance(first_scene.get("first_phrase_review"), dict)
        else review.get("first_scene_review")
        if isinstance(review.get("first_scene_review"), dict)
        else {}
    )
    first_phrase_score = first_scene.get("first_phrase_score") or first_phrase_review.get("score") or "—"
    first_phrase_issues = (
        first_phrase_review.get("issues")
        if isinstance(first_phrase_review.get("issues"), list)
        else []
    )
    first_phrase_status = (
        "нужно усилить: " + "; ".join(_safe_text(x) for x in first_phrase_issues if _safe_text(x))
        if first_phrase_issues
        else "ок, первая фраза поддерживает удержание"
    )
    scene_functions = []
    for scene in scene_list:
        scene_no = scene.get("scene", scene.get("scene_id", "?"))
        fn = _safe_text(scene.get("retention_function")) or "—"
        scene_functions.append(f"{scene_no}:{fn}")
    return (
        "VIRAL LAYER: КАК ПРИМЕНЕНО\n\n"
        "Это не отдельный отчёт: эти решения уже встроены в сценарий и дальше уйдут в промпты кадров/видео.\n\n"
        f"Первая фраза в сценарии: {_safe_text(first_scene.get('text')) or '—'}\n"
        f"Проверка первой фразы: {first_phrase_score}/10, {first_phrase_status}\n"
        f"Первый кадр из brief: {_safe_text(brief_data.get('strongest_first_frame')) or '—'}\n"
        f"Что реально в сцене 1: {_safe_text(first_scene.get('action')) or '—'}\n\n"
        f"Причина досмотреть: {_safe_text(brief_data.get('reason_to_watch_end')) or '—'}\n"
        f"Финальный payoff в сценарии: {_safe_text(final_scene.get('payoff_type')) or _safe_text(brief_data.get('payoff_type')) or '—'}\n"
        f"Что реально в финале: {_safe_text(final_scene.get('action')) or '—'}\n\n"
        "Функции удержания по сценам: "
        + ", ".join(scene_functions)
    )


def format_concepts(concepts: list[dict[str, str]]) -> str:
    """
    Форматирует список концептов блюд для шага выбора пользователем (номером).

    В конце добавляет подсказку «напиши номер концепта».
    """
    rows = [
        (
            f"КОНЦЕПТ {idx}:\n"
            f"название: {c.get('name', 'Без названия')}\n"
            f"почему зайдет: {c.get('why', '-')}\n"
            f"ключевая фишка: {c.get('hook', '-')}"
        )
        for idx, c in enumerate(concepts, start=1)
    ]
    rows.append("\nНапиши номер концепта (например: 2), чтобы продолжить.")
    return "\n\n---\n\n".join(rows)


def format_recipe_steps(steps: list[str]) -> str:
    """
    Выводит нормализованный список шагов рецепта (без креатива, только «ШАГ N: …»).

    Используется при правках сценария как напоминание внутренних этапов (1 шаг = 1 сцена).
    """
    lines = ["РЕЦЕПТ (базовые шаги):"]
    for idx, step in enumerate(steps, start=1):
        lines.append(f"ШАГ {idx}: {step}")
    return "\n".join(lines)


def format_full_recipe_for_approval(recipe_text: str) -> str:
    """
    Один цельный блок рецепта для этапа «утвердить рецепт» (без нумерации шагов для съёмки).
    """
    body = (recipe_text or "").strip() or "—"
    return f"РЕЦЕПТ:\n\n{body}"


def _draft_duration_by_step_text(step: str) -> int:
    words = [w for w in (step or "").split() if w.strip()]
    if len(words) <= 6:
        return 4
    if len(words) <= 11:
        return 6
    return 8


def _draft_phrase_by_step_text(step: str) -> str:
    words = [w.strip(" ,.;:!?") for w in (step or "").split() if w.strip(" ,.;:!?")]
    core = " ".join(words[:8]).strip()
    if not core:
        core = "сейчас готовим вкусное блюдо"
    return f"я {core} [пауза] делАю шаг аккурАтно"


def _draft_dish_name(steps: list[str]) -> str:
    """
    Грубая эвристика: короткое название блюда для хука.
    """
    if not steps:
        return "вкусное блюдо"
    first = (steps[0] or "").lower()
    # Частые заготовки для творожных/мясных/суповых блюд.
    if "сырник" in first or "творог" in first:
        return "сырники"
    if "суп" in first:
        return "суп"
    if "котлет" in first:
        return "котлеты"
    if "паста" in first:
        return "паста"
    # fallback: первые 2-3 слова без цифр.
    words = [w.strip(" ,.;:!?") for w in steps[0].split() if w.strip(" ,.;:!?")]
    words = [w for w in words if not any(ch.isdigit() for ch in w)]
    short = " ".join(words[:3]).strip()
    return short or "это блюдо"


def format_recipe_step_scene_draft(steps: list[str]) -> str:
    """
    Черновой превью-блок будущих сцен на этапе апрува рецепта.

    Для каждого шага показывает:
    - шаг рецепта;
    - draft-фразу для реплики;
    - модель;
    - длительность.
    """
    blocks: list[str] = ["ПРЕДВАРИТЕЛЬНЫЙ ЧЕРНОВИК СЦЕН (до генерации сценария):"]
    for idx, step in enumerate(steps, start=1):
        duration = _draft_duration_by_step_text(step)
        blocks.append(
            f"\nСЦЕНА {idx}\n"
            f"Шаг: {step}\n"
            "Модель: veo3_fast\n"
            f"Длительность: {duration} сек"
        )
    return "\n".join(blocks)


def format_frame_prompts_for_approval(frame_prompts: list[dict[str, Any]]) -> str:
    """
    Форматирует блок START/END frame промптов для отдельного апрува пользователем.

    Args:
        frame_prompts: Массив сцен с полями кадра (`scene`, `shot_type`, `start/end_*`).
    Returns:
        Многострочный текст с разделителем `---` между сценами.

    Номер в заголовке «СЦЕНА N» — порядковый по списку (1…len), а не поле ``scene`` в объекте.
    """
    blocks: list[str] = []
    for i, item in enumerate(frame_prompts, start=1):
        shot = item.get("shot_type", "medium")
        anchors = item.get("continuity_anchors") if isinstance(item.get("continuity_anchors"), list) else []
        anchors_text = ", ".join(_safe_text(x) for x in anchors if _safe_text(x)) or "—"
        model = item.get("model", "veo3_fast")
        duration = item.get("duration_sec", 4)
        start_ru = item.get("start_frame_prompt_ru", "пусто")
        end_ru = item.get("end_frame_prompt_ru", "пусто")
        blocks.append(
            f"СЦЕНА {i}\n"
            f"Ракурс: {shot}\n"
            f"Опоры консистентности: {anchors_text}\n"
            f"Модель: {model}, длительность: {duration} сек\n\n"
            "START FRAME (RU):\n"
            f"{start_ru}\n\n"
            "END FRAME (RU):\n"
            f"{end_ru}"
        )
    return "FRAME PROMPTS (апрув перед production):\n\n" + "\n\n---\n\n".join(blocks)


def format_production_bundle_for_approval(scenes: list[dict[str, Any]]) -> str:
    """
    Единый блок всех промптов (start / end / video) по сценам перед пошаговой генерацией медиа.
    """
    blocks: list[str] = []
    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        scene_no = scene.get("scene", i + 1)
        start = _safe_text(scene.get("start_frame_prompt")) or "пусто"
        end = _safe_text(scene.get("end_frame_prompt")) or "пусто"
        prompt_map = scene.get("video_prompt_by_lang") if isinstance(scene.get("video_prompt_by_lang"), dict) else {}
        video = _safe_text(scene.get("video_prompt")) or "—"
        if prompt_map:
            video = "\n".join(
                f"[{lang}] {_safe_text(prompt)}"
                for lang, prompt in prompt_map.items()
            )
        speech_map = scene.get("speech_by_lang") if isinstance(scene.get("speech_by_lang"), dict) else {}
        phrase = _safe_text(scene.get("text")) or "—"
        if speech_map:
            phrase = "\n".join(f"[{lang}] {_safe_text(text)}" for lang, text in speech_map.items())
        action = _safe_text(scene.get("action")) or "—"
        retention_function = _safe_text(scene.get("retention_function")) or "—"
        micro_events_raw = scene.get("micro_events") if isinstance(scene.get("micro_events"), list) else []
        micro_events = ", ".join(_safe_text(x) for x in micro_events_raw if _safe_text(x)) or "—"
        payoff = _safe_text(scene.get("payoff_type")) or "—"
        blocks.append(
            f"СЦЕНА {scene_no}\n"
            f"Что происходит: {action}\n"
            f"Фраза: {phrase}\n"
            f"Retention function: {retention_function}\n"
            f"Микрособытия: {micro_events}\n"
            f"Payoff: {payoff}\n\n"
            "START FRAME (RU):\n"
            f"{start}\n\n"
            "END FRAME (RU):\n"
            f"{end}\n\n"
            "VIDEO PROMPT (EN):\n"
            f"{video}"
        )
    body = "\n\n---\n\n".join(blocks) if blocks else "—"
    return (
        "ПРОМПТЫ ПО ВСЕМ СЦЕНАМ (ознакомление)\n\n"
        f"{body}\n\n"
        "Дальше: после «ОК» — по очереди каждая сцена: утверждение стартового промпта → генерация старта → "
        "при необходимости конечный кадр → video prompt → генерация видео (со стартовым и конечным кадром, если он есть)."
    )


def format_production_cards(scenes: list[dict[str, Any]]) -> str:
    """
    Человекочитаемые карточки финального production-пакета по сценам.

    Args:
        scenes: Финальные production-сцены с кадрами, video_prompt и TTS полями.
    Returns:
        Большой текстовый блок карточек для ручной проверки пользователем.
    """
    cards: list[str] = []
    for scene in scenes:
        scene_no = scene.get("scene", "?")
        model = scene.get("model", "veo3_fast")
        duration = scene.get("duration_sec", 4)
        anchors = scene.get("continuity_anchors") if isinstance(scene.get("continuity_anchors"), list) else []
        anchors_text = ", ".join(_safe_text(x) for x in anchors if _safe_text(x)) or "—"
        start_frame = _safe_text(scene.get("start_frame_prompt")) or "пусто"
        end_frame = _safe_text(scene.get("end_frame_prompt")) or "пусто"
        video_prompt = _safe_text(scene.get("video_prompt")) or "—"
        tts = scene.get("tts") if isinstance(scene.get("tts"), dict) else {}
        tts_provider = _safe_text(tts.get("provider")) or "native"
        voice_profile = _safe_text(tts.get("voice_profile")) or "native_scene_voice"
        voice_mode = _safe_text(tts.get("voice_mode")) or "native"
        voice_id = _safe_text(tts.get("voice_id")) or "—"
        phrase = _safe_text(tts.get("text_ru")) or _safe_text(scene.get("text")) or "—"
        speaker_profiles = tts.get("speaker_profiles") if isinstance(tts.get("speaker_profiles"), dict) else {}
        speakers_text = "—"
        if speaker_profiles:
            lines: list[str] = []
            for speaker, params in speaker_profiles.items():
                if isinstance(params, dict):
                    lines.append(f"{speaker}: profile={_safe_text(params.get('voice_profile'))}")
            if lines:
                speakers_text = "; ".join(lines)
        cards.append(
            f"СЦЕНА {scene_no}\n"
            f"Опоры консистентности: {anchors_text}\n"
            f"Модель: {model}, длительность: {duration} сек\n\n"
            "START FRAME (RU):\n"
            f"{start_frame}\n\n"
            "END FRAME (RU):\n"
            f"{end_frame}\n\n"
            "VIDEO PROMPT (EN):\n"
            f"{video_prompt}\n\n"
            f"TTS провайдер: {tts_provider}\n"
            f"TTS режим: {voice_mode}\n"
            f"Voice profile: {voice_profile}\n"
            f"ElevenLabs voice_id: {voice_id}\n\n"
            f"Голоса персонажей: {speakers_text}\n\n"
            "Фраза:\n"
            f"{phrase}"
        )
    return "PRODUCTION CARDS:\n\n" + "\n\n---\n\n".join(cards)
