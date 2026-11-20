import cv2
import numpy as np
import pytest

from app import editorial_approved
from app import editorial_font_selection as fonts
from app.editorial_titles import fallback_parts


def test_selected_typefaces_and_reference_coral():
    styles = fonts.selection()['styles']
    assert {sid: style['variant'] for sid, style in styles.items()} == {
        '1': 'А', '2': 'В', '3': None, '5': 'Б', '9': 'А',
    }
    assert styles['1']['ru'][0]['color'] == '#EC5424'
    assert styles['1']['en'][0]['color'] == '#EA5527'
    assert styles['9']['ru'][2]['text'] == 'ты?'
    assert styles['2']['fonts'] == ['young', 'tex', 'bebB']


@pytest.mark.parametrize('lang,title,counts', [('ru','Почему не ты?', [2,2]),
                                               ('en','Why not you?', [1,2])])
def test_underlines_are_continuous_and_timed_with_their_word(lang, title, counts):
    parts = tuple(enumerate(fallback_parts(title)))
    for role, count in enumerate(counts, start=1):
        layers = fonts.layers('3', lang, parts, role)
        assert len(layers) == count
        if count == 2:
            _, mask = layers[1]
            assert cv2.connectedComponents((np.array(mask) >= 128).astype('uint8'))[0] == 2
    style = {'editorial_style_id':'3','editorial_font_revision':fonts.REVISION}
    timing = [{'text':word,'start':i*.5,'end':(i+1)*.5} for i,word in parts]
    cue = {'language':lang,'text':title,'start_sec':0,'end_sec':2,'word_timing':timing}
    lines = editorial_approved.ass_events(cue,style,720,1280,'Default')
    assert sum(',0:00:00.50,' in line for line in lines if line.startswith('Dialogue: 2')) == counts[0]
    assert sum(line.startswith('Dialogue: 2,0:00:01.00,') for line in lines) == counts[1]


def test_lowercase_t_in_ninth_preset_even_after_uppercased_input():
    text = ((0, 'ПОЧЕМУ'), (1, 'НЕ'), (2, 'ТЫ?'))
    layers = fonts.layers('9', 'ru', text, 2)
    assert len(layers) == 2
    assert layers[0][1].getbbox() == fonts.layers('9', 'ru', tuple(enumerate('Почему не ты?'.split())), 2)[0][1].getbbox()
