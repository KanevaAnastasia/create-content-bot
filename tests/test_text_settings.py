import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import text_settings as settings
from app import llm_utils as llm
from app.writing_style import HUMAN_WRITING_RULES, LEGACY_HUMAN_WRITING_RULES, with_writing_style


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'text.json')


def test_saved_model_overrides_provider_and_survives_reread(monkeypatch):
    monkeypatch.setenv('TEXT_LLM_PROVIDER', 'gemini')
    settings.save_text_model('gpt-6-astra')
    assert settings.selected_text_model() == 'gpt-6-astra'
    assert llm.text_llm_provider() == 'openai'
    settings.save_text_model('gpt-6.1-sol')
    assert settings.selected_text_model() == 'gpt-6.1-sol'
    with pytest.raises(ValueError):
        settings.save_text_model('../image')
    assert settings.selected_text_model() == 'gpt-6.1-sol'


def test_guest_isolation_in_concurrent_tasks_threads_and_after_error(monkeypatch):
    monkeypatch.setenv('TEXT_LLM_PROVIDER', 'gemini')
    settings.save_text_model('gpt-6-astra')

    def snapshot():
        return settings.selected_text_model(), llm.text_llm_provider(), with_writing_style('source')

    @settings.guest_text_context
    async def guest(update, context, identity):
        await asyncio.sleep(0)
        return await asyncio.to_thread(snapshot)

    async def run():
        results = await asyncio.gather(
            guest(None, None, SimpleNamespace(role='guest')),
            guest(None, None, SimpleNamespace(role='admin')),
        )
        assert results[0] == (None, 'gemini', LEGACY_HUMAN_WRITING_RULES + '\n\nsource')
        assert results[1] == ('gpt-6-astra', 'openai', HUMAN_WRITING_RULES + '\n\nsource')
        assert not settings.LEGACY_GUEST_TEXT.get()

    asyncio.run(run())


def test_legacy_saved_prompt_is_upgraded_without_duplicate_or_payload_change():
    payload = '{"quote":"Это не X, а Y","amount":200}'
    result = with_writing_style(LEGACY_HUMAN_WRITING_RULES + '\n\n' + payload)
    assert result == HUMAN_WRITING_RULES + '\n\n' + payload
    assert with_writing_style(result) == result


def test_command_rejects_guest_before_mutation(monkeypatch):
    from app import bot_app
    monkeypatch.setattr(bot_app, '_identity_for_update', lambda _: SimpleNamespace(role='guest'))
    monkeypatch.setattr(bot_app, '_quiet_mode_enabled_safe', lambda: False)
    monkeypatch.setattr(bot_app, 'guest_start', AsyncMock())
    asyncio.run(bot_app.text_model_command(SimpleNamespace(), SimpleNamespace(args=['gpt-6-astra'])))
    assert not settings.SETTINGS_PATH.exists()


def test_model_command_saves_and_reports():
    message = SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(settings.text_model_command(SimpleNamespace(effective_message=message), SimpleNamespace(args=['gpt-6-astra'])))
    assert settings.selected_text_model() == 'gpt-6-astra'
    assert 'gpt-6-astra' in message.reply_text.call_args.args[0]


@pytest.mark.parametrize('workspace, expected_model', [(False, 'gemini-nano-banana-2.1'), (True, None)])
def test_guest_image_model_does_not_change_admin_workspace(monkeypatch, workspace, expected_model):
    from app import guest_handlers as guest
    from unittest.mock import Mock
    session = {'guest_mode': True, 'project_workspace_mode': workspace, 'guest_data': {'image_description': 'scene'}}
    monkeypatch.setattr(guest, '_reject_generation_in_quiet_mode', AsyncMock(return_value=False))
    monkeypatch.setattr(guest, '_guest_image_limit_reached', lambda _: False)
    monkeypatch.setattr(guest, '_selected_character_references', lambda _: [])
    monkeypatch.setattr(guest, '_reply', AsyncMock())
    monkeypatch.setattr(guest, '_send_photo', AsyncMock())
    monkeypatch.setattr(guest, '_persist_image_candidate', Mock())
    monkeypatch.setattr(guest, '_record_automatic_quiet_mode_generation_safely', Mock())
    monkeypatch.setattr(guest, 'guest_keyboard_for_session', lambda _: None)
    generate = Mock(return_value=b'image')
    monkeypatch.setattr(guest, 'generate_image_vertex', generate)
    asyncio.run(guest._generate_image(None, session, target='image'))
    assert generate.call_args.kwargs.get('model') == expected_model
    assert session['state'] == guest.STATE_IMAGE_APPROVAL


def test_guest_context_resets_on_failure():
    @settings.guest_text_context
    async def failing(update, context, identity):
        assert settings.LEGACY_GUEST_TEXT.get()
        raise RuntimeError('test')

    async def run():
        with pytest.raises(RuntimeError):
            await failing(None, None, SimpleNamespace(role='guest'))
        assert not settings.LEGACY_GUEST_TEXT.get()
    asyncio.run(run())
