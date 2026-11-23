import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import ImageFont

from app import editorial_titles as titles
from app.handlers import editorial_title_picker as picker
from app.post_production import _write_burned_subtitle_ass


def test_three_meaningful_units_not_font_cycle():
    assert titles.fallback_parts('У меня нет энергии') == ['У меня', 'нет', 'энергии']
    assert titles.fallback_parts('I have no energy') == ['I have', 'no', 'energy']
    assert not titles.valid_parts('У меня нет энергии', ['У меня', 'много', 'энергии'])
    assert titles.valid_parts('У меня нет энергии', ['У меня', 'нет', 'энергии'])


def test_semantic_model_may_not_rewrite_user_title(monkeypatch):
    from app import pipeline_llm_steps
    monkeypatch.setattr(pipeline_llm_steps, 'gemini_text', AsyncMock(return_value='{"ru":["Ты", "полон", "энергии"]}'))
    result = asyncio.run(titles.propose_parts({'ru': 'У меня нет энергии'}))
    assert result['ru'] == ['У меня', 'нет', 'энергии']


@pytest.mark.parametrize('style_id', titles.STYLE_IDS)
@pytest.mark.parametrize('language,text', [('ru', 'У меня совершенно нет свободного времени'), ('en', 'I never have enough energy for myself')])
def test_all_presets_fit_bilingual_titles(style_id, language, text):
    style = {titles.STYLE_KEY: style_id}
    rows = titles.layout_parts(text, language, style, 1080, 1920)
    assert len(rows) == 3
    for row in rows:
        font = ImageFont.truetype(str(titles.FONT_DIR / (row['font'].replace('CF ', 'CF-') + '.ttf')), row['size'])
        box = font.getbbox(row['text'])
        assert (box[2] - box[0]) * row['scale_x'] / 100 <= 1080 * .84
        assert row['height'] / 2 <= row['y'] <= 1920 - row['height'] / 2


def test_final_ass_uses_same_preset_and_full_frame_dim_only_during_title(tmp_path):
    path = tmp_path / 'title.ass'
    style = {titles.STYLE_KEY: '7', titles.PARTS_KEY: {'ru': ['У меня', 'нет', 'энергии']}, 'dim_opacity': .23}
    cue = {'kind': 'title', 'language': 'ru', 'text': 'У меня нет энергии', 'start_sec': 1, 'end_sec': 3}
    _write_burned_subtitle_ass(path, [], title_cues=[cue], title_style=style, video_width=1080, video_height=1920)
    ass = path.read_text()
    events = [line for line in ass.splitlines() if line.startswith('Dialogue:')]
    assert len(events) == 4
    assert all(',0:00:01.00,0:00:03.00,' in line for line in events)
    assert 'l 1080 1920' in events[0]
    assert r'\1a&HC4&' in events[0]  # 23% opaque; remaining 77% is transparent.
    assert r'\fnCF Oranienbaum' in ass
    assert r'\fnCF Caveat' in ass
    assert r'\fnCF Montserrat' in ass
    assert 'У МЕНЯ' in ass and 'нет' in ass and 'ЭНЕРГИИ' in ass


def test_picker_does_not_approve_without_explicit_style(monkeypatch):
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session = {'overlay_title_by_lang': {'ru':'У меня нет энергии'}}
    assert asyncio.run(picker.handle_picker_input(update, session, 'ОК')) is True
    send = AsyncMock()
    monkeypatch.setattr(picker, '_send_card', send)
    assert asyncio.run(picker.handle_picker_input(update, session, 'Стиль 9')) is True
    assert session['overlay_title_style'][titles.STYLE_KEY] == '9'
    assert session['overlay_title_style']['editorial_font_revision'] == picker.REVISION
    assert send.await_args.args[2]['editorial_font_revision'] == picker.REVISION
    assert asyncio.run(picker.handle_picker_input(update, session, 'ОК')) is False
    assert send.await_count == 1


def test_picker_rejects_missing_words_and_preserves_style(monkeypatch):
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session = {'overlay_title_by_lang': {'ru':'У меня нет энергии'}, 'overlay_title_style': {titles.STYLE_KEY:'9'}}
    send = AsyncMock()
    monkeypatch.setattr(picker, '_send_card', send)
    asyncio.run(picker.handle_picker_input(update, session, 'У меня | нет | сил'))
    assert titles.PARTS_KEY not in session['overlay_title_style']
    asyncio.run(picker.handle_picker_input(update, session, 'У меня | нет | энергии'))
    assert session['overlay_title_style'][titles.PARTS_KEY]['ru'] == ['У меня', 'нет', 'энергии']


def test_discipline_series_picker_builds_three_level_hierarchy_without_rewriting(monkeypatch):
    replies = []

    async def reply_text(text, **_kwargs):
        replies.append(text)

    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
    session = {
        'selected_project_key': 'discipline',
        'overlay_title_by_lang': {'ru': 'Стивен Кинг', 'en': 'Stephen King'},
        'series_title_by_lang': {
            'ru': 'Истории несовершенных ошибок',
            'en': 'Stories of Imperfect Mistakes',
        },
        'series_counter_by_lang': {'ru': 'История 1', 'en': 'Story 1'},
    }
    send = AsyncMock()
    propose = AsyncMock(side_effect=AssertionError('archive hierarchy must not call the semantic splitter'))
    monkeypatch.setattr(picker, '_send_card', send)
    monkeypatch.setattr(picker, 'propose_parts', propose)

    assert asyncio.run(picker.ask_picker(update, session)) is True

    hierarchy = session['overlay_title_style'][titles.HIERARCHY_KEY]
    assert hierarchy == {
        'ru': ['Стивен Кинг', 'Истории несовершенных ошибок', 'История 1'],
        'en': ['Stephen King', 'Stories of Imperfect Mistakes', 'Story 1'],
    }
    assert send.await_count == len(titles.STYLE_IDS)
    assert all(call.args[2][titles.HIERARCHY_KEY] == hierarchy for call in send.await_args_list)
    assert any('Стивен Кинг / Истории несовершенных ошибок / История 1' in text for text in replies)


def test_discipline_series_style_selection_restores_missing_hierarchy(monkeypatch):
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session = {
        'selected_project_key': 'discipline',
        'overlay_title_by_lang': {'ru': 'Стивен Кинг'},
        'series_title_by_lang': {'ru': 'Истории несовершенных ошибок'},
        'series_counter_by_lang': {'ru': 'История 1'},
        'overlay_title_style': {},
    }
    send = AsyncMock()
    monkeypatch.setattr(picker, '_send_card', send)

    assert asyncio.run(picker.handle_picker_input(update, session, 'Стиль 9')) is True

    style = session['overlay_title_style']
    assert style[titles.STYLE_KEY] == '9'
    assert style[titles.HIERARCHY_KEY] == {
        'ru': ['Стивен Кинг', 'Истории несовершенных ошибок', 'История 1'],
    }
    assert send.await_args.args[2][titles.HIERARCHY_KEY] == style[titles.HIERARCHY_KEY]


def test_final_ass_replaces_archive_series_row_with_editorial_hierarchy(tmp_path, monkeypatch):
    from app import editorial_font_selection as selected

    observed = []

    def fake_drawings(sid, lang, parts, role, visible, foreground=False, hierarchy=False):
        observed.append((sid, lang, parts, role, visible, foreground, hierarchy))
        return (('#ffffff', f'm {role} 0 l {role + 1} 0 {role + 1} 1 {role} 1'),)

    monkeypatch.setattr(selected, 'drawings', fake_drawings)
    path = tmp_path / 'archive-title.ass'
    style = {
        titles.STYLE_KEY: '1',
        titles.HIERARCHY_KEY: {
            'ru': ['Стивен Кинг', 'Истории несовершенных ошибок', 'История 1'],
        },
    }
    cues = [
        {'kind': 'title', 'language': 'ru', 'text': 'Стивен Кинг', 'start_sec': 0, 'end_sec': 2},
        {
            'kind': 'series',
            'language': 'ru',
            'text': 'Истории несовершенных ошибок',
            'counter_text': 'История 1',
            'start_sec': 0,
            'end_sec': 2,
        },
    ]

    _write_burned_subtitle_ass(
        path,
        [],
        title_cues=cues,
        title_style=style,
        video_width=720,
        video_height=1280,
    )

    events = [line for line in path.read_text().splitlines() if line.startswith('Dialogue:')]
    assert len(events) == 4  # one dim layer and exactly three hierarchy rows
    assert [row[3] for row in observed] == [0, 1, 2]
    assert all(row[5:] == (True, True) for row in observed)
    assert observed[0][2] == (
        (0, 'Стивен Кинг'),
        (1, 'Истории несовершенных ошибок'),
        (2, 'История 1'),
    )


def test_three_parts_required_for_long_title():
    assert not titles.valid_parts('У меня нет энергии', ['У меня нет энергии'])
    assert titles.valid_parts('Почему?', ['Почему?'])


def test_discipline_style_approval_resumes_scene_review(monkeypatch):
    from app.handlers import handlers_state_scene as scene
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session = {
        'selected_project_key': 'discipline',
        'overlay_title': 'У меня нет энергии',
        'overlay_title_by_lang': {'ru':'У меня нет энергии'},
        'overlay_title_style': {titles.STYLE_KEY:'9'},
        scene.OVERLAY_SUBTITLE_DECIDED_SESSION_KEY: True,
        scene.OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY: 'editorial_scene_approval',
        'scenes_text': 'План сцен',
    }
    monkeypatch.setattr(scene, 'SESSIONS', {})
    monkeypatch.setattr(scene, 'set_generation_run_title_color_for_session', lambda *args: None)
    monkeypatch.setattr(scene, '_reply_text_chunked', AsyncMock())
    monkeypatch.setattr(scene, '_reply_text_with_optional_markup', AsyncMock())
    asyncio.run(scene._handle_waiting_overlay_title_style_approval_state(update, None, chat_id=1, session=session, text='ОК'))
    assert session['state'] == scene.STATE_WAITING_SCENE_APPROVAL
    assert session['overlay_title_style'][titles.STYLE_KEY] == '9'
    assert 'editorial_font_revision' not in session['overlay_title_style']
    assert scene.OVERLAY_TITLE_PENDING_NEXT_SESSION_KEY not in session
