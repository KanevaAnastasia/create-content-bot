import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import guest_handlers as guest
from app.handlers import handlers_state_start as start
from app.project_modes import PENDING_MODE_KEY, project_mode_labels
from app.session_store import _chat_session_cache_payload


def labels(markup):
    return [button.text for row in markup.keyboard for button in row]


@pytest.mark.parametrize('project', ['discipline', 'grandma_kitchen', 'other'])
def test_both_project_entry_points_share_formats(project):
    session={'selected_project_key':project,'project_workspace_mode':True}
    expected=project_mode_labels(project)
    assert labels(start._generation_mode_markup(session))==expected
    workspace=labels(guest.guest_main_menu_markup(session))
    assert [label for label in workspace if label in expected]==expected
    assert ('Совет' in expected)==(project=='grandma_kitchen')
    assert ('О Васе' in expected)==(project=='discipline')
    assert ('А что если…' in expected)==(project=='discipline')


@pytest.mark.parametrize('label,mode', [('О Васе','about_vasya'),('А что если…','what_if'),('История',None)])
def test_workspace_choice_survives_text_entry_without_second_mode_selection(monkeypatch,label,mode):
    sessions={}
    monkeypatch.setattr(guest,'SESSIONS',sessions)
    monkeypatch.setattr(start,'SESSIONS',sessions)
    monkeypatch.setattr(start,'_announce_generation_mode',AsyncMock())
    message=SimpleNamespace(reply_text=AsyncMock())
    update=SimpleNamespace(message=message,effective_chat=SimpleNamespace(id=1))
    session={'selected_project_key':'discipline','project_workspace_mode':True,'guest_mode':True,
             'discipline_narrative_mode':'old'}
    asyncio.run(guest._start_workspace_format(update,None,1,session,label))
    assert session['state']=='state_waiting_first_step_generate_text'
    assert _chat_session_cache_payload(session)[PENDING_MODE_KEY]==label
    asyncio.run(start._start_generation_with_recipe(update,None,recipe='У меня нет энергии',prefer_vertex_api=False))
    result=sessions[1]
    assert result['state']==start.STATE_WAITING_STORY_ENGLISH_CHOICE
    assert result.get('discipline_narrative_mode')==mode
    assert PENDING_MODE_KEY not in result
    assert result[start.STORY_SOURCE_SESSION_KEY]=='У меня нет энергии'
    assert not result['guest_mode'] and not result['project_workspace_mode']


@pytest.mark.parametrize('label,state',[('Копия','state_waiting_first_step_copy_video'),
                                      ('Визуализация',start.STATE_WAITING_VISUALIZATION_MEDIA)])
def test_media_formats_go_directly_to_upload(monkeypatch,label,state):
    monkeypatch.setattr(guest,'SESSIONS',{})
    monkeypatch.setattr(start,'SESSIONS',{})
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session={'selected_project_key':'discipline','generation_run_id':'old-run','production':{'old':True}}
    asyncio.run(guest._start_workspace_format(update,None,1,session,label))
    assert session['state']==state
    assert 'generation_run_id' not in session and 'production' not in session
    assert PENDING_MODE_KEY not in session


def test_grandma_advice_choice_uses_advice_pipeline(monkeypatch):
    sessions={}
    monkeypatch.setattr(guest,'SESSIONS',sessions)
    monkeypatch.setattr(start,'SESSIONS',sessions)
    monkeypatch.setattr(start,'_announce_generation_mode',AsyncMock())
    series=AsyncMock()
    monkeypatch.setattr(start,'_ask_series_choice',series)
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()),effective_chat=SimpleNamespace(id=1))
    session={'selected_project_key':'grandma_kitchen'}
    asyncio.run(guest._start_workspace_format(update,None,1,session,'Совет'))
    asyncio.run(start._start_generation_with_recipe(update,None,recipe='Почему курица сухая?',prefer_vertex_api=False))
    assert sessions[1]['generation_mode']=='advice'
    series.assert_awaited_once()
    assert 'discipline_narrative_mode' not in sessions[1]
