"""
Утилиты для сцен: выбор модели генерации, нормализация полей, валидация качества.

Сцены приходят из LLM как JSON; здесь мы:
- дополняем обязательных персонажей и пути к ассетам;
    - приводим длительность и сценовые поля к допустимым значениям;
- проверяем соответствие сцен шагам рецепта и эвристики «еда / кот / конфликт».
"""

import re
from typing import Any

from .assets import ASSETS


def _as_text(value: Any) -> str:
    """
    Безопасно приводит произвольное значение к строке для дальнейшего ``strip()``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value)


def pick_model(scene: dict[str, Any]) -> str:
    """
    Выбирает идентификатор «нейросети» для сцены по правилам продукта.

    Текущий прод-режим: всегда ``veo3_fast`` (Vertex AI).
    Другие модели для сцен отключены.
    """
    _ = scene
    return "veo3_fast"


def _normalized_duration_for_model(model: str, raw_duration: Any) -> int:
    """
    Нормализует длительность сцены по правилам платформ:
    - ``veo3*``: только 6/8 сек (берём ближайшее значение);
    - остальные модели: от 2 сек и выше.
    """
    try:
        sec = int(raw_duration)
    except Exception:
        sec = 6 if model.startswith("veo3") else 2
    if model.startswith("veo3"):
        allowed = (6, 8)
        return min(allowed, key=lambda x: abs(x - sec))
    return max(2, sec)


def voice_provider_for_model(model: str) -> str:
    """
    Источник озвучки по модели сцены.
    - heygen / veo3_fast: нативный lip sync/voice;
    - всё остальное: elevenlabs (закадровая озвучка).
    """
    m = (model or "").lower()
    if m in {"heygen", "veo3_fast"}:
        return "native"
    return "elevenlabs"


def _location_asset_refs(location_key: str, shot_type: str) -> dict[str, str]:
    """
    Возвращает ссылку на базовый референс локации и top-view, если он релевантен ракурсу.
    """
    loc_value = (ASSETS.get("locations") or {}).get(location_key)
    refs: dict[str, str] = {}
    base: str | None = None
    top_view: str | None = None
    if isinstance(loc_value, str):
        base = loc_value
    elif isinstance(loc_value, dict):
        raw_base = loc_value.get("base")
        raw_top = loc_value.get("top_view")
        if isinstance(raw_base, str) and raw_base.strip():
            base = raw_base
        if isinstance(raw_top, str) and raw_top.strip():
            top_view = raw_top
    if base:
        refs["location"] = base
    shot = (shot_type or "").lower().replace("-", " ").strip()
    is_top_view = shot in {"top", "top view", "overhead", "bird view", "bird eye", "birds eye"}
    if is_top_view and top_view:
        refs["location_top_view"] = top_view
    return refs


def _normalized_stove_burner(value: Any, scene_index: int) -> str:
    """
    Нормализует указание конфорки до одной из 4 стандартных позиций.
    """
    v = _as_text(value).lower().strip()
    aliases = {
        "передняя левая": "front_left",
        "front left": "front_left",
        "левая передняя": "front_left",
        "задняя левая": "back_left",
        "back left": "back_left",
        "левая задняя": "back_left",
        "передняя правая": "front_right",
        "front right": "front_right",
        "правая передняя": "front_right",
        "задняя правая": "back_right",
        "back right": "back_right",
        "правая задняя": "back_right",
    }
    if v in aliases:
        return aliases[v]
    if v in {"front_left", "back_left", "front_right", "back_right"}:
        return v
    # Детерминированный fallback по номеру сцены: всегда одна из 4 конфорок.
    order = ("front_left", "front_right", "back_left", "back_right")
    return order[(max(scene_index, 1) - 1) % len(order)]


def _stove_burner_ru(code: str) -> str:
    mapping = {
        "front_left": "переднюю левую",
        "front_right": "переднюю правую",
        "back_left": "заднюю левую",
        "back_right": "заднюю правую",
    }
    return mapping.get(code, "переднюю левую")


def apply_veo3_speech_format(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Возвращает сцены без локальной нормализации реплик.

    Локальная пост-обработка речи (ударения/паузы/обрезка) отключена:
    текст должен оставаться в том виде, в котором его вернула модель.
    """
    return [dict(s) for s in scenes]


def apply_veo3_lowercase_format(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Локальный пост-фикс по запросу пользователя:
    привести veo3/veo3_fast реплики к нижнему регистру, сохранив [пауза].
    """
    out: list[dict[str, Any]] = []
    for s in scenes:
        item = dict(s)
        model = str(item.get("model", "")).lower()
        if model.startswith("veo3"):
            txt = _as_text(item.get("text"))
            txt = re.sub(r"\[\s*пауза\s*\]", " [пауза] ", txt, flags=re.IGNORECASE)
            txt = re.sub(r"\bпауза\b", " [пауза] ", txt, flags=re.IGNORECASE)
            txt = txt.lower()
            txt = re.sub(r"[.,!?;:]", " ", txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            item["text"] = txt or "сейчас готовим вкусное блюдо"
        out.append(item)
    return out


def _default_continuity_anchors(scene: dict[str, Any]) -> list[str]:
    """Собирает устойчивые предметные опоры кадра без привязки к location assets."""
    raw = scene.get("continuity_anchors")
    anchors: list[str] = []
    if isinstance(raw, list):
        anchors = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        anchors = [x.strip() for x in re.split(r"[,;]\s*", raw) if x.strip()]
    blob = " ".join(
        _as_text(scene.get(k))
        for k in ("action", "visual_description", "recipe_step", "food_item", "dish_description")
    ).lower()
    candidates = (
        ("кастрюл" in blob or "суп" in blob, "та же кастрюля с видимым состоянием блюда"),
        ("сковород" in blob or "жар" in blob, "та же сковорода и поверхность плиты"),
        ("мис" in blob or "смеш" in blob or "тесто" in blob, "та же глубокая миска для смешивания"),
        ("нарез" in blob or "реж" in blob or "руб" in blob, "та же деревянная разделочная доска"),
        ("тарел" in blob or "подач" in blob or "готов" in blob, "та же сервировочная тарелка"),
        ("лож" in blob, "та же ложка"),
        ("венчик" in blob, "тот же венчик"),
        ("кот" in blob, "кот рядом с рабочей поверхностью"),
    )
    for enabled, text in candidates:
        if enabled and text not in anchors:
            anchors.append(text)
    for fallback in (
        "одна и та же деревянная рабочая поверхность",
        "одинаковая палитра посуды и ингредиентов",
        "бабушка и кот в том же мультяшном дизайне",
    ):
        if len(anchors) >= 3:
            break
        if fallback not in anchors:
            anchors.append(fallback)
    return anchors[:6]


def _default_continuity_state(scene: dict[str, Any]) -> str:
    state = _as_text(scene.get("continuity_state")).strip()
    if state:
        return state
    parts = [
        _as_text(scene.get("food_item")).strip(),
        _as_text(scene.get("action")).strip(),
        _as_text(scene.get("dish_description")).strip(),
    ]
    text = ". ".join(p for p in parts if p)
    return text or "Состояние еды, посуды и рабочих предметов соответствует текущему шагу рецепта."


def _sanitize_scene(scene: dict[str, Any], idx: int, allowed_locations: set[str] | None = None) -> dict[str, Any]:
    """
    Приводит одну сцену к инвариантам пайплайна: номер, длительность, персонажи, ``references``.

    - Принудительно добавляет ``grandma`` и ``cat`` в ``characters``.
    - Заполняет character ``references`` путями из ``ASSETS``.
    - Добавляет предметные ``continuity_anchors`` вместо location-контракта.
    - Гарантирует упоминание кота в ``action`` (минимальная защита от «пустых» сцен).
    """
    out = dict(scene)
    out["scene"] = idx
    out["goal"] = out.get("goal") or "шаг"
    chars = out.get("characters")
    if not isinstance(chars, list):
        chars = []
    for required in ("grandma", "cat"):
        if required not in chars:
            chars.append(required)
    out["characters"] = chars
    action = _as_text(out.get("action")).strip()
    if not action:
        action = "Бабушка выполняет следующий кулинарный шаг, кот активно взаимодействует с едой."
    if "кот" not in action.lower():
        action += " Кот активно взаимодействует с едой."
    cooking_on_stove = any(x in action.lower() for x in ("плит", "конфорк", "сковород", "жарит", "обжар"))
    if cooking_on_stove:
        burner = _normalized_stove_burner(out.get("stove_burner"), idx)
        out["stove_burner"] = burner
        if burner not in action.lower() and "конфорк" not in action.lower():
            action += f" Использует {_stove_burner_ru(burner)} конфорку из четырёх."
    out["action"] = action
    out.setdefault("cat_emotion", f"эмоция_{idx}")
    existing_refs_raw = out.get("references")
    existing_refs: dict[str, str] = {}
    if isinstance(existing_refs_raw, dict):
        for k, v in existing_refs_raw.items():
            if isinstance(v, str) and v.strip():
                existing_refs[str(k).strip()] = v.strip()

    refs_merged = dict(existing_refs)
    refs_merged.pop("location", None)
    refs_merged.pop("location_top_view", None)
    for ch in out["characters"]:
        ch_key = str(ch).lower()
        ch_data = (ASSETS.get("characters") or {}).get(ch_key)
        if isinstance(ch_data, dict) and isinstance(ch_data.get("base"), str):
            refs_merged.setdefault(ch_key, ch_data["base"])
    out["references"] = refs_merged
    out["continuity_anchors"] = _default_continuity_anchors(out)
    out["continuity_state"] = _default_continuity_state(out)
    model = pick_model(out)
    out["model"] = model
    out["duration_sec"] = _normalized_duration_for_model(model, out.get("duration_sec"))
    out["voice_provider"] = voice_provider_for_model(model)
    return out


def normalize_scenes_for_pipeline(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Нормализует список сцен после ответа LLM.

    Действия:
    - обрезает до 6 сцен, при нехватке дублирует последнюю (временный костыль для модели);
    - требует минимум 4 сцены (дополняет копиями);
    - первая сцена помечается ``goal=хук``, последняя — ``результат``;
    - **во всех сценах** — короткая реплика ``text``, ``voice_needed``, ``has_lipsync``.

    Важно: длина списка потом может быть урезана до ``len(recipe_steps)`` в ``pipeline.generate_scenes_plan``.
    """
    scenes = scenes[:6]
    if len(scenes) < 4:
        while len(scenes) < 4:
            scenes.append(dict(scenes[-1] if scenes else {}))
    normalized = [_sanitize_scene(scene, idx) for idx, scene in enumerate(scenes, start=1)]
    normalized[0]["goal"] = "хук"
    normalized[-1]["goal"] = "результат"
    # Финальная сцена всегда про подачу готового блюда.
    final_action = _as_text(normalized[-1].get("action")).strip()
    if "подач" not in final_action.lower() and "готов" not in final_action.lower():
        normalized[-1]["action"] = (
            f"{final_action} Бабушка выполняет подачу готового блюда на тарелке с видимыми ингредиентами."
        ).strip()
    dish_desc = _as_text(normalized[-1].get("dish_description")).strip()
    if not dish_desc:
        normalized[-1]["dish_description"] = (
            "Готовое блюдо аккуратно подано на тарелке, хорошо видны ключевые ингредиенты и текстура."
        )
    speech_fallbacks = (
        "Смотри внимательно, сейчас будет вкусно.",
        "Ну куда ты лезешь!",
        "Береги плиту!",
    )
    fi = 0
    for s in normalized:
        if not _as_text(s.get("text")).strip():
            s["text"] = speech_fallbacks[fi % len(speech_fallbacks)]
            fi += 1
    for s in normalized:
        s["voice_needed"] = True
        s["has_lipsync"] = True
        s["model"] = pick_model(s)
        s["voice_provider"] = voice_provider_for_model(str(s.get("model", "")))
    return normalized


def has_banned_scene_phrases(scenes: list[dict[str, Any]]) -> bool:
    """
    Проверяет наличие запрещённых шаблонных формулировок в текстах сцен (для триггера перегенерации).

    Ищет подстроки в полях ``action``, ``visual_description``, ``text``, ``goal``.
    """
    banned = ("простое действие", "что-то делает")
    for scene in scenes:
        blob = " ".join(str(scene.get(k, "")) for k in ("action", "visual_description", "text", "goal")).lower()
        if any(phrase in blob for phrase in banned):
            return True
    return False


def extract_recipe_steps_text(text: str) -> list[str]:
    """
    Парсит многострочный ответ модели с шагами рецепта в список строк без префиксов «ШАГ N».

    Поддерживает строки вида ``ШАГ 1: ...``, ``1. ...``, ``1) ...``.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    steps: list[str] = []
    for line in lines:
        m = re.match(r"^(?:шаг\s*\d+[:.-]?\s*|\d+[).:-]\s*)(.+)$", line, flags=re.IGNORECASE)
        if m:
            steps.append(m.group(1).strip())
        elif line.lower().startswith("шаг "):
            parts = line.split(":", 1)
            steps.append(parts[1].strip() if len(parts) == 2 else line)
    return steps


def scene_matches_step(scene: dict[str, Any], recipe_step: str) -> bool:
    """
    Грубая проверка: отражает ли сцена смысл шага рецепта (пересечение значимых слов ≥4 букв).

    Учитываются ``action``, ``text``, ``visual_description``, ``food_item``, ``dish_description`` —
    чтобы шаблонный ``action`` плюс «битая» реплика Veo3 не давали ложный ``step_not_reflected``,
    если смысл шага виден в других полях.
    """
    stopwords = {"и", "в", "на", "с", "из", "по", "для", "под", "над", "до", "после", "бабушка", "кот", "чтобы", "этот", "эту", "этой", "такой"}
    step_tokens = {t for t in re.findall(r"[a-zA-Zа-яА-ЯёЁ]{4,}", recipe_step.lower()) if t not in stopwords}
    if not step_tokens:
        return True
    parts = [
        _as_text(scene.get("action")),
        _as_text(scene.get("text")),
        _as_text(scene.get("visual_description")),
        _as_text(scene.get("food_item")),
        _as_text(scene.get("dish_description")),
    ]
    blob = " ".join(parts).lower()
    return any(tok in blob for tok in step_tokens)


def _text_implies_food_context(
    text: str,
    food_keywords: tuple[str, ...],
    ingredient_markers: tuple[str, ...],
) -> bool:
    """Есть ли в тексте явные маркеры кулинарного шага / ингредиентов / веса."""
    low = (text or "").lower()
    if not low.strip():
        return False
    if any(k in low for k in food_keywords):
        return True
    if any(k in low for k in ingredient_markers):
        return True
    return bool(re.search(r"\d+\s*г(?:рамм)?", low))


def scenes_follow_recipe_steps(scenes: list[dict[str, Any]], recipe_steps: list[str]) -> list[str]:
    """
    Проверяет соответствие «одна сцена = один шаг рецепта».

    Args:
        scenes: Нормализованные сцены пайплайна в порядке показа.
        recipe_steps: Канонический список шагов рецепта в том же порядке.

    Returns:
        Список кодов ошибок:
        - `recipe_steps_count_mismatch:<expected>!=<actual>`, если длины не совпадают;
        - `scene_<n>: step_not_reflected`, если сцена `n` не отражает свой шаг.

    Логика:
        1) Сначала сравнивает количество элементов.
        2) Затем построчно проверяет пары (scene[i], recipe_steps[i]) через `scene_matches_step`.
    """
    errors: list[str] = []
    if len(scenes) != len(recipe_steps):
        errors.append(f"recipe_steps_count_mismatch:{len(recipe_steps)}!={len(scenes)}")
        return errors
    for idx, (scene, step) in enumerate(zip(scenes, recipe_steps), start=1):
        if not scene_matches_step(scene, step):
            errors.append(f"scene_{idx}: step_not_reflected")
    return errors


def scenes_quality_errors(
    scenes: list[dict[str, Any]], recipe_steps: list[str] | None = None
) -> list[str]:
    """
    Эвристически валидирует качество каждой сцены и сценовые контракты.

    Args:
        scenes: Список сцен после нормализации (ожидаются поля `text`, `action`, `model`,
            `duration_sec`, `characters`, `continuity_anchors`, `goal` и пр.).
        recipe_steps: Канонические шаги рецепта. Используются для проверки первой сцены
            (название/суть блюда), если переданы.

    Returns:
        Список строк-кодов ошибок формата `scene_<n>: <code>`.
        Пустой список означает, что обязательные правила пройдены.

    Логика (в порядке выполнения):
        1) Для каждой сцены собирается общий `blob` из текстовых полей.
        2) Проверяется базовый контракт (речь, действие, наличие кота, отсутствие абстракций).
        3) Проверяются ограничения модели (`veo3`/non-`veo3`) и длительности.
        4) Проверяются предметные правила (конфорка/плита по действию, стиль нарезки).
        5) Для сцен подачи проверяется информативность `dish_description`.
        6) Дополнительно контролируется глобальный контракт:
           - первая сцена содержит хук и упоминание, что готовим;
           - последняя сцена является презентацией блюда.
    """
    errors: list[str] = []
    cut_markers = ("нарез", "шинков", "ломтик", "кубик", "соломк", "дольк", "кольц")
    cut_style_markers = ("кубик", "соломк", "ломтик", "дольк", "кольц", "мелко", "крупно", "тонко", "наискосок")
    dish_markers = ("готовое блюдо", "подач", "сервиров", "на тарелк", "финал")
    ingredient_markers = (
        "картоф",
        "лук",
        "морков",
        "мяс",
        "рыб",
        "куриц",
        "сыр",
        "сливк",
        "томат",
        "гриб",
        "яйц",
        "мук",
        "рис",
        "макарон",
        "перец",
        "соль",
        "зелень",
        "соус",
    )
    hook_markers = (
        "смотрите",
        "сейчас",
        "сегодня",
        "за",
        "секунд",
        "вкусн",
        "аппетит",
        "хитр",
        "фишк",
        "понадоб",
    )
    presentation_markers = ("презентац", "подач", "подаем", "крупный план", "готовое блюдо", "результат")
    banned_low_angle_markers = ("снизу", "нижний ракурс", "low angle", "worm eye", "worm's eye")

    def _step_tokens(step: str) -> list[str]:
        """Выделяет 4+ символьные значимые токены шага для первой сцены."""
        # Нужны «опорные» слова блюда/шага; служебные слова отфильтровываем.
        words = re.findall(r"[а-яёa-z]{4,}", (step or "").lower())
        stop = {
            "бабушка",
            "кот",
            "шаг",
            "рецепта",
            "текущий",
            "сцена",
            "потом",
            "после",
            "чтобы",
            "нужно",
            "добавить",
            "смешать",
            "готовить",
        }
        uniq: list[str] = []
        for w in words:
            if w in stop:
                continue
            if w not in uniq:
                uniq.append(w)
        return uniq[:6]
    def _scene_blob(scene: dict[str, Any]) -> str:
        """
        Собирает текстовый «снимок» сцены.

        Args:
            scene: Одна сцена.
        Returns:
            Нижнерегистровая строка из ключевых текстовых полей, используемая для `in`-проверок.
        """
        return " ".join(
            _as_text(scene.get(k))
            for k in (
                "action",
                "visual_description",
                "text",
                "goal",
                "recipe_step",
                "food_item",
                "dish_description",
                "grandma_reaction",
                "cat_action",
            )
        ).lower()

    def _canonical_recipe_step(scene: dict[str, Any], idx: int) -> str:
        """
        Возвращает канонический шаг для сцены по индексу.

        Args:
            scene: Текущая сцена.
            idx: Индекс сцены (0-based).
        Returns:
            `recipe_steps[idx]`, если список шагов передан и индекс валиден,
            иначе `scene["recipe_step"]`.
        """
        if recipe_steps is not None and 0 <= idx < len(recipe_steps):
            return recipe_steps[idx]
        return _as_text(scene.get("recipe_step"))

    def _validate_basic_fields(scene: dict[str, Any], i: int, blob: str) -> None:
        """
        Проверяет базовые обязательные поля и добавляет ошибки в `errors`.

        Args:
            scene: Текущая сцена.
            i: Номер сцены (1-based) для кода ошибки.
            blob: Склеенный текст сцены для эвристик по абстракциям.
        """
        if not _as_text(scene.get("text")).strip():
            errors.append(f"scene_{i}: no_speech")
        if not _as_text(scene.get("action")).strip():
            errors.append(f"scene_{i}: no_step_explanation")
        chars = [str(c).lower() for c in (scene.get("characters") or [])]
        if "cat" not in chars:
            errors.append(f"scene_{i}: no_cat")
        if "простое действие" in blob or "что-то делает" in blob:
            errors.append(f"scene_{i}: banned_abstraction")

    def _validate_model_requirements(scene: dict[str, Any], i: int) -> None:
        """
        Проверяет модельные ограничения длительности и формата речи.

        Args:
            scene: Текущая сцена.
            i: Номер сцены (1-based).
        Returns:
            None. Ошибки добавляются во внешний список `errors`.
        """
        model = str(scene.get("model", "")).lower()
        duration = scene.get("duration_sec")
        if model.startswith("veo3"):
            if duration not in {6, 8}:
                errors.append(f"scene_{i}: invalid_duration_for_veo3")
            txt = _as_text(scene.get("text")).strip()
            if re.search(r"[.,!?;:]", txt):
                errors.append(f"scene_{i}: veo3_text_has_punctuation")
            if not re.search(r"[АЕЁИОУЫЭЮЯ]", txt):
                errors.append(f"scene_{i}: veo3_text_no_stress_marks")
            if len(txt.split()) >= 6 and "[пауза]" not in txt:
                errors.append(f"scene_{i}: veo3_text_no_pause_token")
            return
        if not isinstance(duration, int) or duration < 2:
            errors.append(f"scene_{i}: invalid_duration_for_non_veo")

    def _validate_cooking_surface_constraints(scene: dict[str, Any], i: int, blob: str) -> None:
        """Если сцена явно на плите/сковороде, проверяет указание конфорки."""
        if not any(x in blob for x in ("плит", "конфорк", "сковород", "жарит", "обжар")):
            return
        burner = str(scene.get("stove_burner", "")).lower()
        if burner not in {"front_left", "front_right", "back_left", "back_right"}:
            errors.append(f"scene_{i}: stove_burner_missing")

    def _validate_cutting(scene: dict[str, Any], i: int, blob: str) -> None:
        """Если есть маркеры нарезки, требует явно указать форму/стиль."""
        mentions_cutting = any(k in blob for k in cut_markers)
        has_cut_style = any(k in blob for k in cut_style_markers)
        if mentions_cutting and not has_cut_style:
            errors.append(f"scene_{i}: cut_style_missing")

    def _validate_dish_description(scene: dict[str, Any], i: int, blob: str) -> None:
        """Для сцен результата проверяет развернутость описания блюда и наличие ингредиентов."""
        looks_like_dish_scene = str(scene.get("goal", "")).lower() == "результат" or any(
            k in blob for k in dish_markers
        )
        if not looks_like_dish_scene:
            return
        dish_desc = " ".join(
            _as_text(scene.get(k))
            for k in ("dish_description", "visual_description", "action", "text")
        ).strip()
        ingredients_hits = sum(1 for k in ingredient_markers if k in dish_desc.lower())
        if len(dish_desc) < 45 or ingredients_hits < 2:
            errors.append(f"scene_{i}: dish_description_with_ingredients_missing")

    def _validate_first_scene_contract(scene: dict[str, Any], i: int, blob: str, canonical_step: str) -> None:
        """
        Контракт первой сцены: нужен хук и явное обозначение блюда/шага.

        Args:
            scene: Текущая сцена.
            i: Номер сцены (1-based).
            blob: Склеенный текст сцены.
            canonical_step: Канонический шаг рецепта для этой сцены.
        """
        if i != 1:
            return
        has_hook = any(k in blob for k in hook_markers)
        if not has_hook:
            errors.append("scene_1: first_scene_hook_missing")
        step_tokens = _step_tokens(canonical_step)
        if step_tokens and not any(t in blob for t in step_tokens):
            errors.append("scene_1: first_scene_dish_name_missing")

    def _validate_last_scene_contract(scene: dict[str, Any], i: int, blob: str, total: int) -> None:
        """
        Контракт последней сцены: финал должен быть презентацией/подачей.

        Args:
            scene: Текущая сцена.
            i: Номер сцены (1-based).
            blob: Склеенный текст сцены.
            total: Общее количество сцен.
        """
        if i != total:
            return
        has_presentation = (
            str(scene.get("goal", "")).lower() == "результат" or any(k in blob for k in presentation_markers)
        )
        if not has_presentation:
            errors.append(f"scene_{i}: final_presentation_missing")

    def _validate_shot_and_scene_transition(scene: dict[str, Any], i: int, blob: str) -> None:
        """Проверяет запрет ракурса снизу и смену ракурса/предметного фокуса между соседними сценами."""
        shot = _as_text(scene.get("shot_type")).lower().replace("-", " ").strip()
        if shot in {"low", "low angle", "from below", "bottom"} or any(x in blob for x in banned_low_angle_markers):
            errors.append(f"scene_{i}: low_angle_forbidden")
        if i <= 1:
            return
        prev = scenes[i - 2]
        prev_shot = _as_text(prev.get("shot_type")).lower().replace("-", " ").strip()
        curr_shot = shot
        prev_focus = " ".join(_as_text(x).lower() for x in (prev.get("continuity_anchors") or []))
        curr_focus = " ".join(_as_text(x).lower() for x in (scene.get("continuity_anchors") or []))
        if prev_shot == curr_shot and prev_focus == curr_focus:
            errors.append(f"scene_{i}: no_location_or_shot_change")

    total_scenes = len(scenes)
    for i, scene in enumerate(scenes, start=1):
        blob = _scene_blob(scene)
        canonical_step = _canonical_recipe_step(scene, i - 1)
        _validate_basic_fields(scene, i, blob)
        _validate_model_requirements(scene, i)
        _validate_cooking_surface_constraints(scene, i, blob)
        _validate_cutting(scene, i, blob)
        _validate_dish_description(scene, i, blob)
        _validate_first_scene_contract(scene, i, blob, canonical_step)
        _validate_last_scene_contract(scene, i, blob, total_scenes)
        _validate_shot_and_scene_transition(scene, i, blob)
    return errors


def normalize_recipe_steps(steps: list[str]) -> list[str]:
    """
    Чистит шаги рецепта: схлопывает пробелы, убирает пустые строки.

    Не проверяет диапазон 4–6 — это делает ``pipeline.generate_recipe_steps`` после expand/merge.
    """
    cleaned: list[str] = []
    for step in steps:
        s = re.sub(r"\s+", " ", step).strip(" .-")
        if s:
            cleaned.append(s)
    return cleaned
