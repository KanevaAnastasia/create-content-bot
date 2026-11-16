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
    assert title in content or title.replace(' ', r'\h') in content
    pp._write_burned_subtitle_ass(path, [], title_cues=cues,
                                  video_width=1080, video_height=1920)
    assert ',SnowCapCyr,' not in path.read_text()
    assert ',Pudelinka,' not in path.read_text()


def test_music_only_overrides_holiday_series():
    assert holiday_music({}) is None
    path, volume = holiday_music({'series_theme': HOLIDAY_THEME})
    assert path.endswith('/assets/music/17-melodija.mp3')
    assert volume == 0.18
