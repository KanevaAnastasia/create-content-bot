from types import SimpleNamespace

import pytest

from app import post_production as pp
from app.directorial_notes import direction_for_session
from app.series_store import attach_series_to_session, payload_for_series
from app.series_theme import HOLIDAY_THEME, holiday_music


def test_theme_survives_selection_and_is_removed_on_deselect():
    row = SimpleNamespace(id='test', title_ru='Новогодние рецепты', title_en='Christmas Recipes',
                          current_video_index=1, total_videos=10,
                          meta_json={'series_theme': HOLIDAY_THEME})
    session = {'overlay_title_style': {'title_color': '#ffffff'}}
    attach_series_to_session(session, payload_for_series(row))
    assert session['series']['series_theme'] == HOLIDAY_THEME
    assert session['overlay_title_style']['series_theme'] == HOLIDAY_THEME
    direction = direction_for_session(session)
    for text in ('EVERY scene', 'Soviet', 'falling snow', 'Christmas tree'):
        assert text in direction
    attach_series_to_session(session, None)
    assert session['overlay_title_style'] == {'title_color': '#ffffff'}
    assert direction_for_session(session) == ''


@pytest.mark.parametrize('language,title,series', [
    ('ru', 'Имбирное печенье', 'Новогодние рецепты'),
    ('en', 'Gingerbread Cookies', 'Christmas Recipes'),
])
def test_holiday_ass_fonts_and_no_leak(tmp_path, language, title, series):
    cues = [pp._build_title_overlay_cue({language: title}, language=language, duration_sec=2),
            pp._build_title_series_overlay_cue({language: series}, {language: '1 / 10'},
                                               language=language, duration_sec=2)]
    path = tmp_path / 'holiday.ass'
    pp._write_burned_subtitle_ass(path, [], title_cues=cues,
                                  title_style={'series_theme': HOLIDAY_THEME},
                                  video_width=1080, video_height=1920)
    content = path.read_text()
    assert ',SnowCapCyr,' in content
    assert ',Pudelinka,' in content
    series_event = next(line for line in content.splitlines() if line.startswith('Dialogue:') and r'\1c&H5AA63F&' in line)
    assert series.replace(' ', r'\h').lower() in series_event.lower()
    assert sum(r'\1c&H5AA63F&' in line for line in content.splitlines()) == 1
    assert title.upper().replace(' ', r'\h') in content
    assert title.replace(' ', r'\h') not in content
    assert ',&H00342BCD,' in content
    assert r'\fnCF SnowCap Snow\1c&HFFFFFF&' in content
    assert all(cue['text'] != title.upper() for cue in cues)
    pp._write_burned_subtitle_ass(path, [], title_cues=cues,
                                  video_width=1080, video_height=1920)
    assert ',SnowCapCyr,' not in path.read_text()
    assert ',Pudelinka,' not in path.read_text()
    assert 'CF SnowCap Snow' not in path.read_text()


def test_music_only_overrides_holiday_series():
    assert holiday_music({}) is None
    path, volume = holiday_music({'series_theme': HOLIDAY_THEME})
    assert path.endswith('/assets/music/17-melodija.mp3')
    assert volume == 0.18


def test_holiday_replaces_white_but_keeps_selected_colour():
    from app.series_theme import holiday_dish_color
    assert holiday_dish_color({'title_color': '#ffffff'}) == '#cd2b34'
    assert holiday_dish_color({'title_color': '#fff7e8'}) == '#cd2b34'
    assert holiday_dish_color({'title_color': '#2352aa'}) == '#2352aa'


@pytest.mark.parametrize('text', ['БОРЩ ЁЖ ЙОГУРТ', 'GINGERBREAD COOKIES'])
def test_snow_mask_aligns_with_base_in_final_renderer(tmp_path, text):
    from PIL import Image, ImageChops
    if not pp._resolve_ffmpeg():
        pytest.skip('ffmpeg is required for font alignment verification')
    source = tmp_path / 'black.png'
    Image.new('RGB', (1080, 300)).save(source)
    masks = []
    for index, font in enumerate(('SnowCapCyr', 'CF SnowCap Snow')):
        ass = tmp_path / f'{index}.ass'
        ass.write_text(
            '[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 300\n'
            '[V4+ Styles]\n'
            'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
            f'Style: Default,{font},120,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,20,20,20,1\n'
            '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
            f'Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{text}\n'
        )
        output = tmp_path / f'{index}.png'
        pp._run_ffmpeg([pp._resolve_ffmpeg(), '-y', '-i', str(source), '-vf', pp._subtitle_filter(ass),
                        '-frames:v', '1', str(output)], timeout=30, stage='snow_alignment_test')
        with Image.open(output) as image:
            masks.append(image.convert('L').point(lambda value: 255 if value > 200 else 0))
    base, snow = masks
    assert snow.getbbox() is not None
    assert ImageChops.multiply(base, snow).getbbox() is None
    assert snow.getbbox()[3] < base.getbbox()[3] * 0.65


@pytest.mark.parametrize('language,title,series,counter', [
    ('ru', 'Имбирное печенье', 'Новогодние рецепты', 'день 1 из 10'),
    ('en', 'Gingerbread Cookies', 'Christmas Recipes', 'day 1 of 10'),
])
def test_holiday_counter_has_own_right_aligned_row(tmp_path, language, title, series, counter):
    import re
    cues = [pp._build_title_overlay_cue({language: title}, language=language, duration_sec=2),
            pp._build_title_series_overlay_cue({language: series}, {language: counter}, language=language, duration_sec=2)]
    path = tmp_path / 'layout.ass'
    pp._write_burned_subtitle_ass(path, [], title_cues=cues,
                                  title_style={'series_theme': HOLIDAY_THEME}, video_width=1080, video_height=1920)
    content = path.read_text()
    day = next(line for line in content.splitlines() if line.startswith('Dialogue:') and r'\fnMarianna' in line)
    assert r'\an9' in day
    x, y = map(int, re.search(r'\\pos\((\d+),(\d+)\)', day).groups())
    assert x == 972
    title_style = next(line for line in content.splitlines() if line.startswith('Style: TitleOverlay'))
    title_y = int(title_style.split(',')[-2])
    assert y > title_y + 80
    # ASS overrides may split the number from surrounding text, but no words disappear.
    visible = re.sub(r'\{[^}]*\}', '', day.split(',,', 2)[-1]).replace(r'\h', ' ')
    assert counter in visible
    series_event = next(line for line in content.splitlines() if r'\1c&H5AA63F&' in line)
    assert int(re.search(r'\\fs(\d+)', series_event).group(1)) >= 150
