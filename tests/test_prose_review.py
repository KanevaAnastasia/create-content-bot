import json
from unittest.mock import Mock

import pytest

from app.prose_review import EditorialValidationError, prose_issues, review_prose_output
from app.text_settings import LEGACY_GUEST_TEXT


def test_editor_runs_even_without_mechanical_flags_and_does_not_touch_visuals():
    raw = json.dumps({'voiceover_text': 'Вася совершает эпохальный шаг.', 'image_prompt': '3D render — warm light', 'duration': 8})
    generate = Mock(return_value='{"0":"Вася вышел из дома."}')
    result = json.loads(review_prose_output('Write a new story', raw, generate))
    assert result['voiceover_text'] == 'Вася вышел из дома.'
    assert result['image_prompt'] == '3D render — warm light'
    assert result['duration'] == 8
    assert generate.call_count == 1


def test_retries_dash_but_preserves_numbers_and_speech_in_video_prompt():
    raw = json.dumps({'scenes': [{'text': 'Вася — дома уже 8 часов.', 'video_prompt': '3D — warm. CHARACTERS SPEAKS: Вася — дома уже 8 часов.', 'image_prompt': 'A — B'}]})
    generate = Mock(side_effect=['{"0":"Вася — дома уже 8 часов."}', '{"0":"Вася сидит дома уже 8 часов."}'])
    result = json.loads(review_prose_output('New scene', raw, generate))['scenes'][0]
    assert result['text'] == 'Вася сидит дома уже 8 часов.'
    assert result['video_prompt'] == '3D — warm. CHARACTERS SPEAKS: Вася сидит дома уже 8 часов.'
    assert result['image_prompt'] == 'A — B'
    assert generate.call_count == 2


def test_rejects_changed_quantities_and_never_returns_invalid_draft():
    generate = Mock(return_value='{"0":"Возьми 300 граммов."}')
    with pytest.raises(EditorialValidationError):
        review_prose_output('New recipe', '{"speech":"Возьми 200 граммов."}', generate)
    assert generate.call_count == 2


def test_preserves_approved_source_and_skips_pure_visuals_and_guest():
    generate = Mock()
    raw = '{"voiceover_text":"Вася — дома.","image_prompt":"Ночь — кухня"}'
    assert review_prose_output('Копируй: Вася — дома.', raw, generate) == raw
    assert review_prose_output('Describe scene', '{"description":"Кухня — ночью"}', generate) == '{"description":"Кухня — ночью"}'
    token = LEGACY_GUEST_TEXT.set(True)
    try:
        assert review_prose_output('New story', raw, generate) == raw
    finally:
        LEGACY_GUEST_TEXT.reset(token)
    generate.assert_not_called()


@pytest.mark.parametrize('text', ['Вася — дома', 'Вася – дома', 'Вася - дома', 'Вася -- дома', '3–5 минут'])
def test_all_prose_dashes_are_detected(text):
    assert 'тире' in prose_issues(text)


@pytest.mark.parametrize('text', ['кто-то', 'по-русски', 'https://example.org/a-b', '[пауза]'])
def test_technical_hyphens_and_exact_quotes_survive(text):
    assert not prose_issues(text)


def test_plain_publication_prose_is_explicitly_selected():
    generate = Mock(return_value='{"0":"Вася снова дома."}')
    assert review_prose_output('Write a post', 'Вася — дома.', generate, plain_text=True) == 'Вася снова дома.'


@pytest.mark.parametrize('answer', ['{"extra":"test"}', '{"0":null}', '{"0":""}', 'not json'])
def test_invalid_editor_contract_fails_bounded(answer):
    generate = Mock(return_value=answer)
    with pytest.raises(EditorialValidationError):
        review_prose_output('New story', '{"voiceover_text":"Вася идёт домой."}', generate)
    assert generate.call_count == 2


def test_entrypoint_uses_same_backend_without_recursive_review(monkeypatch):
    from app import llm_utils as llm
    monkeypatch.setattr(llm, 'text_llm_provider', lambda: 'gemini')
    generate = Mock(side_effect=['{"voiceover_text":"Вася — дома."}', '{"0":"Вася сидит дома."}'])
    monkeypatch.setattr(llm, '_gemini_generate_sync', generate)
    assert json.loads(llm.gemini_generate_sync('New story'))['voiceover_text'] == 'Вася сидит дома.'
    assert generate.call_count == 2


def test_quotes_cannot_hide_new_dashes():
    assert prose_issues('«Вася — дома»')
    assert not prose_issues('«Исходник — цитата»', protected_quotes=('«Исходник — цитата»',))


@pytest.mark.parametrize('text', ['Это не план, а мечта.', 'Это не просто план, но и мечта.', '“Новый Вася”', '**Важный вывод**'])
def test_obvious_style_markers_are_rejected(text):
    assert prose_issues(text)


def test_video_visual_text_is_never_replaced():
    raw = json.dumps({'scenes': [{'text': 'Вася — дома.', 'video_prompt': 'The sign says Вася — дома. No dialogue.'}]})
    generate = Mock(return_value='{"0":"Вася сидит дома."}')
    result = json.loads(review_prose_output('New scene', raw, generate))
    assert result['scenes'][0]['video_prompt'] == 'The sign says Вася — дома. No dialogue.'


def test_duplicate_speech_edits_must_match():
    raw = '{"scenes":[{"speech":"Вася дома."},{"speech":"Вася дома."}]}'
    generate = Mock(return_value='{"0":"Вася отдыхает.","1":"Вася гуляет."}')
    with pytest.raises(EditorialValidationError):
        review_prose_output('New story', raw, generate)
