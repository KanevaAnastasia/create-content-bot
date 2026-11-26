"""Prepare one advertising product every two days, deliver only to an idle owner."""
from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .db import SessionLocal
from .models import AdvertisingProduct, AdvertisingQueueSchedule, BotUser
from .quiet_mode import QUIET_MODE_OWNER_USERNAME
from .advertising_product_files import photo_path

logger = logging.getLogger("bot.advertising_queue")
PRODUCT_KEY = "advertising_queue_product_id"
PHOTOS_KEY = "advertising_product_paths"
_LOCKS: dict[int, asyncio.Lock] = {}


def chat_lock(chat_id: int) -> asyncio.Lock:
    return _LOCKS.setdefault(chat_id, asyncio.Lock())


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def owner_chat_id():
    with SessionLocal() as db:
        return db.scalar(select(BotUser.telegram_id).where(
            BotUser.normalized_username == QUIET_MODE_OWNER_USERNAME,
            BotUser.role == "admin", BotUser.is_active.is_(True)))


def session_is_busy(session: dict | None) -> bool:
    if not session:
        return False
    state = str(session.get("state") or "")
    if state == "waiting_inner_boss_scene_workflow":
        return (session.get("inner_boss_scene_workflow") or {}).get("stage") != "done"
    if state == "ready_for_production":
        # Scene generation finishes before the external final worker delivers
        # videos and captions. Do not overwrite that run while rendering.
        return not bool(session.get("social_descriptions_sent"))
    return bool(state)


def claim_due_product(now=None):
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        schedule = db.get(AdvertisingQueueSchedule, 1, with_for_update=True)
        if not schedule or not schedule.enabled:
            return None
        active = db.scalars(select(AdvertisingProduct).where(
            AdvertisingProduct.status.in_(["preparing", "ready", "offered", "failed"])
        ).order_by(AdvertisingProduct.position, AdvertisingProduct.created_at, AdvertisingProduct.id)).first()
        if active:
            if active.status != "preparing" or (active.claimed_at and now - aware(active.claimed_at) < timedelta(minutes=30)):
                return None
            active.claimed_at = now
            db.commit()
            return active.id
        if now < aware(schedule.next_run_at):
            return None
        product = db.scalars(select(AdvertisingProduct).where(AdvertisingProduct.status == "pending")
                             .order_by(AdvertisingProduct.position, AdvertisingProduct.created_at, AdvertisingProduct.id)
                             .with_for_update()).first()
        # Catch up once after downtime; keep the original Moscow calendar,
        # without a burst of missed advertising runs.
        next_at = aware(schedule.next_run_at)
        while next_at <= now:
            next_at += timedelta(days=2)
        schedule.next_run_at = next_at
        if product is None:
            db.commit()
            return None
        product.status = "preparing"
        product.claimed_at = now
        db.commit()
        return product.id


def _save_prepared(product_id, payload, *, status="preparing"):
    with SessionLocal() as db:
        row = db.get(AdvertisingProduct, product_id)
        row.prepared = copy.deepcopy(payload)
        row.status = status
        row.error = ""
        row.claimed_at = datetime.now(timezone.utc)
        db.commit()


def prepare_product(product_id):
    from .advertising_generation import (
        analyze_advertising_product, generate_advertising_campaigns, generate_advertising_scene_plan,
        generate_advertising_post_package, advertising_editorial_rules,
    )
    with SessionLocal() as db:
        product = db.get(AdvertisingProduct, product_id)
        title, comment, images = product.title, product.comment, list(product.images)
        payload = copy.deepcopy(product.prepared or {})
        product_format = product.format
    if not images:
        raise ValueError("У товара нет фотографий.")
    for path in images:
        photo_path(path)
    source = f"Товар: {title}\nКомментарий владельца: {comment or 'не указан'}"
    if not payload.get("brief"):
        brief = analyze_advertising_product(photo_path(images[0]).read_bytes(), source_text=source)
        brief["product_name"] = title
        payload.update(brief=brief, source=source)
        _save_prepared(product_id, payload)
    # Apply current speech rules to resumed preparations too.
    payload["brief"]["owner_comment"] = comment
    payload["brief"]["speech_min_chars"] = 101
    payload["brief"].pop("speech_max_chars", None)
    _save_prepared(product_id, payload)
    if not payload.get("campaigns"):
        from .llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, extract_json_object, gemini_generate_sync
        import json
        if not payload.get("sales_analysis"):
            analysis = extract_json_object(gemini_generate_sync(
                advertising_editorial_rules(payload["brief"], source)
                + "Разбери товар для продающей рекламы ВК с бабушкой и рыжим котом. "
                "Используй только факты из карточки и комментария. Не обращайся к закрытым сайтам. "
                "Отдели подтверждённые свойства от гипотез о потребностях аудитории. "
                "Нельзя выдумывать цену, скидку, состав, эффективность, отзывы и исследования. "
                "Верни JSON: audience_hypotheses (список), supported_benefits (список), "
                "objections (список), demonstration_ideas (список), selling_description (текст), "
                "unknown_facts (список). Тон живой, домашний, без канцелярита и агрессивных обещаний.\n"
                + json.dumps({"source": source, "product": payload["brief"]}, ensure_ascii=False),
                model_profile=GEMINI_MODEL_PROFILE_CREATIVE,
            ))
            if not str(analysis.get("selling_description") or "").strip():
                raise ValueError("Не удалось подготовить продающее описание товара.")
            payload["sales_analysis"] = analysis
            payload["brief"]["sales_analysis"] = analysis
            _save_prepared(product_id, payload)
        result = generate_advertising_campaigns(
            source_text=source + "\nПодбери сильные продающие идеи для аудитории ВК бабушки: "
            "понятная бытовая польза, живые фразы, наглядное применение, мягкий призыв. "
            "Не придумывай отзывы, исследования, скидки, характеристики или гарантии результата.",
            product_brief=payload["brief"],
        )
        payload["campaigns"] = result["campaigns"]
        _save_prepared(product_id, payload)
    for campaign in payload["campaigns"]:
        if product_format == "post":
            if not campaign.get("prepared_post_package"):
                campaign["prepared_post_package"] = generate_advertising_post_package(
                    source_text=source, product_brief=payload["brief"], campaign=campaign)
                _save_prepared(product_id, payload)
        elif not campaign.get("prepared_scene_plan"):
            campaign["prepared_scene_plan"] = generate_advertising_scene_plan(
                source_text=source, product_brief=payload["brief"], selected_campaign=campaign)
            _save_prepared(product_id, payload)
    _save_prepared(product_id, payload, status="ready")


async def finish_product_if_complete(bot, chat_id, session):
    """Complete on final approval, independently of publication selection."""
    product_id = session.get(PRODUCT_KEY)
    if not product_id:
        return
    done = bool(session.get("social_descriptions_sent")) or (
        not session.get("state") and len(session.get("advertising_post_image_paths") or []) == 1)
    if not done:
        return
    with SessionLocal() as db:
        product = db.get(AdvertisingProduct, uuid.UUID(product_id), with_for_update=True)
        if not product or product.status not in {"offered", "completed"}:
            return
        product.status = "completed"
        product.completed_at = product.completed_at or datetime.now(timezone.utc)
        unfinished = db.scalar(select(AdvertisingProduct.id).where(
            AdvertisingProduct.id != product.id, AdvertisingProduct.status != "completed").limit(1))
        notify = unfinished is None and not product.refill_notified
        db.commit()
    if notify:
        await bot.send_message(chat_id=chat_id, text="Пополните базу")
        with SessionLocal() as db:
            product = db.get(AdvertisingProduct, uuid.UUID(product_id))
            product.refill_notified = True
            db.commit()


async def deliver_ready_product(bot, chat_id):
    from .state import SESSIONS
    from .session_store import save_session
    from .advertising_generation import STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE
    from .handlers.handlers_state_start import _advertising_campaign_choice_markup

    lock = chat_lock(chat_id)
    if lock.locked():
        return
    async with lock:
        session = SESSIONS.get(chat_id) or {}
        await finish_product_if_complete(bot, chat_id, session)
        from .article_store import blocks_advertising
        if blocks_advertising(chat_id):
            return
        with SessionLocal() as db:
            schedule = db.get(AdvertisingQueueSchedule, 1)
            if not schedule or not schedule.enabled:
                return
            abandoned = db.scalars(select(AdvertisingProduct).where(AdvertisingProduct.status == "offered")).first()
            if abandoned and session.get(PRODUCT_KEY) != str(abandoned.id):
                # Starting over/cancelling must not silently consume a product.
                abandoned.status = "ready"
                abandoned.prepared = {**abandoned.prepared, "delivery_index": 0}
                db.commit()
            row = db.scalars(select(AdvertisingProduct).where(AdvertisingProduct.status == "ready")
                             .order_by(AdvertisingProduct.position, AdvertisingProduct.created_at)).first()
            if not row:
                return
            product_id, title, images, payload = str(row.id), row.title, list(row.images), copy.deepcopy(row.prepared)
            product_format = row.format
        resuming = session.get(PRODUCT_KEY) == product_id
        if resuming and session.get("state") != STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE:
            _save_prepared(uuid.UUID(product_id), payload, status="offered")
            return
        if session_is_busy(session) and not resuming:
            return
        if not resuming:
            # Fresh advertising session, never inherit a previous run's media,
            # series, project-specific flags or publication approvals.
            session = {
                "state": STATE_WAITING_ADVERTISING_CAMPAIGN_CHOICE,
                "selected_project_key": "grandma_kitchen", "generation_mode": "advertising",
                "target_languages": ["ru"], "sponsored_content": True,
                "advertising_format": product_format,
                "advertising_source_text": payload["source"], "user_input": payload["source"],
                "advertising_product_brief": payload["brief"], "advertising_product_path": images[0],
                PHOTOS_KEY: images, PRODUCT_KEY: product_id,
                "advertising_campaigns": payload["campaigns"],
            }
            save_session(chat_id, session)  # Fail before sending if persistence is unavailable.
            SESSIONS[chat_id] = session
        messages = [f"Реклама по очереди: {title}\nВсе {len(images)} фото товара уже добавлены в рекламный режим. "
                    f"Тип: {'Пост' if product_format == 'post' else 'Клип'}. Подготовила пять вариантов; выберите подходящий."]
        description = str((payload.get("sales_analysis") or {}).get("selling_description") or "")
        if description:
            messages[0] += "\n\nОписание товара:\n" + description
        for index, campaign in enumerate(payload["campaigns"], 1):
            creative = ((campaign.get("prepared_post_package") or {}).get("post_text") if product_format == "post"
                        else (campaign.get("prepared_scene_plan") or {}).get("text_plan"))
            if not creative:
                creative = "\n".join(str(campaign.get(key) or "") for key in ("hook", "product_role", "cta"))
            messages.append(f"Вариант {index}. {campaign['title']}\n{campaign['idea']}\n\n" + creative)
        # Store a delivery cursor: retries don't regenerate or resend confirmed chunks.
        chunks = [text[start:start + 3500] for text in messages for start in range(0, len(text), 3500)]
        for index in range(int(payload.get("delivery_index", 0)), len(chunks)):
            await bot.send_message(chat_id=chat_id, text=chunks[index])
            payload["delivery_index"] = index + 1
            _save_prepared(uuid.UUID(product_id), payload, status="ready")
        await bot.send_message(chat_id=chat_id, text="Выберите сценарий 1–5 или запросите другие варианты.",
                               reply_markup=_advertising_campaign_choice_markup())
        _save_prepared(uuid.UUID(product_id), payload, status="offered")


async def tick(bot):
    chat_id = owner_chat_id()
    if chat_id is None:
        return
    await deliver_ready_product(bot, chat_id)
    from .article_store import blocks_advertising
    if blocks_advertising(chat_id):
        return
    product_id = claim_due_product()
    if product_id:
        try:
            await asyncio.to_thread(prepare_product, product_id)
        except Exception as exc:
            logger.exception("advertising_queue.prepare_failed product=%s", product_id)
            with SessionLocal() as db:
                product = db.get(AdvertisingProduct, product_id)
                product.status = "failed"
                product.error = f"{type(exc).__name__}: {str(exc)[:800]}"
                title = product.title
                db.commit()
            from .advertising_speech import SpeechLengthError
            reason = ("Нейросеть вернула слишком короткие реплики. Фото и карточку менять не нужно. "
                      if isinstance(exc, SpeechLengthError) else
                      "Подготовка остановилась; подробности сохранены в поле «Ошибка» карточки товара. ")
            await bot.send_message(chat_id=chat_id, text=f"Не удалось подготовить рекламу «{title}». "
                                   + reason + "Товар и готовые варианты сохранены. В админке доступно действие «Повторить подготовку товаров с ошибкой».")
        else:
            await deliver_ready_product(bot, chat_id)


async def run_queue(bot):
    logger.info("advertising_queue.started cadence=2-days time=09:00 timezone=Europe/Moscow")
    while True:
        try:
            await tick(bot)
        except Exception:
            logger.exception("advertising_queue.tick_failed")
        await asyncio.sleep(20)
