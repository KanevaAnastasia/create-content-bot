from ..constants import GEMINI_GENERATE_TIMEOUT_SEC_ENV_NAME
from ..constants import STATE_WAITING_SCENE_MEDIA
from ..character_catalog import GRANDMA_KITCHEN_PROJECT_SLUG, GRANDMA_KITCHEN_PROJECT_TITLE

from .handlers_state_deps import (
    Any,
    ContextTypes,
    ELEVENLABS_VOICE_ID_ENV_NAME,
    GEMINI_KEYS_ENV_NAME,
    GEMINI_KEY_ENV_NAME,
    GEMINI_MODEL_ENV_NAME,
    GEMINI_MODEL_FALLBACKS_ENV_NAME,
    SESSIONS,
    SCENE_MEDIA_APPROVE_MODE_AI,
    SCENE_MEDIA_APPROVE_MODE_SESSION_KEY,
    SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY,
    SCENE_MEDIA_AI_APPROVE_BUTTON,
    TELEGRAM_MAX_TEXT_LEN,
    TOKEN_ENV_NAME,
    Update,
    SyntaxFallbackPendingConfirmation,
    VideoProviderChoiceRequired,
    _bump_debounce_generation,
    _call_with_503_retries,
    _elevenlabs_voice_warning,
    encode_video_provider_choice_for_session,
    encode_syntax_fallback_pending_for_session,
    _generate_image_with_lenient_fallback,
    _generate_video_bytes_with_high_load_retry,
    _image_scene_prompt,
    _is_video_prompt_filtered_error,
    _mask_secret_tail,
    _parse_video_command_args,
    _regenerate_video_prompt_from_text,
    run_syntax_fallback_from_session_payload,
    run_vertex_fallback_from_session_payload,
    syntax_fallback_parse_user_confirmation,
    video_provider_choice_parse_user_selection,
    _user_error_message,
    _wants_restart_end_frame,
    _wants_restart_start_frame,
    apply_veo3_lowercase_format,
    asyncio,
    build_human_readable_plan,
    format_full_recipe_for_approval,
    generate_recipe_concepts,
    generate_recipe_full_text,
    generate_scenes_plan,
    gemini_generate_sync,
    get_last_video_result_source,
    get_last_video_result_uri,
    io,
    logger,
    os,
)
from .handlers_keyboards import (
    FIRST_STEP_BUTTON_DESCRIPTION,
    FIRST_STEP_BUTTON_GENERATE,
    FIRST_STEP_BUTTON_IMAGE,
    FIRST_STEP_BUTTON_PROJECT,
    FIRST_STEP_BUTTON_PROJECT_IMAGE,
    FIRST_STEP_BUTTON_SOUND,
    FIRST_STEP_BUTTON_VIDEO,
    first_step_menu_markup,
    reply_text_without_keyboard,
)

NAV_BUTTON_PREVIOUS_STEP = "Предыдущий шаг"
NAV_BUTTON_START_OVER = "В начало"

PROJECT_KEY_SESSION = "selected_project_key"
PROJECT_KEY_GRANDMA_CAT_COOKING = GRANDMA_KITCHEN_PROJECT_SLUG
PROJECT_LABEL_GRANDMA_CAT_COOKING = GRANDMA_KITCHEN_PROJECT_TITLE
PROJECT_LEGACY_KEY_GRANDMA_CAT_COOKING = "grandma_cat_cooking"
PROJECT_LEGACY_LABEL_GRANDMA_CAT_COOKING = "Бабушка + кот (кулинарный Shorts)"
PROJECT_BUTTON_BACK = "Назад"


def _project_label_by_key(project_key: str | None) -> str:
    """Человекочитаемое имя проекта по ключу."""
    if (project_key or "").strip() in {PROJECT_KEY_GRANDMA_CAT_COOKING, PROJECT_LEGACY_KEY_GRANDMA_CAT_COOKING}:
        return PROJECT_LABEL_GRANDMA_CAT_COOKING
    return PROJECT_LABEL_GRANDMA_CAT_COOKING


def project_choice_markup():
    """Клавиатура выбора проекта для первого шага."""
    try:
        from telegram import ReplyKeyboardMarkup
    except Exception:
        return None
    return ReplyKeyboardMarkup(
        keyboard=[
            [PROJECT_LABEL_GRANDMA_CAT_COOKING],
            [PROJECT_BUTTON_BACK],
            [NAV_BUTTON_START_OVER],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def _regenerate_image_prompt_from_feedback(scene: dict, feedback: str) -> str:
    current = _image_scene_prompt(scene)
    shot = str(scene.get("shot_type", "medium"))
    anchors = ", ".join(str(x) for x in (scene.get("continuity_anchors") or []) if str(x).strip())
    prompt = (
        "Ты редактируешь промпт для генерации СТАРТОВОГО кадра.\n"
        "Верни только один готовый промпт на русском, без пояснений.\n"
        "Требования:\n"
        "- промпт максимально конкретный и самодостаточный\n"
        "- если есть еда, перечисляй конкретные ингредиенты\n"
        "- если в кадре есть надписи, явно укажи: все надписи на русском\n"
        "- картинка должна быть в одном визуальном стиле с референсами проекта (бабушка и кот как на character sheets)\n"
        "- сохраняй общую сцену, ракурс и предметную непрерывность: посуда, кастрюля/сковорода, рабочая поверхность, "
        "ингредиенты и состояние еды\n\n"
        f"РАКУРС: {shot}\n"
        f"CONTINUITY ANCHORS: {anchors or '-'}\n"
        f"ТЕКУЩИЙ ПРОМПТ:\n{current}\n\n"
        f"ФИДБЕК ПОЛЬЗОВАТЕЛЯ:\n{feedback}\n"
    )
    raw = await asyncio.to_thread(gemini_generate_sync, prompt)
    return raw.strip()


async def _reply_text_chunked(update: Update, text: str, *, separator: str = "\n\n---\n\n") -> None:
    """
    Безопасно отправляет длинный текст в Telegram, разбивая на части меньше лимита.
    """
    if update.message is None:
        return
    if len(text) <= TELEGRAM_MAX_TEXT_LEN:
        await update.message.reply_text(text)
        return
    parts = text.split(separator) if separator and separator in text else [text]
    chunk = ""
    for part in parts:
        candidate = part if not chunk else f"{chunk}{separator}{part}"
        if len(candidate) <= TELEGRAM_MAX_TEXT_LEN:
            chunk = candidate
            continue
        if chunk:
            await update.message.reply_text(chunk)
        if len(part) <= TELEGRAM_MAX_TEXT_LEN:
            chunk = part
            continue
        # Если один блок сам по себе слишком большой, режем его по длине.
        start = 0
        while start < len(part):
            end = min(start + TELEGRAM_MAX_TEXT_LEN, len(part))
            await update.message.reply_text(part[start:end])
            start = end
        chunk = ""
    if chunk:
        await update.message.reply_text(chunk)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда ``/start``: проверка живости бота, без изменения сессии.
    """
    if update.message is not None:
        effective_chat = getattr(update, "effective_chat", None)
        chat_id = effective_chat.id if effective_chat is not None else None
        session = SESSIONS.get(chat_id) if chat_id is not None else None
        current_project = _project_label_by_key(
            str((session or {}).get(PROJECT_KEY_SESSION) or PROJECT_KEY_GRANDMA_CAT_COOKING)
        )
        await update.message.reply_text(
            "Выберите первый шаг кнопкой: Генерировать, Описание, Изображение, Видео или Проект.\n"
            f"Текущий проект: {current_project}",
            reply_markup=first_step_menu_markup(),
        )
        warn = _elevenlabs_voice_warning()
        if warn:
            await update.message.reply_text(warn)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда ``/cancel`` (alias: ``/reset``): полный сброс текущей сессии диалога.
    """
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    if chat_id in SESSIONS:
        del SESSIONS[chat_id]
    _bump_debounce_generation(chat_id)
    await update.message.reply_text(
        "Диалог сброшен. Выберите первый шаг кнопкой.",
        reply_markup=first_step_menu_markup(),
    )


async def check_env(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда ``/check_env``: показывает, какие ключи окружения настроены.
    """
    if update.message is None:
        return
    required = [TOKEN_ENV_NAME, "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"]
    optional = [
        GEMINI_MODEL_ENV_NAME,
        GEMINI_MODEL_FALLBACKS_ENV_NAME,
        GEMINI_GENERATE_TIMEOUT_SEC_ENV_NAME,
        "VERTEX_API_KEY",
        "VERTEX_IMAGE_MODEL",
        "VERTEX_IMAGE_MODEL_FALLBACKS",
        "ELEVENLABS_API_KEY",
        ELEVENLABS_VOICE_ID_ENV_NAME,
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SYNTAX_FALLBACK_ENABLED",
        "SYNTAX_FALLBACK_MODE",
        "SYNTAX_FALLBACK_TIMEOUT_SEC",
        "SYNTAX_BOT_USERNAME",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELETHON_SESSION_PATH",
        "TELETHON_SESSION_PATHS",
        "TELETHON_STRING_SESSION",
        "TELETHON_STRING_SESSIONS",
        "AI_APPROVAL_ENABLED",
        "AI_APPROVAL_MODEL",
        "AI_APPROVAL_STRICTNESS",
        "AI_APPROVAL_MAX_REGENERATE_ATTEMPTS",
        "AI_APPROVAL_IMAGE_THRESHOLD",
        "AI_APPROVAL_VIDEO_THRESHOLD",
        "AI_APPROVAL_HUMAN_REVIEW_THRESHOLD",
        "AI_APPROVAL_EXTRACT_VIDEO_FRAMES",
        "AI_APPROVAL_FRAME_COUNT",
        "AI_APPROVAL_AUDIO_TRANSCRIPTION_ENABLED",
        "N8N_PUBLISH_WEBHOOK_URL",
        "N8N_PUBLISH_WEBHOOK_TOKEN",
        "CONTENT_FABRIC_PUBLIC_BASE_URL",
        "PUBLICATION_ARTIFACTS_DIR",
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID_RU",
        "YOUTUBE_CHANNEL_ID_EN",
        "INSTAGRAM_APP_ID",
        "INSTAGRAM_APP_SECRET",
        "INSTAGRAM_ACCESS_TOKEN",
        "INSTAGRAM_BUSINESS_ACCOUNT_ID_RU",
        "INSTAGRAM_BUSINESS_ACCOUNT_ID_EN",
        "INSTAGRAM_PAGE_ID_RU",
        "INSTAGRAM_PAGE_ID_EN",
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
        "TIKTOK_ACCESS_TOKEN",
        "TIKTOK_OPEN_ID",
        "VK_CLIENT_ID",
        "VK_CLIENT_SECRET",
        "VK_ACCESS_TOKEN",
        "VK_GROUP_ID_RU",
        "RUTUBE_CHANNEL_ID_RU",
    ]
    lines = ["Проверка окружения:"]
    for key in required:
        val = (os.getenv(key) or "").strip()
        lines.append(f"{key}: {'OK' if val else 'MISSING'}")
    single_key = (os.getenv(GEMINI_KEY_ENV_NAME) or "").strip()
    many_keys_raw = (os.getenv(GEMINI_KEYS_ENV_NAME) or "").strip()
    many_keys = [x.strip() for x in many_keys_raw.split(",") if x.strip()] if many_keys_raw else []
    many_keys_masked = ", ".join(_mask_secret_tail(x) for x in many_keys[:5]) if many_keys else "-"
    lines.append(
        "Gemini transport: используйте Vertex AI mode (VERTEX_API_KEY + GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION)."
    )
    lines.append(f"GEMINI_API_KEY (legacy): {_mask_secret_tail(single_key)}")
    lines.append(f"GEMINI_API_KEYS sample (legacy): {many_keys_masked}")
    for key in optional:
        val = (os.getenv(key) or "").strip()
        lines.append(f"{key}: {'set' if val else 'not set'}")
    session_path = (os.getenv("TELETHON_SESSION_PATH") or "").strip()
    if session_path:
        lines.append(f"TELETHON_SESSION_FILE_EXISTS: {'yes' if os.path.exists(session_path) else 'no'}")
    else:
        lines.append("TELETHON_SESSION_FILE_EXISTS: n/a (path not set)")
    await update.message.reply_text("\n".join(lines))


async def ai_approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/ai_approve``: включает ИИ-апрув для текущего медиа-пайплайна."""
    _ = context
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id)
    if not isinstance(session, dict) or session.get("state") != STATE_WAITING_SCENE_MEDIA:
        await update.message.reply_text(
            f"{SCENE_MEDIA_AI_APPROVE_BUTTON} можно включить на этапе пошаговой генерации сцен. После включения появится кнопка `СТОП` для возврата в ручной режим."
        )
        return
    session.pop(SCENE_MEDIA_AUTO_APPROVE_SESSION_KEY, None)
    session[SCENE_MEDIA_APPROVE_MODE_SESSION_KEY] = SCENE_MEDIA_APPROVE_MODE_AI
    session.pop("scene_media_ai_human_review", None)
    await update.message.reply_text(
        "Включён 🤖 ИИ апрув. Теперь промты, стартовые фреймы и видео будут проверяться ИИ по чек-листу. "
        "Хорошие результаты будут проходить автоматически, спорные будут отправляться на ручную проверку. "
        "Кнопка `СТОП` вернёт ручной режим."
    )
    from .handlers_state_media import _run_scene_media_ai_approve

    await _run_scene_media_ai_approve(update, context=context, chat_id=chat_id, session=session)


async def generate_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда ``/image <prompt>``: сгенерировать одно изображение по текстовому промпту.
    """
    if update.message is None:
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""
    if not prompt:
        await update.message.reply_text('Пришлите промпт после команды.\nПример: /image "Бабушка на кухне смешивает творог и яйцо в миске"')
        return
    await reply_text_without_keyboard(update.message, "Генерируем изображение, ждите.")
    try:
        image_bytes = await _generate_image_with_lenient_fallback(prompt)
    except Exception as e:
        logger.exception("image command generation failed")
        await update.message.reply_text(f"Ошибка генерации изображения: {e}")
        return
    bio = io.BytesIO(image_bytes)
    bio.name = "image.png"
    await update.message.reply_photo(photo=bio, caption="Изображение готово.")
    await update.message.reply_text(
        "Выберите первый шаг кнопкой.",
        reply_markup=first_step_menu_markup(),
    )


async def _extract_video_source_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes | None:
    """
    Возвращает bytes исходного изображения для `/video`.

    Источник:
    1) фото в текущем сообщении;
    2) фото в сообщении, на которое сделали reply.
    """
    if update.message is None:
        return None
    msg = update.message
    source_photo = None
    if msg.photo:
        source_photo = msg.photo[-1]
    elif msg.reply_to_message and msg.reply_to_message.photo:
        source_photo = msg.reply_to_message.photo[-1]
    if source_photo is None:
        return None
    tg_file = await context.bot.get_file(source_photo.file_id)
    data = await tg_file.download_as_bytearray()
    return bytes(data)


async def generate_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда ``/video <секунды> <промпт>``: сгенерировать одно видео по изображению.

    Изображение берётся либо из фото в этом же сообщении, либо из reply на сообщение с фото.
    """
    if update.message is None:
        return
    raw = update.message.text or update.message.caption or ""
    try:
        duration_sec, prompt = _parse_video_command_args(raw)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    source_image = await _extract_video_source_image(update, context)
    if not source_image:
        await update.message.reply_text(
            "Для /video приложите одно изображение или ответьте командой на сообщение с фото."
        )
        return
    await reply_text_without_keyboard(update.message, "Генерируем видео, это может занять некоторое время.")
    try:
        video_bytes = await _generate_video_bytes_with_high_load_retry(
            update,
            prompt=prompt,
            model="veo3_fast",
            duration_sec=duration_sec,
            start_image=source_image,
            end_image=None,
            quality="720",
            aspect_ratio="9:16",
            audio_enabled=True,
        )
    except VideoProviderChoiceRequired as pending_choice:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        sess = SESSIONS.setdefault(chat_id, {})
        payload = encode_video_provider_choice_for_session(pending_choice)
        payload["source"] = "video_command"
        payload["original_prompt"] = prompt
        payload["duration_for_caption"] = duration_sec
        sess["video_provider_choice_pending"] = payload
        await update.message.reply_text(
            "Syntax не вернул видео.\n\n"
            "Можно:\n"
            "1. написать `Syntax` и запустить эту же генерацию ещё раз;\n"
            "2. написать `Vertex` и попробовать резервный канал Vertex/Veo."
        )
        return
    except SyntaxFallbackPendingConfirmation as pending_exc:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        sess = SESSIONS.setdefault(chat_id, {})
        payload = encode_syntax_fallback_pending_for_session(pending_exc)
        payload["source"] = "video_command"
        payload["original_prompt"] = prompt
        payload["duration_for_caption"] = duration_sec
        sess["syntax_fallback_pending"] = payload
        await update.message.reply_text(
            "Vertex (Veo) отклонил генерацию по политике безопасности после всех попыток в регионах.\n\n"
            "Включён резервный канал Syntax. Режим manual (human-in-the-loop): нужно ваше подтверждение.\n\n"
            "Чтобы запустить генерацию через Syntax, ответьте: «да» или «ок».\n"
            "Чтобы отказаться и перейти к автоматическому переписыванию промпта, ответьте: «нет»."
        )
        return
    except Exception as e:
        logger.exception("single video generation failed")
        if _is_video_prompt_filtered_error(e):
            err_text = str(e)
            try:
                safe_prompt = await _regenerate_video_prompt_from_text(prompt, err_text)
                await update.message.reply_text(
                    "Видео отклонено политиками/RAI. Автоперегенерацию не запускаю без апрува.\n"
                    "Кандидат prompt:\n"
                    f"{safe_prompt}\n\n"
                    "Отредактируйте при необходимости и запустите снова командой:\n"
                    f"/video {duration_sec} <ваш prompt>"
                )
                return
            except Exception as rewrite_err:
                logger.exception("single video safety rewrite failed")
                await update.message.reply_text(
                    "Не удалось подготовить prompt после safety-filter.\n"
                    f"Предложенный prompt: {safe_prompt if 'safe_prompt' in locals() else '(не сформирован)'}\n"
                    f"Ошибка: {_user_error_message(rewrite_err, domain='video')}"
                )
                return
        await update.message.reply_text(f"Ошибка генерации видео: {_user_error_message(e, domain='video')}")
        return
    vb = io.BytesIO(video_bytes)
    vb.name = "video.mp4"
    await update.message.reply_video(video=vb, caption=f"Видео готово ({duration_sec} сек, 9:16, 720, audio).")
    if get_last_video_result_source() == "uri":
        uri = get_last_video_result_uri() or "gs://..."
        await update.message.reply_text(f"Источник результата: video URI ({uri}).")
    await update.message.reply_text(
        "Выберите первый шаг кнопкой.",
        reply_markup=first_step_menu_markup(),
    )


async def _handle_syntax_fallback_pending_video_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """
    Обрабатывает ответ пользователя после ``SyntaxFallbackPendingConfirmation`` в команде ``/video``.
    """
    pending = session.get("syntax_fallback_pending")
    if not isinstance(pending, dict) or pending.get("source") != "video_command":
        return False
    if update.message is None:
        return True
    choice = syntax_fallback_parse_user_confirmation(text)
    if choice is None:
        await update.message.reply_text(
            "Не поняла ответ. Напишите «да» (запустить Syntax) или «нет» (переписать промпт)."
        )
        return True
    duration_sec = int(pending.get("duration_for_caption") or pending.get("duration_sec") or 6)
    orig_prompt = str(pending.get("original_prompt") or pending.get("prompt") or "").strip()
    err_text = str(pending.get("vertex_error_message") or "")
    if choice is False:
        session.pop("syntax_fallback_pending", None)
        try:
            safe_prompt = await _regenerate_video_prompt_from_text(orig_prompt, err_text)
            await update.message.reply_text(
                "Ок, Syntax не запускаю. Кандидат prompt после модерации:\n"
                f"{safe_prompt}\n\n"
                "Отредактируйте при необходимости и запустите снова:\n"
                f"/video {duration_sec} <ваш prompt>"
            )
        except Exception as rewrite_err:
            logger.exception("syntax decline: video safety rewrite failed")
            await update.message.reply_text(
                "Не удалось подготовить prompt после отказа от Syntax.\n"
                f"Ошибка: {_user_error_message(rewrite_err, domain='video')}"
            )
        return True
    payload = dict(pending)
    session.pop("syntax_fallback_pending", None)
    await update.message.reply_text("Запускаю резервный канал Syntax по вашему подтверждению…")
    try:
        video_bytes = await run_syntax_fallback_from_session_payload(payload)
    except Exception as fb_err:
        logger.exception("syntax fallback execution failed (/video)")
        await update.message.reply_text(
            f"Ошибка при запуске Syntax fallback: {_user_error_message(fb_err, domain='video')}"
        )
        try:
            safe_prompt = await _regenerate_video_prompt_from_text(orig_prompt, err_text)
            await update.message.reply_text(
                "Переходим к переписыванию промпта. Кандидат:\n"
                f"{safe_prompt}\n\n"
                f"/video {duration_sec} <ваш prompt>"
            )
        except Exception as rewrite_err:
            logger.exception("syntax fail: video safety rewrite failed")
            await update.message.reply_text(
                f"И переписать промпт не вышло: {_user_error_message(rewrite_err, domain='video')}"
            )
        return True
    if not video_bytes:
        await update.message.reply_text(
            "Syntax fallback не вернул видео. Переходим к переписыванию промпта."
        )
        try:
            safe_prompt = await _regenerate_video_prompt_from_text(orig_prompt, err_text)
            await update.message.reply_text(
                f"{safe_prompt}\n\n"
                f"/video {duration_sec} <ваш prompt>"
            )
        except Exception as rewrite_err:
            logger.exception("syntax empty: video safety rewrite failed")
            await update.message.reply_text(
                f"Не удалось подготовить prompt: {_user_error_message(rewrite_err, domain='video')}"
            )
        return True
    vb = io.BytesIO(video_bytes)
    vb.name = "video.mp4"
    await update.message.reply_video(
        video=vb,
        caption=f"Видео готово ({duration_sec} сек, 9:16, 720, audio). Источник: Syntax fallback.",
    )
    return True


async def _handle_video_provider_choice_pending_video_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    session: dict,
    text: str,
) -> bool:
    """
    Handles user choice after automatic Syntax attempt failed in `/video`.
    """
    pending = session.get("video_provider_choice_pending")
    if not isinstance(pending, dict) or pending.get("source") != "video_command":
        return False
    if update.message is None:
        return True
    choice = video_provider_choice_parse_user_selection(text)
    if choice is None:
        await update.message.reply_text(
            "Не поняла ответ. Напишите `Syntax`, чтобы повторить через Syntax, или `Vertex`, чтобы попробовать резервный Vertex."
        )
        return True
    duration_sec = int(pending.get("duration_for_caption") or pending.get("duration_sec") or 6)
    if choice == "syntax":
        await update.message.reply_text("Повторяю генерацию через Syntax…")
        try:
            video_bytes = await run_syntax_fallback_from_session_payload(pending)
        except Exception as fb_err:
            logger.exception("syntax retry execution failed (/video)")
            await update.message.reply_text(
                f"Ошибка при повторе Syntax: {_user_error_message(fb_err, domain='video')}"
            )
            return True
        if not video_bytes:
            await update.message.reply_text(
                "Syntax снова не вернул видео. Можно написать `Syntax` для ещё одной попытки или `Vertex` для резервного канала."
            )
            return True
        session.pop("video_provider_choice_pending", None)
        vb = io.BytesIO(video_bytes)
        vb.name = "video.mp4"
        await update.message.reply_video(
            video=vb,
            caption=f"Видео готово ({duration_sec} сек, 9:16, 720, audio). Источник: Syntax.",
        )
        return True

    await update.message.reply_text("Пробую резервный канал Vertex/Veo…")
    try:
        video_bytes = await run_vertex_fallback_from_session_payload(pending)
    except Exception as err:
        logger.exception("vertex fallback execution failed (/video)")
        await update.message.reply_text(
            f"Ошибка резервного Vertex: {_user_error_message(err, domain='video')}"
        )
        return True
    session.pop("video_provider_choice_pending", None)
    vertex_duration_sec = int(pending.get("vertex_duration_sec") or pending.get("duration_sec") or duration_sec)
    vb = io.BytesIO(video_bytes)
    vb.name = "video.mp4"
    await update.message.reply_video(
        video=vb,
        caption=f"Видео готово ({vertex_duration_sec} сек, 9:16, 720, audio). Источник: Vertex fallback.",
    )
    return True


async def fix_stress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда ``/fix_stress``: приводит реплики veo3 к формату без ударений/пунктуации.
    """
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    session = SESSIONS.get(chat_id)
    if not session:
        await update.message.reply_text("Нет активного сценария. Сначала запусти /generate.")
        return

    scenes = session.get("scenes_plan")
    if not isinstance(scenes, list) or not scenes:
        await update.message.reply_text("Пока нет сцен для правки ударений.")
        return

    fixed = apply_veo3_lowercase_format(scenes)
    session["scenes_plan"] = fixed
    session["scenes_text"] = build_human_readable_plan(fixed)

    # Если уже есть production, синхронизируем text и speech_by_lang['ru'] по номеру сцены.
    production = session.get("production")
    if isinstance(production, dict):
        plan = production.get("json_plan")
        if isinstance(plan, list):
            text_by_scene = {int(s.get("scene", i + 1)): str(s.get("text", "")) for i, s in enumerate(fixed)}
            for i, item in enumerate(plan):
                if not isinstance(item, dict):
                    continue
                scene_no = int(item.get("scene", i + 1))
                new_text = text_by_scene.get(scene_no)
                if isinstance(new_text, str) and new_text.strip():
                    item["text"] = new_text
                    speech_map = item.get("speech_by_lang")
                    if isinstance(speech_map, dict):
                        speech_map["ru"] = new_text
                    tts = item.get("tts")
                    if isinstance(tts, dict):
                        tts["text_ru"] = new_text

    await update.message.reply_text("Убрала ударения и нормализовала [пауза] в текущих сценах.")
    await _reply_text_chunked(update, session["scenes_text"])
