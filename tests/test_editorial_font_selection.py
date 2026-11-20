import pytest

from app import editorial_approved
from app import editorial_font_selection as fonts
from app.editorial_titles import fallback_parts


def test_selected_typefaces_and_original_colors():
    styles = fonts.selection()['styles']
    assert {sid: style['variant'] for sid, style in styles.items()} == {
        '1': 'А', '2': 'В', '3': None, '5': 'Б', '9': 'Б / А / В',
    }
    assert styles['1']['ru'][0]['color'] == '#EC5424'
    assert styles['1']['en'][0]['color'] == '#EA5527'
    assert styles['3']['ru'][2]['color'] == '#FC3259'
    assert styles['3']['en'][2]['color'] == '#FC3258'
    assert styles['2']['fonts'] == ['young', 'tex', 'bebB']
    assert styles['9']['fonts'] == ['cyr', 'ci', 'young']
    assert styles['9']['ru'][2]['text'] == 'ты?'


@pytest.mark.parametrize('lang,title', [('ru', 'Почему не ты?'), ('en', 'Why not you?')])
@pytest.mark.parametrize('sid', ['3', '9'])
def test_each_foreground_word_appears_once_without_an_underline(sid, lang, title):
    parts = tuple(enumerate(fallback_parts(title)))
    for role in (1, 2):
        assert len(fonts.layers(sid, lang, parts, role)) == 1
    style = {'editorial_style_id': sid, 'editorial_font_revision': fonts.REVISION}
    timing = [{'text': word, 'start': i * .5, 'end': (i + 1) * .5} for i, word in parts]
    cue = {'language': lang, 'text': title, 'start_sec': 0, 'end_sec': 2, 'word_timing': timing}
    events = editorial_approved.ass_events(cue, style, 720, 1280, 'Default')
    starts = [line.split(',')[1] for line in events if line.startswith('Dialogue: 2,')]
    assert starts == ['0:00:00.50', '0:00:01.00']


@pytest.mark.parametrize('lang,title,min_gap,min_top', [
    ('ru', 'Почему не ты?', 38, 600),
    ('en', 'Why not you?', 20, 578),
])
def test_first_style_script_clears_the_face_and_connecting_word(lang, title, min_gap, min_top):
    parts = tuple(enumerate(fallback_parts(title)))
    upper = fonts.layers('1', lang, parts, 0)[0][1]  # Include the thin outline.
    middle = fonts.layers('1', lang, parts, 1)[0][1]
    left, top, right, _ = middle.getbbox()
    script_above_word = upper.crop((left, 0, right, upper.height)).getbbox()
    assert upper.getbbox()[1] >= min_top
    assert script_above_word and top - script_above_word[3] >= min_gap


@pytest.mark.parametrize('lang,title', [('ru', 'Почему не ты?'), ('en', 'Why not you?')])
def test_ninth_style_displays_the_reference_cap_height_behind_the_subject(lang, title):
    parts = tuple(enumerate(fallback_parts(title)))
    upper = fonts.layers('9', lang, parts, 0)[0][1]
    _, top, _, bottom = upper.getbbox()
    assert 270 <= top <= 280
    assert 558 <= bottom <= 568


def test_lowercase_t_in_ninth_preset_even_after_uppercased_input():
    uppercase = ((0, 'ПОЧЕМУ'), (1, 'НЕ'), (2, 'ТЫ?'))
    normal = tuple(enumerate('Почему не ты?'.split()))
    upper_mask = fonts.layers('9', 'ru', uppercase, 2)[0][1]
    normal_mask = fonts.layers('9', 'ru', normal, 2)[0][1]
    assert upper_mask.tobytes() == normal_mask.tobytes()
