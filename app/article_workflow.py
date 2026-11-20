"""Owner approval loop: five topics, article, two images, publish, story handoff."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import logging
from pathlib import Path
import secrets
from time import monotonic

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop

from . import article_store as store
from . import article_generation as generation
from .article_publisher import NotSubmitted, publish

logger = logging.getLogger("bot.articles")
_LOCK = asyncio.Lock()


def owner(update):
    config = store.snapshot()
    user, chat = update.effective_user, update.effective_chat
    return bool(config.get("enabled") and user and chat and user.id == config.get("owner_id")
                and chat.id == config.get("owner_id") and chat.type == "private")


def keyboard(row, buttons):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f'ar:{row["id"]}:{row["revision"]}:{action}')]
                                 for label, action in buttons])


def topic_fields(row, topic):
    if row.get("topic") == topic:
        return {"topic": topic}
    versions = list(row.get("versions", []))
    if row.get("text") or row.get("images"):
        versions.append({key: row[key] for key in ("topic", "text", "teaser", "image_prompt", "images") if key in row})
    return {"topic": topic, "versions": versions, "text": "", "teaser": "", "image_prompt": "", "images": {},
            "feedback": "", "image_feedback": "", "square_crop": 50, "text_approved": False, "images_approved": False}


def new_image_version(row, *, feedback=None):
    previous = {key: row[key] for key in ("images", "image_version", "square_crop", "image_feedback", "image_reference_manifest") if key in row}
    return {"images": {}, "square_crop": 50, "images_approved": False, "image_reference_manifest": [],
            "image_version": row.get("image_version", 0) + 1,
            "image_feedback": row.get("image_feedback", "") if feedback is None else feedback,
            "versions": row["versions"] + [previous]}


async def show(bot, chat_id, row):
    started = monotonic()
    state = row["state"]
    target = store.target_for(row)
    destination = target.get("title") or target.get("username") or str(target["channel_id"])
    test_notice = f'Тест для канала «{destination}». Стиль «У бабушки дома».\n\n' if row.get("test_mode") else ""
    buttons = []
    if state == "topics":
        text = ('Выберите тему:\n\n' if row.get("test_mode") else 'Статья для «У бабушки дома». Выберите тему:\n\n') + '\n\n'.join(
            f'{n}. {t["title"]}\n{t["angle"]}' for n, t in enumerate(row["topics"], 1))
        buttons = [(str(n) + '. ' + t["title"], f"choose{n-1}") for n, t in enumerate(row["topics"], 1)]
        buttons += [("Другие пять тем", "more"), ("Своя тема / пожелания", "custom")]
        if row.get("text") and row.get("topic"):
            buttons += [("Вернуться к сохранённой статье", "resume")]
    elif state == "topic":
        saved_text = bool(row.get("text"))
        next_step = "После согласования покажу сохранённый текст." if saved_text else "После согласования подготовлю текст через GPT."
        text = f'Тема: {row["topic"]["title"]}\n\n{row["topic"].get("angle", "")}\n\n{next_step}'
        label = "Тему согласовываю, показать текст" if saved_text else "Тему согласовываю, написать статью"
        buttons = [(label, "draft"), ("Изменить тему", "custom")]
    elif state == "text":
        await bot.send_message(chat_id=chat_id, text=row["text"])
        text = f'Короткий анонс для сториз:\n{row["teaser"]}\n\nСогласуйте текст и анонс. Затем покажу квадрат для Telegram и вертикальный вариант для сториз.'
        saved_images = all(Path(row.get("images", {}).get(k, "")).is_file() for k in ("square", "vertical"))
        label = "Текст согласован, показать картинки" if saved_images else "Текст согласован, сделать картинки"
        buttons = [(label, "images"), ("Исправить текст или анонс", "edit")]
    elif state in {"images", "ready"}:
        if state == "images":
            for key, label in (("square", "Квадратная картинка 1:1 для Telegram"),
                               ("vertical", "Вертикальная картинка 9:16 для сториз")):
                with Path(row["images"][key]).open("rb") as photo:
                    await bot.send_photo(chat_id=chat_id, photo=photo, caption=label)
            text = "Проверьте обе картинки: кот, тема статьи и композиция. Можно внести правки."
            buttons = [("Обе картинки согласованы", "approve")]
        else:
            text = (f'Готово к публикации в канале «{destination}»\n'
                    f'Тема: {row["topic"]["title"]}\n\nПубликация содержит согласованный текст и квадратную картинку 1:1. '
                    'После неё пришлю вертикальную картинку, ссылку и короткий анонс тремя отдельными сообщениями.')
            publish_label = f"Опубликовать в {destination}" if row.get("test_mode") else "Опубликовать статью в канале"
            buttons = [(publish_label, "publish"), ("Исправить текст", "edit")]
        buttons += [("Перегенерировать картинку", "regenerate"), ("Исправить изображение", "imageedit"),
                    ("Показать референсы", "references")]
        if row.get("square_crop", 50) > 0:
            buttons += [("Квадрат: сдвинуть выше", "crop_up")]
        if row.get("square_crop", 50) < 100:
            buttons += [("Квадрат: сдвинуть ниже", "crop_down")]
    elif state == "failed":
        text = row.get("error", "Не удалось завершить этап.") + "\nСохранённые материалы не потеряны."
        buttons = [("Повторить незавершённый этап", "retry")]
    elif state in {"publishing", "publication_uncertain"}:
        text = "Публикация требует проверки в канале. Повторно пост не отправляю."
        buttons = [("Проверить публикацию", "check")]
    elif state == "published":
        text = "Статья опубликована: " + row["url"]
        buttons = [("Прислать комплект для сториз ещё раз", "handoff")]
    elif state == "cancelled":
        text = "Статья отменена. Можно вернуться к сохранённому шагу кнопкой ниже. Следующие темы придут по расписанию."
    else:
        text = "Этот этап сейчас выполняется. Результат пришлю сюда."
    if store.previous_state(row) is not None:
        buttons += [("Предыдущий шаг", "back")]
    if state in {"topics", "topic", "text", "images", "ready", "failed"}:
        buttons += [("Отменить эту статью", "cancel")]
    await bot.send_message(chat_id=chat_id, text=test_notice + text, reply_markup=keyboard(row, buttons) if buttons else None,
                           disable_web_page_preview=True)
    store.checkpoint(row["id"], shown_revision=row["revision"])
    logger.info("article.show issue=%s state=%s revision=%s elapsed=%.2fs", row["id"], state, row["revision"], monotonic() - started)


async def deliver_handoff(bot, chat_id, row, *, again=False):
    delivered = {} if again else dict(row.get("delivery", {}))
    for key in ("photo", "url", "teaser"):
        if key in delivered:
            continue
        if key == "photo":
            with Path(row["images"]["vertical"]).open("rb") as photo:
                message = await bot.send_photo(chat_id=chat_id, photo=photo)
        else:
            message = await bot.send_message(chat_id=chat_id, text=row[key], disable_web_page_preview=True)
        delivered[key] = message.message_id
        store.checkpoint(row["id"], delivery=delivered)


async def generate(bot, chat_id, row, *, show_result=True):
    op = row["operation"]
    started = monotonic()
    logger.info("article.generation_started issue=%s operation=%s", row["id"], op)
    try:
        config = store.snapshot()
        if op == "topics":
            values = await asyncio.to_thread(generation.topics, config, row.get("feedback", ""))
            previous = row.get("past_topic_titles", []) + [t["title"] for t in row.get("topics", [])]
            row = store.change(row["id"], row["revision"], allowed={"working"}, state="topics", topics=values,
                               past_topic_titles=previous)
        elif op == "draft":
            result = await asyncio.to_thread(generation.draft, config, row)
            versions = row["versions"] + ([{k: row[k] for k in ("text", "teaser", "images") if k in row}] if row.get("text") else [])
            row = store.change(row["id"], row["revision"], allowed={"working"}, state="text", versions=versions,
                               images={}, text_approved=False, images_approved=False, **result)
        elif op == "images":
            images = await asyncio.to_thread(generation.generate_missing_images, config, row)
            row = store.change(row["id"], row["revision"], allowed={"working"}, state="images", images=images)
        else:
            raise ValueError("Неизвестный этап статьи.")
    except Exception as exc:
        logger.warning("article.generation_failed issue=%s operation=%s type=%s", row["id"], op, type(exc).__name__)
        # Raw upstream errors can include credentials; never echo them to Telegram.
        row = store.change(row["id"], row["revision"], allowed={"working"}, state="failed",
                           error="Не удалось завершить подготовку. Можно повторить только этот этап.")
    logger.info("article.generation_finished issue=%s operation=%s state=%s elapsed=%.2fs",
                row["id"], op, row["state"], monotonic() - started)
    if show_result:
        await show(bot, chat_id, row)


async def request_edit(bot, chat_id, row, kind):
    prompts = {"custom": "Напишите тему статьи или пожелания к теме ответом на это сообщение.",
               "edit": "Какие изменения внести в текст или короткий анонс? Ответьте на это сообщение.",
               "imageedit": "Что изменить на изображении? Подготовлю новый вертикальный оригинал и квадрат из него. Текст статьи сохранится. Ответьте на это сообщение."}
    msg = await bot.send_message(chat_id=chat_id, text=prompts[kind], reply_markup=ForceReply(selective=True))
    store.remember_reply(msg.message_id, row, kind)


async def command(update, context):
    if not owner(update):
        return
    row = store.current()
    if row:
        await show(context.bot, update.effective_chat.id, row)
    else:
        from zoneinfo import ZoneInfo
        due = datetime.fromisoformat(store.snapshot()["next_run_at"]).astimezone(ZoneInfo("Europe/Moscow"))
        await update.effective_message.reply_text(f'Первые пять тем: {due:%d.%m.%Y в %H:%M} МСК. Затем каждые три дня. '
                                                  'Тема, текст и картинки согласовываются отдельно.')


async def callback(update, context):
    query = update.callback_query
    if not owner(update):
        await query.answer()
        return
    await query.answer()
    if _LOCK.locked():
        await query.message.reply_text("Сейчас выполняется этап статьи. Подождите результат.")
        return
    async with _LOCK:
        try:
            _, iid, revision, action = query.data.split(":")
            row = store.issue(iid)
            if row["revision"] != int(revision):
                raise ValueError("Эта кнопка относится к предыдущей версии. Откройте /articles.")
            chat_id = update.effective_chat.id
            if action in {"custom", "edit", "imageedit"}:
                valid = {"custom": {"topics", "topic"}, "edit": {"text", "ready"},
                         "imageedit": {"images", "ready"}}
                if row["state"] not in valid[action]:
                    raise ValueError("Этот этап уже изменился. Откройте /articles.")
                await request_edit(context.bot, chat_id, row, action)
                return
            if action == "references":
                if row["state"] not in {"images", "ready"}:
                    raise ValueError("Этот шаг уже изменился.")
                for n, path in enumerate(store.snapshot()["reference_paths"], 1):
                    caption = "Оригинальный референс бабушки из проекта" if n == 1 else "Оригинальный референс Тимоши из проекта"
                    with Path(path).open("rb") as photo:
                        await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
                return
            if action.startswith("choose"):
                index = int(action.removeprefix("choose"))
                if not 0 <= index < 5:
                    raise ValueError("Неверный номер темы.")
                row = store.change(iid, row["revision"], allowed={"topics"}, state="topic",
                                   **topic_fields(row, row["topics"][index]))
            elif action == "resume":
                if not row.get("text") or not row.get("topic"):
                    raise ValueError("Нет сохранённой статьи.")
                row = store.change(iid, row["revision"], allowed={"topics"}, state="topic")
            elif action in {"more", "draft", "images", "retry"}:
                op = {"more": "topics", "draft": "draft", "images": "images", "retry": row.get("operation")}[action]
                valid = {"more": {"topics"}, "draft": {"topic"}, "images": {"text"}, "retry": {"failed"}}
                if action == "draft" and row["state"] == "topic" and row.get("text"):
                    row = store.change(iid, row["revision"], allowed={"topic"}, state="text")
                    await show(context.bot, chat_id, row)
                    return
                if (action == "images" and row["state"] == "text"
                        and all(Path(row.get("images", {}).get(k, "")).is_file() for k in ("square", "vertical"))):
                    row = store.change(iid, row["revision"], allowed={"text"}, state="images", text_approved=True, images_approved=False)
                    await show(context.bot, chat_id, row)
                    return
                extra = {"image_version": row.get("image_version", 0) + (0 if row.get("images") else 1),
                         "text_approved": True} if action == "images" else {}
                row = store.change(iid, row["revision"], allowed=valid[action], state="working", operation=op, **extra)
                progress = {"topics": "Подбираю пять новых тем: четыре кулинарные и одну бытовую.",
                            "draft": "Пишу статью по согласованной теме. Пришлю текст и короткий анонс для правок.",
                            "images": "Рисую один вертикальный оригинал с бабушкой и Тимошей по вашим референсам. Квадрат для Telegram вырежу из него без второй генерации."}
                await query.message.reply_text(progress[op])
                await generate(context.bot, chat_id, row)
                return
            elif action == "approve":
                row = store.change(iid, row["revision"], allowed={"images"}, state="ready", images_approved=True)
            elif action == "regenerate":
                row = store.change(iid, row["revision"], allowed={"images", "ready"}, state="working", operation="images",
                                   **new_image_version(row))
                await query.message.reply_text("Создаю новый вариант по оригинальным референсам. Предыдущая картинка и текст сохранены. Затем покажу вертикаль и квадрат для согласования.")
                await generate(context.bot, chat_id, row)
                return
            elif action in {"crop_up", "crop_down"}:
                if row["state"] not in {"images", "ready"}:
                    raise ValueError("Кадрирование недоступно на этом шаге.")
                position = max(0, min(100, row.get("square_crop", 50) + (-10 if action == "crop_up" else 10)))
                images = await asyncio.to_thread(generation.crop_square, row, position)
                row = store.change(iid, row["revision"], allowed={"images", "ready"}, state="images",
                                   images=images, square_crop=position, images_approved=False)
            elif action in {"publish", "check"}:
                if action == "publish":
                    if not row.get("text_approved") or not row.get("images_approved"):
                        raise ValueError("Сначала согласуйте текст и обе картинки.")
                    row = store.change(iid, row["revision"], allowed={"ready"}, state="publishing",
                                       publish_random_id=secrets.randbits(63),
                                       publish_started_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat())
                elif row["state"] != "publication_uncertain":
                    raise ValueError("Дождитесь завершения публикации.")
                try:
                    mid = await publish(store.snapshot(), row, check_only=action == "check")
                except NotSubmitted:
                    row = store.change(iid, row["revision"], allowed={"publishing"}, state="ready")
                    await query.message.reply_text("Пост не отправлен: Telegram сейчас недоступен или занят. Можно повторить публикацию позже.")
                    await show(context.bot, chat_id, row)
                    return
                except Exception as exc:
                    logger.warning("article.publish_unconfirmed issue=%s type=%s", iid, type(exc).__name__)
                    mid = None
                if mid:
                    row = store.change(iid, row["revision"], allowed={"publishing", "publication_uncertain"}, state="published",
                                       channel_message_id=mid, url=store.post_url(row, mid))
                    await deliver_handoff(context.bot, chat_id, row)
                else:
                    row = store.change(iid, row["revision"], allowed={"publishing", "publication_uncertain"}, state="publication_uncertain")
            elif action == "handoff" and row["state"] == "published":
                await deliver_handoff(context.bot, chat_id, row, again=True)
                return
            elif action == "back":
                row = store.go_back(iid, row["revision"])
            elif action == "cancel":
                row = store.change(iid, row["revision"], allowed={"topics", "topic", "text", "images", "ready", "failed"},
                                   state="cancelled", cancelled_from=row["state"])
            else:
                raise ValueError("Этот шаг недоступен. Откройте /articles.")
            await show(context.bot, chat_id, row)
        except (ValueError, KeyError, IndexError):
            await query.message.reply_text("Этот шаг уже изменился или недоступен. Откройте текущую статью: /articles.")


async def reply(update, context):
    if not owner(update):
        return
    message = update.effective_message
    original = message.reply_to_message
    ref = store.snapshot()["replies"].get(str(original.message_id)) if original else None
    if not ref:
        return
    # Stop the recipe handler for this reply, but keep polling other updates
    # while GPT or the image provider is working.
    context.application.create_task(apply_reply(update, context, ref))
    raise ApplicationHandlerStop


async def apply_reply(update, context, ref):
    message = update.effective_message
    if _LOCK.locked():
        await message.reply_text("Подождите завершения текущего этапа статьи, затем повторите правку.")
        return
    async with _LOCK:
        try:
            row = store.issue(ref["id"])
            if row["revision"] != ref["revision"]:
                raise ValueError("Ответ относится к старой версии.")
            if ref["kind"] not in {"custom", "edit", "imageedit"}:
                raise ValueError("Откройте актуальную кнопку изменения.")
            feedback = (message.text or "").strip()
            if not feedback or generation.utf16_length(feedback) > 3500:
                raise ValueError("Напишите правку длиной до 3500 символов.")
            if ref["kind"] == "custom":
                row = store.change(row["id"], row["revision"], allowed={"topics", "topic"}, state="topic",
                                   **topic_fields(row, {"title": feedback.split("\n")[0][:100], "angle": feedback}))
                await show(context.bot, update.effective_chat.id, row)
            else:
                image_edit = ref["kind"] == "imageedit"
                fields = (new_image_version(row, feedback=feedback) if image_edit else
                          {"feedback": feedback, "text_approved": False, "images_approved": False})
                row = store.change(row["id"], row["revision"], allowed={"images", "ready"} if image_edit else {"text", "ready"},
                                   state="working", operation="images" if image_edit else "draft", **fields)
                await message.reply_text("Вношу правки. Покажу результат для нового согласования.")
                await generate(context.bot, update.effective_chat.id, row)
        except (ValueError, KeyError):
            await message.reply_text("Эта правка недоступна или относится к старой версии. Откройте /articles и нажмите кнопку изменения.")


def owner_busy(chat_id):
    from .advertising_queue import chat_lock, session_is_busy
    from .state import SESSIONS
    return chat_lock(chat_id).locked() or session_is_busy(SESSIONS.get(chat_id))


def advertising_has_priority(now=None):
    from sqlalchemy import select
    from .advertising_queue import SessionLocal, aware
    from .models import AdvertisingProduct, AdvertisingQueueSchedule
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        schedule = db.get(AdvertisingQueueSchedule, 1)
        if not schedule or not schedule.enabled:
            return False
        if db.scalar(select(AdvertisingProduct.id).where(
                AdvertisingProduct.status.in_(["preparing", "ready", "offered", "failed"])).limit(1)):
            return True
        return now >= aware(schedule.next_run_at) and db.scalar(select(AdvertisingProduct.id).where(
            AdvertisingProduct.status == "pending").limit(1)) is not None


async def show_when_idle(bot, chat_id, row):
    from .advertising_queue import chat_lock, session_is_busy
    from .state import SESSIONS
    if chat_lock(chat_id).locked():
        return
    async with chat_lock(chat_id):
        if session_is_busy(SESSIONS.get(chat_id)):
            return
        await show(bot, chat_id, row)


async def tick(bot):
    if _LOCK.locked():
        return
    async with _LOCK:
        config = store.snapshot()
        if not config.get("enabled") or owner_busy(config["owner_id"]):
            return
        # Complete owner handoffs even when a later issue has already become due.
        for row in config["issues"].values():
            if row["state"] == "published" and len(row.get("delivery", {})) < 3:
                await deliver_handoff(bot, config["owner_id"], row)
        # Claim under the same chat lock as advertising delivery. Existing ad
        # work wins; once claimed, this article reserves the editorial slot.
        from .advertising_queue import chat_lock
        row = None
        if not store.blocks_advertising(config["owner_id"]):
            async with chat_lock(config["owner_id"]):
                if not advertising_has_priority():
                    row = store.claim_due()
        if row:
            if row["state"] == "working":
                await generate(bot, config["owner_id"], row, show_result=False)
            await show_when_idle(bot, config["owner_id"], store.issue(row["id"]))
        else:
            row = store.current()
            if row and row["state"] not in store.TERMINAL | {"working", "publishing"} and row.get("shown_revision") != row["revision"]:
                await show_when_idle(bot, config["owner_id"], row)


async def run_queue(bot):
    store.recover_interrupted()
    logger.info("articles.started enabled=%s cadence=3-days timezone=Europe/Moscow", store.snapshot().get("enabled", False))
    while True:
        try:
            await tick(bot)
        except Exception as exc:
            logger.warning("articles.tick_failed type=%s", type(exc).__name__)
        await asyncio.sleep(60)
