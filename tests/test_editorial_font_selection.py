import io
from pathlib import Path

import pytest
from PIL import Image

from app import editorial_approved
from app import editorial_font_selection as fonts
from app.editorial_titles import fallback_parts


@pytest.fixture
def reference_lettering(monkeypatch):
    """Use actual font specimens for geometry tests on hosts without licensed files."""
    choices = fonts.selection()['styles']
    directory = Path(__file__).parent / 'fixtures' / 'editorial_letters'
    files = {
        (style['fonts'][role], row['text']): directory / f'ink-{sid}-{lang}-{role}.png'
        for sid, style in choices.items() for lang in ('ru', 'en')
        for role, row in enumerate(style[lang])
    }

    def shapes(parts, timed):
        groups = {}
        for part in parts:
            with Image.open(files[part]) as alpha:
                image = Image.new('RGBA', alpha.size, 'white')
                image.putalpha(alpha)
                buf = io.BytesIO()
                image.save(buf, format='PNG')
            groups[part] = (buf.getvalue(),)
        return groups

    fonts.layers.cache_clear()
    fonts.drawings.cache_clear()
    monkeypatch.setattr(fonts, '_shaped_parts', shapes)
    yield
    fonts.layers.cache_clear()
    fonts.drawings.cache_clear()


def test_selected_typefaces_and_original_colors():
    styles = fonts.selection()['styles']
    assert {sid: style['variant'] for sid, style in styles.items()} == {
        '1': 'А', '2': 'В', '3': 'А', '5': 'Б', '9': 'Б / А / В',
    }
    assert styles['1']['ru'][0]['color'] == '#EC5424'
    assert styles['1']['en'][0]['color'] == '#EA5527'
    assert styles['3']['ru'][2]['color'] == '#FC3259'
    assert styles['3']['en'][2]['color'] == '#FC3258'
    assert styles['2']['fonts'] == ['young', 'tex', 'bebB']
    assert styles['3']['fonts'] == ['bebB', 'ci', 'sign']
    assert styles['9']['fonts'] == ['cyr', 'ci', 'young']
    assert styles['9']['ru'][2]['text'] == 'ты?'


@pytest.mark.parametrize('style_id', ['1', '2', '3', '5', '9'])
@pytest.mark.parametrize('lang,title', [('ru', 'Почему не ты?'), ('en', 'Why not you?')])
def test_saved_styles_render_like_a_new_selection(style_id, lang, title, reference_lettering):
    from app.editorial_titles import ass_events

    cue = {'language': lang, 'text': title, 'start_sec': 0, 'end_sec': 2}
    style = {'editorial_style_id': style_id}
    def render(settings):
        return ass_events(cue, settings, 720, 1280, 'Default')

    current = render({**style, 'editorial_font_revision': fonts.REVISION})
    assert render(style) == current
    assert render({**style, 'editorial_font_revision': 'review_v4'}) == current


@pytest.mark.parametrize('title', ['Почему не ты?', 'У меня нет времени'])
def test_reference_and_new_titles_require_the_selected_font_files(title, monkeypatch):
    parts = tuple(enumerate(fallback_parts(title)))
    observed = []

    def missing_font(shaping_request, timed):
        observed.extend(shaping_request)
        raise FileNotFoundError('missing font file')

    fonts.layers.cache_clear()
    monkeypatch.setattr(fonts, '_shaped_parts', missing_font)
    with pytest.raises(FileNotFoundError, match='missing font file'):
        fonts.layers('3', 'ru', parts, 2)
    assert [font for font, text in observed] == ['bebB', 'ci', 'sign']


@pytest.mark.parametrize('lang,title', [('ru', 'Почему не ты?'), ('en', 'Why not you?')])
@pytest.mark.parametrize('sid', ['3', '9'])
def test_each_foreground_word_appears_once_without_an_underline(sid, lang, title, reference_lettering):
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
def test_first_style_script_clears_the_face_and_connecting_word(lang, title, min_gap, min_top, reference_lettering):
    parts = tuple(enumerate(fallback_parts(title)))
    upper = fonts.layers('1', lang, parts, 0)[0][1]  # Include the thin outline.
    middle = fonts.layers('1', lang, parts, 1)[0][1]
    left, top, right, _ = middle.getbbox()
    script_above_word = upper.crop((left, 0, right, upper.height)).getbbox()
    assert upper.getbbox()[1] >= min_top
    assert script_above_word and top - script_above_word[3] >= min_gap


@pytest.mark.parametrize('lang,title', [('ru', 'Почему не ты?'), ('en', 'Why not you?')])
def test_ninth_style_displays_the_reference_cap_height_behind_the_subject(lang, title, reference_lettering):
    parts = tuple(enumerate(fallback_parts(title)))
    upper = fonts.layers('9', lang, parts, 0)[0][1]
    _, top, _, bottom = upper.getbbox()
    assert 270 <= top <= 280
    assert 558 <= bottom <= 568


@pytest.mark.parametrize('sid', ['3', '9'])
@pytest.mark.parametrize('lang,short', [('ru', 'Я'), ('en', 'I’M')])
def test_short_depth_words_clear_the_subject_but_the_foreground_pivot_keeps_its_position(sid, lang, short):
    record = fonts.selection()['styles'][sid][lang][0]
    left, _, right, _ = fonts._box(sid, record, 0, short, False, False)
    assert 0 < left < right < 220
    assert fonts._box(sid, record, 0, short, False, True) == tuple(record['box'])


def test_archive_hierarchy_uses_large_then_two_medium_rows():
    record = fonts.selection()['styles']['1']['ru'][0]
    boxes = [fonts._box('1', record, role, 'Текст', False, True, True) for role in range(3)]
    heights = [bottom - top for _left, top, _right, bottom in boxes]
    assert boxes[0][1] < boxes[1][1] < boxes[2][1]
    assert heights[0] > heights[1]
    assert heights[0] > heights[2]
    assert heights[2] <= heights[1]


def test_lowercase_t_in_ninth_preset_even_after_uppercased_input(reference_lettering):
    uppercase = ((0, 'ПОЧЕМУ'), (1, 'НЕ'), (2, 'ТЫ?'))
    normal = tuple(enumerate('Почему не ты?'.split()))
    upper_mask = fonts.layers('9', 'ru', uppercase, 2)[0][1]
    normal_mask = fonts.layers('9', 'ru', normal, 2)[0][1]
    assert upper_mask.tobytes() == normal_mask.tobytes()


@pytest.mark.parametrize('text', ['Monday', 'New York', 'я и Маша', 'you and I'])
def test_script_preserves_proper_names_in_new_phrases(text):
    assert fonts._case(text, 'you?') == text
