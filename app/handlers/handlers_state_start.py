from .handlers_state_deps import (
    Any,
    ContextTypes,
    SESSIONS,
    STATE_REFINING_CONCEPT,
    STATE_REFINING_RECIPE,
    STATE_WAITING_CONCEPT,
    STATE_WAITING_RECIPE_APPROVAL,
    STATE_WAITING_SCENE_APPROVAL,
    Update,
    _bump_debounce_generation,
    _call_with_503_retries,
    _elevenlabs_voice_warning,
    _extract_recipe_from_command_text,
    _extract_first_http_url,
    _fetch_recipe_text_from_url,
    _format_scenes_plan_attempt_notice,
    _format_scenes_regeneration_report,
    _try_parse_direct_recipe_from_user_text,
    _user_error_message,
    _build_concepts_session,
    _build_direct_recipe_session,
    ok_only_markup,
    format_full_recipe_for_approval,
    format_viral_application_summary,
    format_viral_concept_brief,
    generate_recipe_from_source_text,
    generate_recipe_concepts,
    generate_recipe_full_text,
    generate_scenes_plan,
    generate_viral_concept_brief,
    logger,
    re,
    ServiceUnavailableRetriesExhaustedError,
)
from .handlers_state_commands import _reply_text_chunked
from .handlers_state_commands import NAV_BUTTON_START_OVER
from .handlers_keyboards import reply_text_without_keyboard, safe_reply_text
from .handlers_state_media import send_social_descriptions_for_recipe
from ..series_store import (
    SERIES_PENDING_PREFER_VERTEX_SESSION_KEY,
    SERIES_PENDING_RECIPE_SESSION_KEY,
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    SERIES_TOTAL_VIDEOS_SESSION_KEY,
    SERIES_VIDEO_INDEX_SESSION_KEY,
    attach_series_to_session,
    create_series,
    format_series_label,
    list_active_series,
    select_series_payload,
)

_PROJECT_KEY_SESSION = "selected_project_key"
STATE_WAITING_SERIES_CHOICE = "waiting_series_choice"
STATE_WAITING_SERIES_CREATE = "waiting_series_create"
_SERIES_CHOICE_IDS_SESSION_KEY = "series_choice_ids"
SERIES_CHOICE_CONFIRMED_SESSION_KEY = "series_choice_confirmed"
SERIES_PENDING_NEXT_SESSION_KEY = "series_pending_next"
SERIES_NEXT_GENERATION = "generation"
SERIES_NEXT_OVERLAY_TITLE = "overlay_title"
_SERIES_BUTTON_NEW = "Создать серию"
_SERIES_BUTTON_SKIP = "Без серии"
_SERIES_BUTTON_BACK = "Назад"
_SERIES_SESSION_KEYS = (
    SERIES_SESSION_KEY,
    SERIES_TITLE_BY_LANG_SESSION_KEY,
    SERIES_VIDEO_INDEX_SESSION_KEY,
    SERIES_TOTAL_VIDEOS_SESSION_KEY,
    SERIES_COUNTER_BY_LANG_SESSION_KEY,
    SERIES_CHOICE_CONFIRMED_SESSION_KEY,
)


async def _start_description_with_recipe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    recipe: str,
) -> None:
    """Generate social descriptions directly from recipe text or URL."""
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    source = (recipe or "").strip()
    if not source:
        await update.message.reply_text("Пришлите рецепт текстом или ссылкой на страницу с рецептом.")
        return
    source_url = _extract_first_http_url(source)
    if source_url:
        try:
            source_text = _fetch_recipe_text_from_url(source_url)
        except Exception as e:
            await update.message.reply_text(
                f"Не удалось извлечь рецепт по ссылке: {_user_error_message(e)}\n"
                "Пришлите, пожалуйста, текст рецепта сообщением."
            )
            return
        await update.message.reply_text(
            "Нашла ссылку и извлекла текст страницы. Передаю весь текст нейросети, чтобы она выделила рецепт."
        )
        await reply_text_without_keyboard(update.message, "Извлекаю рецепт, ждите.")
        try:
            source = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "extract_recipe_from_source_text_for_description",
                lambda: generate_recipe_from_source_text(source_text, source_url=source_url),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return
        except Exception as e:
            logger.exception("recipe extraction from source text for description failed")
            await update.message.reply_text(f"Ошибка извлечения рецепта из ссылки: {_user_error_message(e)}")
            return
        await update.message.reply_text("Ниже рецепт, который я извлекла из страницы.")
        await _reply_text_chunked(update, format_full_recipe_for_approval(source.strip()))
    await send_social_descriptions_for_recipe(update, source)


async def _reply_text_with_optional_markup(update: Update, text: str, reply_markup=None) -> None:
    """Отправляет сообщение с клавиатурой, но не ломается на тестовых стабах без kwargs."""
    if update.message is None:
        return
    if reply_markup is None:
        await safe_reply_text(update.message, text)
        return
    await safe_reply_text(update.message, text, reply_markup=reply_markup)


def _concept_choice_markup(concepts: list[dict]) -> object | None:
    """Клавиатура выбора концепта по номеру; по 2 кнопки в строке."""
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    if not concepts:
        return None
    buttons = [str(i) for i in range(1, len(concepts) + 1)]
    rows: list[list[str]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.extend([["Добавь"], ["Исправь"], [NAV_BUTTON_START_OVER]])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _parse_concept_choice(text: str) -> int | None:
    """Возвращает номер концепта только для явного выбора, а не для любого числа в фидбеке."""
    match = re.fullmatch(r"(?:концепт\s*)?(\d+)", text.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _series_choice_markup(session: dict) -> object | None:
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    rows = [[str(i)] for i in range(1, len(session.get(_SERIES_CHOICE_IDS_SESSION_KEY) or []) + 1)]
    rows.append([_SERIES_BUTTON_NEW])
    rows.append([_SERIES_BUTTON_SKIP])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _format_series_choice_text(series_items) -> str:
    if not series_items:
        return (
            "Можно привязать это видео к серии.\n"
            "Активных серий для проекта пока нет: создайте новую или выберите «Без серии»."
        )
    lines = ["Можно привязать это видео к серии. Выберите номер:"]
    for idx, item in enumerate(series_items, start=1):
        lines.append(f"{idx}. {format_series_label(item)}")
    return "\n".join(lines)


async def _ask_series_choice(update: Update, session: dict) -> None:
    project_slug = str(session.get(_PROJECT_KEY_SESSION) or "").strip() or None
    series_items = list_active_series(project_slug)
    session[_SERIES_CHOICE_IDS_SESSION_KEY] = [item.id for item in series_items]
    session["state"] = STATE_WAITING_SERIES_CHOICE
    await _reply_text_with_optional_markup(
        update,
        _format_series_choice_text(series_items),
        reply_markup=_series_choice_markup(session),
    )


def _parse_series_create_text(text: str) -> tuple[str, str | None, int] | None:
    raw = " ".join(str(text or "").strip().split())
    if not raw:
        return None
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) == 2:
        title = parts[0]
        days_text = parts[1]
        if not title or not re.fullmatch(r"\d+", days_text):
            return None
        return title, None, int(days_text)
    if len(parts) < 3:
        return None
    ru_title = parts[0]
    en_title = parts[1]
    days_text = parts[2]
    days_match = re.fullmatch(r"\d+", days_text)
    legacy_match = re.fullmatch(r"\d+\s*(?:/|из|of)\s*(\d+)", days_text, flags=re.IGNORECASE)
    if not ru_title or not en_title or not (days_match or legacy_match):
        return None
    total_videos = int(days_match.group(0) if days_match else legacy_match.group(1))
    return ru_title, en_title, total_videos


def _copy_series_context(source: dict, target: dict) -> None:
    for key in _SERIES_SESSION_KEYS:
        if key in source:
            target[key] = source[key]


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/generate <текст>``: обычный старт пайплайна (без fastpath)."""
    await _start_generation(update, context, prefer_vertex_api=False)


async def quick_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/quick <текст>``: старт пайплайна с приоритетом VERTEX_API_KEY."""
    await _start_generation(update, context, prefer_vertex_api=True)


async def _announce_generation_mode(
    update: Update,
    *,
    chat_id: int,
    prefer_vertex_api: bool,
) -> None:
    """Отправляет сообщения о режиме запуска и обязательные предупреждения."""
    _bump_debounce_generation(chat_id)
    warn = _elevenlabs_voice_warning()
    if warn:
        await update.message.reply_text(warn)
    if prefer_vertex_api:
        await update.message.reply_text(
            "Приняла режим «быстро»: для Gemini включаю приоритет VERTEX_API_KEY на этом запуске."
        )


async def _start_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    prefer_vertex_api: bool,
) -> None:
    """
    Общий обработчик старта пайплайна для `/generate` и `/quick`.
    """
    if update.message is None or update.effective_chat is None:
        return
    recipe = _extract_recipe_from_command_text(update.message.text or "")
    if not recipe.strip():
        cmd = "/quick" if prefer_vertex_api else "/generate"
        await update.message.reply_text(
            f'Пришлите текст рецепта после команды.\nПример: {cmd} "Суп: 1) нарезать... 2) варить..."'
        )
        return
    await _start_generation_with_recipe(
        update,
        context,
        recipe=recipe,
        prefer_vertex_api=prefer_vertex_api,
    )


async def _start_generation_with_recipe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    recipe: str,
    prefer_vertex_api: bool,
) -> None:
    """
    Запускает пайплайн генерации по уже полученному тексту рецепта/идеи.

    Используется как командным входом (`/generate`, `/quick`), так и кнопочным UX первого шага.
    """
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    existing_session = SESSIONS.get(chat_id) or {}
    selected_project_key = str(existing_session.get(_PROJECT_KEY_SESSION) or "").strip()
    await _announce_generation_mode(update, chat_id=chat_id, prefer_vertex_api=prefer_vertex_api)
    session = {
        **existing_session,
        "state": STATE_WAITING_SERIES_CHOICE,
        SERIES_PENDING_NEXT_SESSION_KEY: SERIES_NEXT_GENERATION,
        SERIES_PENDING_RECIPE_SESSION_KEY: recipe,
        SERIES_PENDING_PREFER_VERTEX_SESSION_KEY: prefer_vertex_api,
    }
    if selected_project_key:
        session[_PROJECT_KEY_SESSION] = selected_project_key
    SESSIONS[chat_id] = session
    await _ask_series_choice(update, session)
    return


async def _continue_generation_with_recipe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    recipe: str,
    prefer_vertex_api: bool,
) -> None:
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    existing_session = SESSIONS.get(chat_id) or {}
    selected_project_key = str(existing_session.get(_PROJECT_KEY_SESSION) or "").strip()
    source_url = _extract_first_http_url(recipe)
    if source_url:
        try:
            source_text = _fetch_recipe_text_from_url(source_url)
        except Exception as e:
            await update.message.reply_text(
                f"Не удалось извлечь рецепт по ссылке: {_user_error_message(e)}\n"
                "Пришлите, пожалуйста, текст рецепта сообщением."
            )
            return
        await update.message.reply_text(
            "Нашла ссылку и извлекла текст страницы. Передаю весь текст нейросети, чтобы она выделила рецепт."
        )
        await reply_text_without_keyboard(update.message, "Извлекаю рецепт, ждите.")
        try:
            recipe = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "extract_recipe_from_source_text",
                lambda: generate_recipe_from_source_text(source_text, source_url=source_url),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return
        except Exception as e:
            logger.exception("recipe extraction from source text failed")
            await update.message.reply_text(f"Ошибка извлечения рецепта из ссылки: {_user_error_message(e)}")
            return
        await update.message.reply_text(
            "Ниже рецепт, который нейросеть извлекла из текста страницы. Подтвердите или исправьте."
        )
    parsed_steps = _try_parse_direct_recipe_from_user_text(recipe)
    # Если пользователь сразу прислал рецепт по шагам — пропускаем этап концептов.
    if parsed_steps or source_url:
        new_session = _build_direct_recipe_session(
            recipe,
            parsed_steps or [],
            prefer_vertex_api=prefer_vertex_api,
        )
        if selected_project_key:
            new_session[_PROJECT_KEY_SESSION] = selected_project_key
        _copy_series_context(existing_session, new_session)
        SESSIONS[chat_id] = new_session
        logger.info(
            "chat=%s state=%s (recipe provided directly%s)",
            chat_id,
            STATE_WAITING_RECIPE_APPROVAL,
            " from link" if source_url else "",
        )
        await update.message.reply_text(
            (
                "Приняла рецепт из ссылки. Ниже текст для подтверждения; после «ОК» сгенерирую план сцен."
                if source_url
                else "Приняла рецепт из сообщения. Ниже тот же текст для подтверждения; после «ОК» сгенерирую план сцен."
            )
        )
        await _reply_text_chunked(update, format_full_recipe_for_approval(recipe.strip()))
        await _reply_text_with_optional_markup(
            update,
            "Если рецепт подходит — нажми «ОК». "
            "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли текст правки.",
            reply_markup=ok_only_markup(),
        )
        return

    try:
        await reply_text_without_keyboard(update.message, "Генерирую варианты рецепта, ждите.")
        result = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "generate_recipe_concepts",
            lambda: generate_recipe_concepts(recipe),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        logger.exception("generate failed after retries")
        await update.message.reply_text(str(e))
        return
    except Exception as e:
        logger.exception("generate failed")
        await update.message.reply_text(f"Ошибка генерации: {_user_error_message(e)}")
        return
    if result is None:
        await update.message.reply_text("Ошибка генерации: не удалось получить ответ модели.")
        return
    new_session = _build_concepts_session(
        recipe,
        result["concepts"],
        prefer_vertex_api=prefer_vertex_api,
    )
    if selected_project_key:
        new_session[_PROJECT_KEY_SESSION] = selected_project_key
    _copy_series_context(existing_session, new_session)
    SESSIONS[chat_id] = new_session
    logger.info("chat=%s state=%s", chat_id, STATE_WAITING_CONCEPT)
    await _reply_text_chunked(update, result["text_plan"])
    await _reply_text_with_optional_markup(
        update,
        "Выберите номер концепта кнопкой. "
        "Либо нажмите «Добавь»/«Исправь» и отправьте правку для нейросети.",
        reply_markup=_concept_choice_markup(result["concepts"]),
    )


async def _continue_generation_after_series(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
) -> bool:
    next_step = str(session.pop(SERIES_PENDING_NEXT_SESSION_KEY, "") or "").strip()
    session.pop(_SERIES_CHOICE_IDS_SESSION_KEY, None)
    if next_step == SERIES_NEXT_OVERLAY_TITLE:
        session["state"] = "state_waiting_overlay_title_ru"
        SESSIONS[chat_id] = session
        await reply_text_without_keyboard(
            update.message,
            "Теперь введите русский заголовок для верхней надписи.",
        )
        return True
    recipe = str(session.get(SERIES_PENDING_RECIPE_SESSION_KEY) or "").strip()
    prefer_vertex_api = bool(session.get(SERIES_PENDING_PREFER_VERTEX_SESSION_KEY))
    if not recipe:
        await update.message.reply_text("Не вижу исходный рецепт или идею. Начните генерацию заново.")
        session["state"] = ""
        SESSIONS[chat_id] = session
        return True
    session.pop(SERIES_PENDING_RECIPE_SESSION_KEY, None)
    session.pop(SERIES_PENDING_PREFER_VERTEX_SESSION_KEY, None)
    SESSIONS[chat_id] = session
    await _continue_generation_with_recipe(
        update,
        context,
        recipe=recipe,
        prefer_vertex_api=prefer_vertex_api,
    )
    return True


async def _handle_waiting_series_choice_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    low = str(text or "").strip().lower()
    if low == _SERIES_BUTTON_SKIP.lower():
        attach_series_to_session(session, None)
        session[SERIES_CHOICE_CONFIRMED_SESSION_KEY] = True
        await update.message.reply_text("Ок, это видео будет без серии.")
        return await _continue_generation_after_series(update, context, chat_id=chat_id, session=session)
    if low == _SERIES_BUTTON_NEW.lower():
        session["state"] = STATE_WAITING_SERIES_CREATE
        SESSIONS[chat_id] = session
        await _reply_text_with_optional_markup(
            update,
            "Пришлите серию в формате:\n"
            "название | дней\n\n"
            "Например: летние напитки | 10\n"
            "Если нужен отдельный английский заголовок: летние напитки | summer drinks | 10",
            reply_markup=ok_only_markup(),
        )
        return True
    choice = _parse_concept_choice(text)
    ids = session.get(_SERIES_CHOICE_IDS_SESSION_KEY) or []
    if choice is not None and 1 <= choice <= len(ids):
        payload = select_series_payload(str(ids[choice - 1]))
        if payload is None:
            await update.message.reply_text(
                "Не нашла активную серию или она уже закончилась. Выберите заново или создайте новую."
            )
            await _ask_series_choice(update, session)
            SESSIONS[chat_id] = session
            return True
        attach_series_to_session(session, payload)
        session[SERIES_CHOICE_CONFIRMED_SESSION_KEY] = True
        await update.message.reply_text(f"Серия выбрана: {format_series_label(payload)}.")
        return await _continue_generation_after_series(update, context, chat_id=chat_id, session=session)
    await _reply_text_with_optional_markup(
        update,
        "Выберите номер серии, «Создать серию» или «Без серии».",
        reply_markup=_series_choice_markup(session),
    )
    return True


async def _handle_waiting_series_create_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    if str(text or "").strip().lower() == _SERIES_BUTTON_BACK.lower():
        await _ask_series_choice(update, session)
        SESSIONS[chat_id] = session
        return True
    parsed = _parse_series_create_text(text)
    if parsed is None:
        await _reply_text_with_optional_markup(
            update,
            "Не смогла разобрать серию. Формат: название | дней",
            reply_markup=ok_only_markup(),
        )
        return True
    ru_title, en_title, total_videos = parsed
    try:
        payload = create_series(
            title_ru=ru_title,
            title_en=en_title,
            total_videos=total_videos,
            project_slug=str(session.get(_PROJECT_KEY_SESSION) or "").strip() or None,
        )
    except Exception as e:
        logger.exception("content series creation failed")
        await update.message.reply_text(f"Ошибка создания серии: {_user_error_message(e)}")
        return True
    attach_series_to_session(session, payload)
    session[SERIES_CHOICE_CONFIRMED_SESSION_KEY] = True
    await update.message.reply_text(f"Серия создана: {format_series_label(payload)}.")
    return await _continue_generation_after_series(update, context, chat_id=chat_id, session=session)


async def _handle_waiting_concept_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """Обрабатывает состояние `STATE_WAITING_CONCEPT`."""
    choice = _parse_concept_choice(text)
    if choice is not None:
        idx = choice - 1
        concepts = session.get("concepts") or []
        if idx < 0 or idx >= len(concepts):
            await update.message.reply_text("Неверный номер концепта. Выбери из списка.")
            return True
        selected = concepts[idx]
        concept_text = f"{selected.get('name', '')}. {selected.get('hook', '')}"
        session["selected_concept"] = concept_text
        session["selected_concept_name"] = str(selected.get("name", "")).strip() or concept_text
        try:
            await reply_text_without_keyboard(update.message, "Генерирую полный рецепт, ждите.")
            recipe_full = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_recipe_full_text",
                lambda: generate_recipe_full_text(
                    concept_text,
                    dish_name=session.get("selected_concept_name"),
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("recipe full text generation failed")
            await update.message.reply_text(f"Ошибка генерации рецепта: {_user_error_message(e)}")
            return True
        session["state"] = STATE_WAITING_RECIPE_APPROVAL
        session["recipe_full_text"] = recipe_full
        session["precheck_recipe_steps"] = None
        session["recipe_steps"] = None
        session["scenes_plan"] = None
        session["scenes_text"] = None
        session["frame_prompts"] = None
        session["frame_prompts_text"] = None
        session["end_frame_scene_requests"] = []
        session["user_feedback"] = None
        await _reply_text_chunked(update, format_full_recipe_for_approval(recipe_full))
        await _reply_text_with_optional_markup(
            update,
            "Если рецепт подходит — нажми «ОК». "
            "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли текст правки.",
            reply_markup=ok_only_markup(),
        )
        return True

    session["state"] = STATE_REFINING_CONCEPT
    session["user_feedback"] = text
    try:
        await reply_text_without_keyboard(update.message, "Дорабатываю концепты, ждите.")
        result = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "refine_recipe_concepts",
            lambda: generate_recipe_concepts(
                session["user_input"],
                user_feedback=text,
                previous_concepts=session.get("concepts"),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        session["state"] = STATE_WAITING_CONCEPT
        return True
    except Exception as e:
        logger.exception("concept refinement failed")
        await update.message.reply_text(f"Ошибка доработки концептов: {_user_error_message(e)}")
        session["state"] = STATE_WAITING_CONCEPT
        return True
    session["concepts"] = result["concepts"]
    session["state"] = STATE_WAITING_CONCEPT
    await update.message.reply_text("Обновила концепты по фидбеку:")
    await _reply_text_chunked(update, result["text_plan"])
    await _reply_text_with_optional_markup(
        update,
        "Выберите номер концепта кнопкой. "
        "Либо нажмите «Добавь»/«Исправь» и отправьте правку для нейросети.",
        reply_markup=_concept_choice_markup(result["concepts"]),
    )
    return True


async def _handle_waiting_recipe_approval_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """Обрабатывает состояние `STATE_WAITING_RECIPE_APPROVAL`."""
    if text.lower() in {"ок", "ok", "okay", "approve"}:
        async def _on_scene_plan_fail(p: dict[str, Any]) -> None:
            if update.message is not None:
                await _reply_text_chunked(update, _format_scenes_plan_attempt_notice(p))

        full_recipe = str(session.get("recipe_full_text") or "").strip()
        if not full_recipe:
            await update.message.reply_text("Нет текста рецепта. Сначала согласуйте рецепт.")
            return True

        try:
            await reply_text_without_keyboard(update.message, "Готовлю viral brief и план сцен, ждите.")
            viral_brief = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_viral_concept_brief",
                lambda: generate_viral_concept_brief(
                    session["selected_concept"],
                    full_recipe_text=full_recipe,
                    sponsored_content=bool(session.get("sponsored_content")),
                    brand_context=session.get("brand_context"),
                    analytics_feedback=session.get("analytics_feedback"),
                ),
            )
            scenes = await _call_with_503_retries(
                update,
                context,
                chat_id,
                "generate_scenes_plan",
                lambda: generate_scenes_plan(
                    session["selected_concept"],
                    full_recipe_text=full_recipe,
                    on_attempt_failure=_on_scene_plan_fail,
                    viral_concept_brief=viral_brief,
                    sponsored_content=bool(session.get("sponsored_content")),
                    brand_context=session.get("brand_context"),
                    analytics_feedback=session.get("analytics_feedback"),
                ),
            )
        except ServiceUnavailableRetriesExhaustedError as e:
            await update.message.reply_text(str(e))
            return True
        except Exception as e:
            logger.exception("scene plan failed")
            await update.message.reply_text(f"Ошибка генерации сцен: {_user_error_message(e)}")
            return True
        session["recipe_steps"] = [
            str(s.get("text") or "").strip()
            for s in (scenes.get("json_plan") or [])
            if isinstance(s, dict)
        ]
        session["state"] = STATE_WAITING_SCENE_APPROVAL
        session["viral_concept_brief"] = scenes.get("viral_concept_brief") or viral_brief
        session["romantic_drama_subplot"] = scenes.get("romantic_drama_subplot") or {}
        session["retention_review"] = scenes.get("retention_review") or {}
        session["scenes_plan"] = scenes["json_plan"]
        session["scenes_text"] = scenes["text_plan"]
        session["frame_prompts"] = None
        session["frame_prompts_text"] = None
        report = _format_scenes_regeneration_report(scenes.get("regeneration_events") or [])
        if report:
            await _reply_text_chunked(update, report)
        await _reply_text_chunked(
            update,
            format_viral_application_summary(
                session["viral_concept_brief"],
                session["scenes_plan"],
                session.get("retention_review"),
            ),
        )
        await _reply_text_chunked(update, scenes["text_plan"])
        await _reply_text_with_optional_markup(
            update,
            "Если план подходит — нажми «ОК». "
            "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли правку текстом.",
            reply_markup=ok_only_markup(),
        )
        return True

    session["state"] = STATE_REFINING_RECIPE
    session["user_feedback"] = text
    try:
        await reply_text_without_keyboard(update.message, "Дорабатываю рецепт, ждите.")
        recipe_full = await _call_with_503_retries(
            update,
            context,
            chat_id,
            "refine_recipe_full_text",
            lambda: generate_recipe_full_text(
                str(session.get("selected_concept") or "").strip(),
                dish_name=session.get("selected_concept_name"),
                user_feedback=text,
                previous_text=session.get("recipe_full_text"),
            ),
        )
    except ServiceUnavailableRetriesExhaustedError as e:
        await update.message.reply_text(str(e))
        session["state"] = STATE_WAITING_RECIPE_APPROVAL
        return True
    except Exception as e:
        logger.exception("recipe regeneration failed")
        await update.message.reply_text(f"Ошибка перегенерации рецепта: {_user_error_message(e)}")
        session["state"] = STATE_WAITING_RECIPE_APPROVAL
        return True
    session["state"] = STATE_WAITING_RECIPE_APPROVAL
    session["recipe_full_text"] = recipe_full
    session["precheck_recipe_steps"] = None
    session["recipe_steps"] = None
    await update.message.reply_text("Обновила рецепт по правкам:")
    await _reply_text_chunked(update, format_full_recipe_for_approval(recipe_full))
    await _reply_text_with_optional_markup(
        update,
        "Если рецепт подходит — нажми «ОК». "
        "Если нужно доработать — нажми «Добавь» или «Исправь», затем пришли правку текстом.",
        reply_markup=ok_only_markup(),
    )
    return True
