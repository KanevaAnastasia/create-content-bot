"""Owner's durable, approval-first editorial queue. No model calls on a timer."""
from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from .db import SessionLocal
from .models import DisciplineIdea, DisciplineQueueSchedule
from .advertising_queue import aware, chat_lock, owner_chat_id, session_is_busy

logger = logging.getLogger('bot.discipline_queue')
KEY = 'discipline_queue_idea_id'
STATE = 'discipline_queue_offer'
OPEN = 'Очередь Дисциплины'
ACCEPT = 'Разобрать и создать'
SKIP = 'Отклонить и следующая'
LATER = 'Оставить на потом'
ACTIVE = ('ready', 'offered', 'deferred', 'in_progress', 'paused')


def markup(idea_id=None, *, paused=False):
    if not idea_id:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Заново с текста' if paused else ACCEPT, callback_data=f'dq:{"r" if paused else "a"}:{idea_id}')],
        [InlineKeyboardButton(SKIP, callback_data=f'dq:s:{idea_id}')],
        [InlineKeyboardButton(LATER, callback_data=f'dq:l:{idea_id}')],
    ])


def _next(db):
    return db.scalars(select(DisciplineIdea).where(DisciplineIdea.status == 'pending')
        .order_by(DisciplineIdea.position, DisciplineIdea.created_at, DisciplineIdea.id)
        .with_for_update()).first()


def claim_due(now=None, *, force=False):
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        schedule = db.get(DisciplineQueueSchedule, 1, with_for_update=True)
        if not schedule or (not schedule.enabled and not force):
            return None
        active = db.scalars(select(DisciplineIdea).where(DisciplineIdea.status.in_(ACTIVE))
                            .order_by(DisciplineIdea.position)).first()
        if active:
            if active.status == 'deferred':
                if not force and now < aware(schedule.next_run_at):
                    return None
                active.status = 'ready'
                active.delivery_index = 0
                while aware(schedule.next_run_at) <= now:
                    schedule.next_run_at = aware(schedule.next_run_at) + timedelta(days=2)
                db.commit()
            return str(active.id)
        if not force and now < aware(schedule.next_run_at):
            return None
        row = _next(db)
        # Skip missed dates without a burst. An empty queue keeps its cadence too.
        next_at = aware(schedule.next_run_at)
        while next_at <= now:
            next_at += timedelta(days=2)
        schedule.next_run_at = next_at
        if row:
            row.status = 'ready'
        db.commit()
        return str(row.id) if row else None


def card_messages(row):
    from .discipline_modes import LABELS
    blocks = [
        f'Дисциплина · {row.series} · {row.episode}\n{row.title}\n\n'
        f'Знакомая проблема\n{row.problem}\n\nПервые секунды\n{row.hook}\n\n'
        f'Режим: {LABELS.get(row.mode, row.mode)}\n\nТекст озвучки\n{row.script}',
        f'Видеоряд\n{row.direction}\n\nРазвязка\n{row.payoff}',
    ]
    if row.sources:
        blocks.append('Источники и референсы\n' + '\n'.join(
            f"{s.get('note', '')}\n{s.get('url', '')}" for s in row.sources))
    if row.review_plan:
        blocks.append('Почему так снимаем и как проверяем\n' + row.review_plan)
    return [block[i:i+3500] for block in blocks for i in range(0, len(block), 3500)]


def _session(chat_id):
    # Unlike the convenience registry, fail closed if persistence cannot be read.
    from .state import get_loaded_session, SESSIONS
    from .session_store import load_session
    session = get_loaded_session(chat_id)
    if session is None:
        session = load_session(chat_id)
        if session is not None:
            dict.__setitem__(SESSIONS, chat_id, session)
    return session or {}


def _persist(chat_id, session):
    from .session_store import save_session
    from .state import SESSIONS
    save_session(chat_id, session)
    SESSIONS[chat_id] = session


def reconcile(chat_id):
    session = _session(chat_id)
    with SessionLocal() as db:
        if session.get(KEY) and session.get('state') not in {STATE, '', None}:
            accepted = db.get(DisciplineIdea, uuid.UUID(session[KEY]), with_for_update=True)
            if accepted and accepted.status in {'offered', 'ready', 'paused'}:
                accepted.status = 'in_progress'
                db.flush()
        row = db.scalars(select(DisciplineIdea).where(DisciplineIdea.status == 'in_progress')
                         .with_for_update()).first()
        if not row:
            return
        if session.get(KEY) == str(row.id):
            row.run_id = str(session.get('generation_run_id') or row.run_id or '') or None
            if session.get('social_descriptions_sent'):
                row.status = 'completed'
                row.completed_at = datetime.now(timezone.utc)
            elif not session.get('state'):
                row.status = 'paused'
        else:
            row.status = 'paused'
        db.commit()


async def deliver(bot, chat_id, *, force=False, locked=False):
    if not locked:
        async with chat_lock(chat_id):
            return await deliver(bot, chat_id, force=force, locked=True)
    from .article_store import blocks_advertising
    session = _session(chat_id)
    reconcile(chat_id)
    if session.get('state') == STATE and session.get(KEY):
        with SessionLocal() as db:
            previous = db.get(DisciplineIdea, uuid.UUID(session[KEY]))
            if previous is None or previous.status in {'skipped', 'completed'}:
                _persist(chat_id, {})
                session = {}
    idea_id = claim_due(force=force)
    if not idea_id:
        if force:
            await bot.send_message(chat_id, 'В очереди нет новых идей. Сценарии можно добавить в админке.')
        return
    with SessionLocal() as db:
        row = db.get(DisciplineIdea, uuid.UUID(idea_id))
        status, cursor = row.status, row.delivery_index
        chunks = card_messages(row)
    if status in {'in_progress', 'paused'}:
        if force:
            if status == 'in_progress' and session.get(KEY) == idea_id:
                from .handlers.handlers_state_scene import keyboard_for_session_state
                await bot.send_message(chat_id, 'Продолжаем сохранённый этап.\n\n' + str(session.get('story_voiceover_text') or ''),
                                       reply_markup=keyboard_for_session_state(session))
            else:
                await bot.send_message(chat_id, 'Генерация приостановлена. Можно заново открыть исходный текст '
                                       'или отклонить идею. Прежние сохранённые результаты останутся в админке.',
                                       reply_markup=markup(idea_id, paused=True))
        return
    owns_offer = session.get(KEY) == idea_id and session.get('state') == STATE
    if (session_is_busy(session) and not owns_offer) or blocks_advertising(chat_id):
        if force:
            await bot.send_message(chat_id, 'Идея сохранена. Сначала завершите текущую работу или отмените её.')
        return
    if status == 'offered' and owns_offer and not force:
        return
    if not owns_offer:
        _persist(chat_id, {'selected_project_key': 'discipline', 'state': STATE, KEY: idea_id})
    if force:
        cursor = 0
    for index in range(cursor, len(chunks)):
        await bot.send_message(chat_id, chunks[index])
        with SessionLocal() as db:
            row = db.get(DisciplineIdea, uuid.UUID(idea_id), with_for_update=True)
            if row is None or row.status not in {'ready', 'offered'}:
                return
            row.delivery_index = index + 1
            db.commit()
    await bot.send_message(chat_id, 'Разберём эту идею? После выбора отдельно согласуем текст, озвучку и кадры.',
                           reply_markup=markup(idea_id))
    with SessionLocal() as db:
        row = db.get(DisciplineIdea, uuid.UUID(idea_id), with_for_update=True)
        if row is None or row.status not in {'ready', 'offered'}:
            return
        row.status = 'offered'
        row.offered_at = row.offered_at or datetime.now(timezone.utc)
        db.commit()


def build_story_session(row, *, english=False):
    from .discipline_modes import MODE_KEY, SHORT_FORMATS
    from .directorial_notes import remember_notes
    from .handlers.handlers_state_start import STATE_WAITING_STORY_SCRIPT_APPROVAL
    creative_headings = ('Ведущий приём внимания\n', 'Операторское решение\n', 'Анимационное решение\n')
    creative = '\n\n'.join(block for block in (row.review_plan or '').split('\n\n')
                           if block.startswith(creative_headings))
    source = row.script + '\n(' + row.direction + ('\n' + creative if creative else '') + ')'
    session = {
        KEY: str(row.id), 'selected_project_key': 'discipline',
        'generation_mode': 'story', MODE_KEY: row.mode, 'discipline_intro_enabled': False,
        'story_visual_style': SHORT_FORMATS[row.mode].style_key,
        'story_english_requested': english, 'target_languages': ['ru'],
        'story_title': row.title, 'story_voiceover_text': row.script,
        'story_source_text': source, 'user_input': source, 'recipe_full_text': source,
        'selected_concept': row.title, 'selected_concept_name': row.title,
        'state': STATE_WAITING_STORY_SCRIPT_APPROVAL,
    }
    remember_notes(session, source)
    return session


async def handle(update, context):
    """Text entry only. Mutations use card-bound callback buttons."""
    chat_id = getattr(getattr(update, 'effective_chat', None), 'id', None)
    text = str(getattr(getattr(update, 'message', None), 'text', '') or '').strip()
    if not text or chat_id != owner_chat_id():
        return False
    session = _session(chat_id)
    if text == OPEN or text.split('@')[0] == '/discipline_queue':
        if session.get('state') == 'waiting_generation_mode' and not session.get('generation_run_id'):
            _persist(chat_id, {})
        await deliver(context.bot, chat_id, force=True, locked=True)
        return True
    if session.get('state') == STATE and not text.startswith('/'):
        await update.message.reply_text('Используйте кнопки под текущей карточкой.', reply_markup=markup(session.get(KEY)))
        return True
    return False


async def command(update, context):
    if getattr(getattr(update, 'effective_chat', None), 'id', None) != owner_chat_id():
        return
    async with chat_lock(update.effective_chat.id):
        await handle(update, context)


async def callback(update, context):
    query = update.callback_query
    chat_id = getattr(getattr(update, 'effective_chat', None), 'id', None)
    if chat_id != owner_chat_id() or query.from_user.id != chat_id:
        await query.answer('Доступ только владельцу.', show_alert=True)
        return
    await query.answer()
    try:
        prefix, action, raw_id = str(query.data).split(':')
        idea_id = uuid.UUID(raw_id)
    except (ValueError, TypeError):
        return
    if prefix != 'dq' or action not in {'a','r','s','l'}:
        return
    async with chat_lock(chat_id):
        session = _session(chat_id)
        reconcile(chat_id)
        with SessionLocal() as db:
            schedule = db.get(DisciplineQueueSchedule, 1, with_for_update=True)
            row = db.get(DisciplineIdea, idea_id, with_for_update=True)
            if not schedule or not row or row.status not in {'ready','offered','paused','deferred'}:
                await context.bot.send_message(chat_id, 'Эта карточка уже обработана. Откройте текущую очередь.')
                return
            own_offer = session.get(KEY) == str(idea_id) and session.get('state') == STATE
            from .article_store import blocks_advertising
            if (session_is_busy(session) and not own_offer) or blocks_advertising(chat_id):
                await context.bot.send_message(chat_id, 'Сначала завершите текущую работу или отмените её. Эта идея сохранена.')
                return
            if action == 's':
                row.status = 'skipped'
                following = _next(db)
                if following:
                    following.status = 'ready'
                db.commit()
                _persist(chat_id, {})
            elif action == 'l':
                row.status = 'deferred'
                now = datetime.now(timezone.utc)
                while aware(schedule.next_run_at) <= now:
                    schedule.next_run_at = aware(schedule.next_run_at) + timedelta(days=2)
                db.commit()
                _persist(chat_id, {})
                await context.bot.send_message(chat_id, 'Идея отложена до следующей распаковки. '
                    'Чат свободен для другой работы. Вернуть раньше можно через «Очередь Дисциплины».')
                return
            else:
                if row.status == 'paused' and action != 'r':
                    await context.bot.send_message(chat_id, 'Выберите, как продолжить.', reply_markup=markup(str(idea_id), paused=True))
                    return
                story = build_story_session(row)
                _persist(chat_id, story)
                row.status = 'in_progress'
                db.commit()
        if action == 's':
            await deliver(context.bot, chat_id, force=True, locked=True)
        else:
            from .handlers.handlers_state_start import _story_script_approval_markup
            await context.bot.send_message(chat_id, 'Текст RU-озвучки\n\n' + story['story_voiceover_text'])
            await context.bot.send_message(chat_id, 'Проверьте текст. «ОК» создаст озвучку. '
                'Можно исправить текст обычными кнопками. Сейчас выбрана RU-версия; публикация согласуется отдельно.',
                reply_markup=_story_script_approval_markup())


async def run_queue(bot):
    logger.info('discipline_queue.started cadence=2-days manual-approval=true')
    while True:
        try:
            chat_id = owner_chat_id()
            if chat_id is not None:
                await deliver(bot, chat_id)
        except Exception:
            logger.exception('discipline_queue.tick_failed')
        await asyncio.sleep(20)
