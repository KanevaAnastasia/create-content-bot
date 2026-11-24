from app.handlers.handlers_state_scene import _story_character_reference_prompt
from app.handlers.handlers_media_scene import _append_scene_identity_to_image_prompt
from app.story_generation import _fallback_start_prompt
from app.story_visual_text import STORY_VISUAL_TEXT_RULES


def test_prop_reference_separates_russian_metadata_from_native_lettering():
    prompt = _story_character_reference_prompt(
        {'entity_type': 'prop', 'display_name': 'Книга Кэрри',
         'description': 'Американское издание романа', 'reference_prompt': 'Тёмная обложка'},
        story_visual_style='history_yarn_puppet',
        feedback='Сохрани название Carrie')
    assert 'Сохрани название Carrie' in prompt
    assert STORY_VISUAL_TEXT_RULES in prompt
    assert 'окружения, надписей' not in prompt


def test_character_sheet_does_not_turn_display_name_into_printed_label():
    prompt = _story_character_reference_prompt(
        {'id': 'stephen_king', 'display_name': 'Стивен Кинг', 'description': 'В очках'},
        story_visual_style='history_yarn_puppet')
    assert 'без текста, букв, подписей' in prompt
    assert 'metadata, never captions or labels to draw' in prompt


def test_scene_and_fallback_preserve_original_prop_language_for_both_releases():
    for language in ('ru', 'en'):
        scene = {'generation_mode': 'story', 'language': language, 'characters': [],
                 'action': 'Книга на столе', 'story_visual_style_key': 'history_yarn_puppet'}
        prompt = _append_scene_identity_to_image_prompt('Книга на столе', scene)
        assert STORY_VISUAL_TEXT_RULES in prompt
        assert STORY_VISUAL_TEXT_RULES in _fallback_start_prompt(scene)
