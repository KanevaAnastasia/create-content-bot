from app import split_panel_typography as panel
from app import story_generation, post_production


def test_opening_title_can_omit_only_explicit_words(monkeypatch):
    rows = [('Каждый', 0, .3), ('мой', .3, .5), ('день', .5, .8), ('такой', .8, 1.1),
            ('же,', 1.1, 1.3), ('как', 1.3, 1.5), ('вчера.', 1.5, 2)]
    monkeypatch.setattr(story_generation, '_word_timing_rows', lambda *args: rows)
    title = 'Каждый день такой же, как вчера.'
    text = 'Каждый мой день такой же, как вчера. В пять звонит будильник.'
    alignment = {'characters':list(text), 'character_start_times_seconds':[i*.01 for i in range(len(text))], 'character_end_times_seconds':[(i+1)*.01 for i in range(len(text))]}
    timing, interval = panel.opening_timing(title, text, 10, alignment, omitted_words=['мой'])
    assert timing[1] == {'text': 'день', 'start': .5, 'end': .8}
    assert interval == [0, 2]
    assert panel.opening_timing(title, text, 10, alignment) == ([], None)
    assert panel.opening_timing(title, text, 10, {}) == ([], None)


def test_opening_suppression_removes_omitted_word_but_keeps_next_sentence():
    cues = [{'text': w, 'start_sec': t, 'end_sec': t+.1} for w,t in [('Каждый',0),('мой',.3),('день',.5),('Будильник',2.1)]]
    result = post_production._without_large_title_words(cues, [],
        {'replace_subtitle_phrases': True, 'subtitle_suppression_ranges_by_lang': {'ru': [[0,2]]}}, 'ru')
    assert [c['text'] for c in result] == ['Будильник']
    assert post_production._without_large_title_words(cues, [], {}, 'ru') == cues


def test_panel_captions_stay_with_selected_speaker(tmp_path):
    cues = [{'fixed_block_words': ['Hello', 'world'], 'visible_word_count': 1,
             'start_sec': 0, 'end_sec': 1, 'text': 'Hello'}]
    for speaker, y in [('top', 576), ('bottom', 1536)]:
        p = tmp_path / (speaker+'.ass')
        post_production._write_burned_subtitle_ass(p, cues, title_style={'speaker_panel': speaker},video_width=1080,video_height=1920)
        assert f'\\pos(540,{y})' in p.read_text()


def test_panel_title_uses_uniform_scale_and_requested_highlight(monkeypatch):
    from app import editorial_approved, editorial_font_selection
    monkeypatch.setattr(editorial_font_selection, 'drawings', lambda *a,**k: [('#ffffff','m 0 0 l 20 0 20 20')])
    cue={'text':'Every day is just like yesterday.', 'language':'en','start_sec':0,'end_sec':3}
    style={'editorial_style_id':'2','speaker_panel':'top',
           'semantic_parts_by_lang':{'en':['Every day','is just like','yesterday.']},
           'part_colors_by_lang':{'en':{'0':'#ffd456'}}}
    events='\n'.join(editorial_approved.ass_events(cue,style,720,1280,'Default'))
    assert '\\pos(180,0)\\fscx50.00000\\fscy50.00000' in events
    assert '\\1c&H56d4ff&' in events
    assert '720 640 0 640' in events


def test_invalid_alignment_never_hides_estimated_opening():
    assert panel.opening_timing('Every day', 'Every day. Work starts.', 25, {'characters':['x']}) == ([], None)
    text = 'Every day.'
    alignment = {'characters':list(text), 'character_start_times_seconds':[0]*len(text),
                 'character_end_times_seconds':[99]*len(text)}
    assert panel.opening_timing(text, text, 25, alignment) == ([], None)


def test_opening_suppression_cannot_swallow_next_word(monkeypatch):
    text = 'Every day. At five.'
    alignment = {'characters':list(text), 'character_start_times_seconds':[i*.01 for i in range(len(text))],
                 'character_end_times_seconds':[(i+1)*.01 for i in range(len(text))]}
    monkeypatch.setattr(story_generation, '_word_timing_rows', lambda *args:
                        [('Every',0,.5),('day.',.5,1.31),('At',1.1,1.4),('five.',1.4,2)])
    timing, interval = panel.opening_timing('Every day.', text, 2, alignment)
    assert interval == [0,1.1]
    assert timing[-1]['end'] == 1.1
