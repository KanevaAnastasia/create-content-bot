import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app import reference_generation as reference
from app.handlers import handlers_state_start as start, handlers_state_media as media


def test_new_generation_keeps_preferences_only():
    preferences = {'selected_project_key': 'discipline', 'project_id': 'project',
                   'bot_user_id': 'user', 'user_role': 'admin', 'guest_mode': False,
                   'project_voice_id': 'voice', 'target_languages': ['ru'],
                   'pending_project_generation_mode': 'По референсу'}
    stale = {'dish_visual_brief': 'маты и бабушка', 'generation_run_id': 'old',
             'scene_media_message_index': {'1': {}}, 'scene_image_edit_pending': True,
             'directorial_notes': 'кухня', 'brand_context': 'старое',
             'scheduled_generation_mode': True, 'unknown_old_content': 'old'}
    original = {**preferences, **stale}
    assert start._session_for_new_generation(original) == preferences
    assert original['dish_visual_brief'] == stale['dish_visual_brief']


@pytest.mark.parametrize('hero', [None, {'display_name': 'Василий'}])
def test_reference_comment_is_literal_without_llm(monkeypatch, hero):
    call = Mock(side_effect=AssertionError('No text generation'))
    monkeypatch.setattr(reference, 'gemini_generate_sync', call)
    comment = 'Все ровно как на референсе\nно на сцене с тарелками наш герой'
    assert reference.generate_reference_image_prompt(b'image', comment=comment, vasily=hero) == comment
    call.assert_not_called()


@pytest.mark.parametrize('session', [
    {'generation_mode': 'reference'},
    {'selected_project_key': 'discipline', 'generation_mode': 'normal'},
])
def test_no_cooking_brief_in_reference_or_discipline(session):
    session['dish_visual_brief'] = 'бабушка, маты и кухня'
    assert media._dish_visual_brief_for_scene(session, {}, 0) == ''


def test_reference_selection_clears_old_content():
    session = {'selected_project_key': 'discipline', 'dish_visual_brief': 'old',
               'generation_run_id': 'old', 'production': {'json_plan': [{}]},
               'scene_media_message_index': {'1': {}}, 'unknown_old_content': 'old'}
    asyncio.run(start._handle_waiting_generation_mode_state(
        SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock())), None,
        chat_id=918280, session=session, text='По референсу'))
    for key in ('dish_visual_brief', 'generation_run_id', 'production',
                'scene_media_message_index', 'unknown_old_content'):
        assert key not in session
    assert session['selected_project_key'] == 'discipline'
    assert session['generation_mode'] == 'reference'
    start.SESSIONS.pop(918280, None)


def test_feedback_instruction_has_no_cooking_cast(monkeypatch):
    from app.handlers import handlers_state_commands as commands
    call = Mock(return_value='Вася на сцене')
    monkeypatch.setattr(commands, 'gemini_generate_sync', call)
    asyncio.run(commands._regenerate_image_prompt_from_feedback(
        {'start_frame_prompt': 'Вася на сцене', 'characters': ['vasily']}, 'Камера дальше'))
    sent = call.call_args.args[0].lower()
    assert 'бабуш' not in sent and 'кот как' not in sent


@pytest.mark.parametrize('project', ['grandma_kitchen', 'discipline'])
def test_actual_image_request_is_only_comment_with_ordered_references(monkeypatch, project):
    comment = 'Все ровно как на референсе\nно на сцене с тарелками наш герой'
    hero = {'display_name': 'Василий', 'asset_uri': 'file:///unused'} if project == 'discipline' else None
    scene = reference.build_reference_scene(source_image=b'composition', image_prompt=comment, vasily=hero)
    session = {'selected_project_key': project, 'generation_mode': 'reference',
               'target_languages': ['ru'], 'dish_visual_brief': 'old mats and grandma'}
    generate = AsyncMock(return_value=SimpleNamespace(data=b'image', model='mock'))
    monkeypatch.setattr(media, '_load_scene_reference_images', lambda *a, **kw: [b'character'])
    monkeypatch.setattr(media, '_generate_scene_image_result', generate)
    monkeypatch.setattr(media, '_safe_reply_photo', AsyncMock(return_value=SimpleNamespace(message_id=1)))
    monkeypatch.setattr(media, 'register_image_candidate', lambda *a, **kw: {'candidate_id': 'test'})
    for name in ('register_media_message_candidate', 'add_scene_shared_cost',
                 '_persist_image_candidate_best_effort', '_persist_message_link_best_effort',
                 '_persist_image_state_best_effort'):
        monkeypatch.setattr(media, name, lambda *a, **kw: None)
    monkeypatch.setattr(media, '_reply_text_with_optional_markup', AsyncMock())
    asyncio.run(media._scene_media_start_ok(
        SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock())),
        session=session, scene=scene, idx=0, chat_id=1))
    assert generate.await_args.args[0] == comment
    assert generate.await_args.kwargs['reference_images'] == [b'composition', b'character']
    assert scene['start_frame_prompt_effective'] == comment
