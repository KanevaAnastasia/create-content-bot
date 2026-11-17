from types import SimpleNamespace

import pytest

from app import syntax_telethon_fallback as syntax


@pytest.mark.parametrize('label,controls,expected', [
    ('Uploaded (1/2)', 1, 1), ('Загружено (1/2)', 1, 1),
    ('', 1, 1), ('Загружено (1/2)', 2, -1), ('', 0, 0),
])
def test_attachment_count_uses_visible_controls_and_rejects_disagreement(label, controls, expected):
    def locator(selector):
        if selector == 'body':
            return SimpleNamespace(inner_text=lambda: label)
        if selector == syntax._SYNTAX_REMOVE_FILE_SELECTOR:
            return SimpleNamespace(count=lambda: controls, nth=lambda _: SimpleNamespace(is_visible=lambda: True))
        assert selector == 'input[type=file]'
        return SimpleNamespace(count=lambda: 1)
    assert syntax._syntax_veo_uploaded_reference_count_sync(SimpleNamespace(locator=locator)) == expected


def test_unreadable_page_is_not_assumed_to_have_zero_frames():
    def locator(_):
        raise RuntimeError('navigation interrupted')
    page = SimpleNamespace(locator=locator)
    assert syntax._syntax_veo_uploaded_reference_count_sync(page) == -1
    with pytest.raises(RuntimeError, match='unknown'):
        syntax._clear_syntax_veo_uploaded_references_sync(page)


@pytest.mark.parametrize('failures', [0, 1, 3])
def test_prepare_keeps_transient_frame_on_same_page_until_submit(monkeypatch, failures):
    events = []
    page = SimpleNamespace(
        reload=lambda **_: events.append('reload'),
        wait_for_load_state=lambda *a, **k: None,
    )
    monkeypatch.setattr(syntax, '_clear_syntax_veo_uploaded_references_sync', lambda p: events.append('clear'))
    monkeypatch.setattr(syntax, '_upload_syntax_kling_start_frame_sync', lambda p, image: events.append('upload'))
    pending = [failures]
    def wait(p, expected, **kwargs):
        assert expected == 1
        events.append('verify')
        if pending[0]:
            pending[0] -= 1
            raise RuntimeError('upload not acknowledged')
    monkeypatch.setattr(syntax, '_wait_for_syntax_veo_uploaded_count_sync', wait)
    monkeypatch.setattr(syntax, '_syntax_veo_uploaded_reference_count_sync', lambda p: 1)
    monkeypatch.setattr(syntax, '_fill_and_commit_syntax_kling_prompt_sync', lambda p, prompt: events.append('prompt'))
    monkeypatch.setattr(syntax, '_set_syntax_aspect_ratio_sync', lambda *a: events.append('aspect'))
    monkeypatch.setattr(syntax, '_syntax_page_contains_aspect_ratio_sync', lambda *a: True)
    monkeypatch.setattr(syntax, '_wait_for_syntax_kling_form_ready_sync', lambda p: events.append('ready'))
    if failures == 3:
        with pytest.raises(syntax.SyntaxVeoNotSubmittedError, match='confirm_upload'):
            syntax._prepare_syntax_veo_form_sync(page, start_image=b'frame', prompt='approved phrase', aspect_ratio='9:16')
        assert 'prompt' not in events
        assert events.count('upload') == 3
    else:
        syntax._prepare_syntax_veo_form_sync(page, start_image=b'frame', prompt='approved phrase', aspect_ratio='9:16')
        assert events[-6:] == ['clear', 'upload', 'verify', 'prompt', 'aspect', 'ready']
        assert events.count('reload') == failures
        assert events.count('upload') == failures + 1
    assert events.count('clear') == events.count('upload')


@pytest.mark.parametrize('stage,label', [
    ('confirm_upload', 'подтверждение загрузки кадра'),
    ('verify_persisted_frame', 'проверка сохранения кадра'),
    ('commit_prompt', 'сохранение промпта'),
])
def test_user_sees_failed_preparation_stage(stage, label):
    from app.handlers.handlers_media_core import _syntax_dispatch_error_user_message
    message = _syntax_dispatch_error_user_message(f'Syntax Veo not submitted: этап={stage}, попыток=3')
    assert label in message
    assert 'Кнопку генерации не нажимали' in message


def test_unconfirmed_launch_is_not_reported_as_not_sent():
    from app.handlers.handlers_media_core import _syntax_dispatch_error_user_message
    message = _syntax_dispatch_error_user_message('Syntax Veo launch unconfirmed: no task status')
    assert 'Результат запуска неизвестен' in message
