import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.handlers import handlers_state_scene as scene
from app.handlers import handlers_media_scene as media


def frame(color):
    stream = io.BytesIO()
    Image.new('RGB', (90, 160), color).save(stream, format='PNG')
    return stream.getvalue()


def update():
    return SimpleNamespace(effective_chat=SimpleNamespace(id=901), message=SimpleNamespace(reply_text=AsyncMock()))


@pytest.mark.parametrize('project', ['cooking', 'discipline', 'story', 'visualization'])
def test_title_waits_for_frame_and_reopens_after_replacement(monkeypatch, project):
    ask = AsyncMock(return_value=True)
    monkeypatch.setattr(scene, '_ask_overlay_title_style_approval', ask)
    session = {'project_key': project, 'state': 'waiting_scene_media', 'image_scene_index': 0,
               'overlay_title_by_lang': {'ru': 'Заголовок'}, 'production': {'json_plan': [{}]}}
    assert not asyncio.run(scene._maybe_ask_title_on_first_frame(update(), session))
    ask.assert_not_awaited()
    first = session['production']['json_plan'][0]
    first['start_frame_image_b64'] = base64.b64encode(frame('red')).decode()
    assert asyncio.run(scene._maybe_ask_title_on_first_frame(update(), session))
    assert session['overlay_title_return_state'] == 'waiting_scene_media'
    session['overlay_title_approved_frame'] = session['overlay_title_review_frame']
    assert not asyncio.run(scene._maybe_ask_title_on_first_frame(update(), session))
    first['start_frame_image_b64'] = base64.b64encode(frame('blue')).decode()
    assert asyncio.run(scene._maybe_ask_title_on_first_frame(update(), session))


def test_preview_passes_real_frame_to_production_renderer(monkeypatch):
    seen = []
    def render(background, **kwargs):
        seen.append(background.getpixel((0, 0)))
        return b'preview'
    monkeypatch.setattr(scene, 'render_title_overlay_preview', render)
    assert scene._render_overlay_title_preview('ru', 'Текст', {}, background=frame('red')) == b'preview'
    assert seen == [(255, 0, 0)]


def test_approval_updates_production_and_resumes_without_regeneration(monkeypatch):
    resume = AsyncMock()
    generate = AsyncMock()
    monkeypatch.setattr(media, '_send_next_scene_media_step', resume)
    monkeypatch.setattr(scene, '_generate_production_after_overlay_title', generate)
    monkeypatch.setattr(scene, 'set_generation_run_title_color_for_session', lambda *args: None)
    session = {'state': scene.STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL,
               'overlay_title_review_frame': 'hash',
               'overlay_title_return_state': 'waiting_scene_media',
               'overlay_title_style': {'title_color': '#123456'}, 'production': {'json_plan': [{}]}}
    session[scene.OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY] = 'first_frame'
    asyncio.run(scene._handle_waiting_overlay_title_style_approval_state(
        update(), None, chat_id=901, session=session, text='ОК'))
    assert session['state'] == 'waiting_scene_media'
    assert session['overlay_title_approved_frame'] == 'hash'
    assert session['production']['overlay_title_style']['title_color'] == '#123456'
    resume.assert_awaited_once()
    generate.assert_not_awaited()
    scene.SESSIONS.pop(901, None)


def test_discipline_defers_picker_until_frame(monkeypatch):
    from app.handlers import editorial_title_picker
    picker = AsyncMock()
    monkeypatch.setattr(editorial_title_picker, 'ask_picker', picker)
    monkeypatch.setattr(scene, 'set_generation_run_title_color_for_session', lambda *args: None)
    session = {scene.PROJECT_KEY_SESSION: 'discipline', 'scenes_text': 'Сцена',
               scene.OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY: 'editorial_scene_approval'}
    asyncio.run(scene._ask_overlay_title_style_approval(update(), session))
    picker.assert_not_awaited()
    assert session['state'] == scene.STATE_WAITING_SCENE_APPROVAL
    scene.SESSIONS.pop(901, None)


@pytest.mark.parametrize('runner_name', ['_run_scene_media_auto_approve', '_run_scene_media_ai_approve'])
def test_automatic_generation_pauses_for_title_review(monkeypatch, runner_name):
    from app.handlers import handlers_state_media
    def unexpected_context(session):
        pytest.fail('Generation continued during title approval')
    monkeypatch.setattr(handlers_state_media, '_scene_media_context', unexpected_context)
    assert asyncio.run(getattr(handlers_state_media, runner_name)(
        update(), context=None, chat_id=901,
        session={'state': scene.STATE_WAITING_OVERLAY_TITLE_STYLE_APPROVAL}))
