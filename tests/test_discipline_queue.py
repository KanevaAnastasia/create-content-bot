import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import discipline_queue as q, state, session_store
from app.models import Base, DisciplineIdea, DisciplineQueueSchedule

START = datetime(2026, 11, 24, 6, tzinfo=timezone.utc)

@pytest.fixture
def db(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(q, 'SessionLocal', factory)
    monkeypatch.setattr(q, 'owner_chat_id', lambda: 123)
    monkeypatch.setattr(state, 'SESSIONS', {})
    monkeypatch.setattr(state, 'get_loaded_session', lambda chat_id: state.SESSIONS.get(chat_id))
    monkeypatch.setattr(session_store, 'save_session', lambda *args: None)
    monkeypatch.setattr(session_store, 'load_session', lambda *args: None)
    from app import article_store
    monkeypatch.setattr(article_store, 'blocks_advertising', lambda chat_id: False)
    with factory() as s:
        s.add(DisciplineQueueSchedule(id=1, enabled=True, next_run_at=START))
        s.commit()
    yield factory
    engine.dispose()


def add(db, pos=1, **kwargs):
    with db() as s:
        row = DisciplineIdea(slug=f'test-{pos}', title=f'Идея {pos}', position=pos,
            mode='two_lives', series='Тест', episode=pos, script='Точный утверждённый текст.',
            direction='0–4: Вася открывает тетрадь. 4–8: Кнопка придерживает лист.',
            problem='Не начинаю учиться', hook='Пустой лист', payoff='Решена задача', sources=[], **kwargs)
        s.add(row); s.commit(); return row.id


def update(text):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=123), message=SimpleNamespace(text=text, reply_text=AsyncMock()))


def click(action, idea_id):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=123),
        callback_query=SimpleNamespace(data=f'dq:{action}:{idea_id}', from_user=SimpleNamespace(id=123), answer=AsyncMock()))


def test_due_cadence_order_and_no_catchup_burst(db):
    second=add(db,2); first=add(db,1)
    assert q.claim_due(START-timedelta(seconds=1)) is None
    assert q.claim_due(START)==str(first)
    assert q.claim_due(START+timedelta(days=9))==str(first)
    with db() as s:
        assert q.aware(s.get(DisciplineQueueSchedule,1).next_run_at)==START+timedelta(days=2)
        s.get(DisciplineIdea,first).status='completed';s.commit()
    assert q.claim_due(START+timedelta(days=9))==str(second)
    with db() as s:
        assert q.aware(s.get(DisciplineQueueSchedule,1).next_run_at)==START+timedelta(days=10)


def test_busy_generation_never_overwritten(db):
    add(db)
    state.SESSIONS[123]={'state':'waiting_scene_media','generation_run_id':'existing'}
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    assert state.SESSIONS[123]['generation_run_id']=='existing'
    assert bot.send_message.await_count==1


def test_offer_does_not_call_model_and_survives_repeat_tick(db):
    idea=add(db);bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    count=bot.send_message.await_count
    asyncio.run(q.deliver(bot,123))
    assert bot.send_message.await_count==count
    assert state.SESSIONS[123][q.KEY]==str(idea)
    assert state.SESSIONS[123]['state']==q.STATE


def test_skip_immediately_offers_next_and_preserves_cadence(db):
    first=add(db);second=add(db,2);bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    with db() as s: next_at=s.get(DisciplineQueueSchedule,1).next_run_at
    asyncio.run(q.callback(click("s",first),SimpleNamespace(bot=bot)))
    assert state.SESSIONS[123][q.KEY]==str(second)
    with db() as s:
        assert s.get(DisciplineIdea,first).status=='skipped'
        assert s.get(DisciplineQueueSchedule,1).next_run_at==next_at


def test_accept_uses_exact_text_manual_approval_no_media(db):
    idea=add(db);bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    asyncio.run(q.callback(click("a",idea),SimpleNamespace(bot=bot)))
    session=state.SESSIONS[123]
    assert session['story_voiceover_text']=='Точный утверждённый текст.'
    assert session['state']=='waiting_story_script_approval'
    assert session['discipline_narrative_mode']=='two_lives'
    assert session['story_visual_style']=='classic_cel_cartoon'
    assert session['directorial_notes']
    assert 'generation_run_id' not in session
    with db() as s: assert s.get(DisciplineIdea,idea).status=='in_progress'


def test_cancel_pauses_but_completion_requires_saved_final(db):
    idea=add(db,status='in_progress')
    state.SESSIONS[123]={q.KEY:str(idea),'state':'ready_for_production','social_descriptions_sent':False}
    q.reconcile(123)
    with db() as s: assert s.get(DisciplineIdea,idea).status=='in_progress'
    state.SESSIONS[123]['social_descriptions_sent']=True
    q.reconcile(123)
    with db() as s: assert s.get(DisciplineIdea,idea).status=='completed'
    second=add(db,2,status='in_progress');state.SESSIONS[123]={}
    q.reconcile(123)
    with db() as s: assert s.get(DisciplineIdea,second).status=='paused'


def test_delivery_failure_retries_only_unsent_chunks(db):
    idea=add(db);bot=SimpleNamespace(send_message=AsyncMock(side_effect=[None,RuntimeError('offline')]))
    with pytest.raises(RuntimeError):asyncio.run(q.deliver(bot,123,force=True))
    with db() as s:
        row=s.get(DisciplineIdea,idea);assert row.delivery_index==1
        expected=q.card_messages(row)[1]
    bot.send_message=AsyncMock()
    asyncio.run(q.deliver(bot,123))
    assert bot.send_message.await_args_list[0].args[1]==expected


def test_other_user_cannot_accept_or_skip(db):
    add(db);up=update(q.ACCEPT);up.effective_chat.id=999
    assert not asyncio.run(q.handle(up,SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))))


def test_pause_schedule_prevents_delivery(db):
    add(db,status='ready')
    with db() as s:s.get(DisciplineQueueSchedule,1).enabled=False;s.commit()
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123))
    bot.send_message.assert_not_awaited()


def test_cold_start_restores_busy_session_before_offering(db, monkeypatch):
    add(db)
    saved={'state':'waiting_scene_media', 'generation_run_id':'saved-run'}
    monkeypatch.setattr(session_store,'load_session',lambda chat:saved)
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    assert state.SESSIONS[123] is saved
    assert state.SESSIONS[123]['generation_run_id']=='saved-run'


def test_storage_read_failure_never_overwrites_dialogue(db, monkeypatch):
    add(db)
    def fail(chat): raise RuntimeError('DB unavailable')
    monkeypatch.setattr(session_store,'load_session',fail)
    bot=SimpleNamespace(send_message=AsyncMock())
    with pytest.raises(RuntimeError):asyncio.run(q.deliver(bot,123,force=True))
    assert not state.SESSIONS
    bot.send_message.assert_not_awaited()


def test_admin_skipped_offer_does_not_block_next_card(db):
    first=add(db);second=add(db,2);bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    with db() as s:s.get(DisciplineIdea,first).status='skipped';s.commit()
    asyncio.run(q.deliver(bot,123,force=True))
    assert state.SESSIONS[123][q.KEY]==str(second)


def test_repeated_old_skip_cannot_skip_next_card(db):
    first=add(db);second=add(db,2);bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    for _ in range(2):asyncio.run(q.callback(click('s',first),SimpleNamespace(bot=bot)))
    with db() as s:assert s.get(DisciplineIdea,second).status=='offered'
    assert state.SESSIONS[123][q.KEY]==str(second)


def test_later_frees_chat_and_reopens_only_on_request(db):
    idea=add(db);bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    asyncio.run(q.callback(click('l',idea),SimpleNamespace(bot=bot)))
    assert not state.SESSIONS[123]
    before=bot.send_message.await_count
    asyncio.run(q.deliver(bot,123))
    assert bot.send_message.await_count==before
    asyncio.run(q.deliver(bot,123,force=True))
    assert state.SESSIONS[123][q.KEY]==str(idea)


def test_admin_skip_during_send_cannot_be_revived(db):
    idea=add(db)
    async def send(*args,**kwargs):
        with db() as s:s.get(DisciplineIdea,idea).status='skipped';s.commit()
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=send))
    asyncio.run(q.deliver(bot,123,force=True))
    with db() as s:assert s.get(DisciplineIdea,idea).status=='skipped'


def test_accept_send_failure_can_reopen_exact_script(db):
    idea=add(db);bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.deliver(bot,123,force=True))
    bot.send_message=AsyncMock(side_effect=RuntimeError('offline'))
    with pytest.raises(RuntimeError):asyncio.run(q.callback(click('a',idea),SimpleNamespace(bot=bot)))
    bot.send_message=AsyncMock()
    asyncio.run(q.deliver(bot,123,force=True))
    assert 'Точный утверждённый текст.' in bot.send_message.await_args.args[1]
    assert bot.send_message.await_args.kwargs['reply_markup'] is not None


def test_paused_can_restart_text_without_generating(db):
    idea=add(db,status='paused');bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.callback(click('r',idea),SimpleNamespace(bot=bot)))
    assert state.SESSIONS[123]['state']=='waiting_story_script_approval'


def test_review_plan_is_visible_but_never_added_to_approved_voice(db):
    idea=add(db,review_plan='Проверить удержание через72часа.')
    with db() as s:
        row=s.get(DisciplineIdea,idea)
        assert any('через72часа' in m for m in q.card_messages(row))
        session=q.build_story_session(row)
        assert 'через72часа' not in session['story_source_text']
        assert session['story_voiceover_text']==row.script


def test_article_work_cannot_be_overwritten_by_old_card(db,monkeypatch):
    from app import article_store
    idea=add(db,status='offered')
    monkeypatch.setattr(article_store,'blocks_advertising',lambda chat_id:True)
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(q.callback(click('a',idea),SimpleNamespace(bot=bot)))
    assert not state.SESSIONS
    with db() as s:assert s.get(DisciplineIdea,idea).status=='offered'


def test_selected_craft_reaches_storyboard_but_analytics_does_not(db):
    idea=add(db,review_plan='Ведущий приём внимания\nПовтор с нарушением.\n\n'
        'Операторское решение\nЛинейка и карандаш на одной диагонали.\n\n'
        'Анимационное решение\nПауза перед касанием.\n\nПосле публикации\nЗамерить пересылки.')
    with db() as s:
        story=q.build_story_session(s.get(DisciplineIdea,idea))
    assert 'одной диагонали' in story['story_source_text']
    assert 'Пауза перед касанием' in story['story_source_text']
    assert 'Замерить пересылки' not in story['story_source_text']
    assert story['story_voiceover_text']=='Точный утверждённый текст.'
