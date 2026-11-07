from app.directorial_notes import split_notes, spoken_text, remember_notes, direction_for_session
from app.scene_speech_budget import normalize_speech_phrase_for_voice
from app.story_generation import _clean_voiceover
from app.discipline_modes import reframe_title_cues
from app.post_production import _extra_title_cues


def test_nested_notes_have_anchor_but_are_not_spoken():
    text='У меня нет энергии (герой у окна (камера сзади)). Листаю телефон [камера входит в экран].'
    voice, notes=split_notes(text)
    assert voice=='У меня нет энергии. Листаю телефон.'
    assert notes[0]['after']=='У меня нет энергии'
    assert notes[0]['direction']=='герой у окна (камера сзади)'
    assert 'камера' not in normalize_speech_phrase_for_voice(text)
    assert 'камера' not in _clean_voiceover(text)
    assert len(notes)==2


def test_pause_and_unclosed_input_are_preserved():
    assert spoken_text('Привет [пауза] мир')=='Привет [пауза] мир'
    assert spoken_text('Привет (пока не закончил')=='Привет (пока не закончил'
    assert spoken_text('Mix flour (200 g).')=='Mix flour.'


def test_notes_survive_session_context_and_keep_order():
    session={}
    remember_notes(session,'Телефон (наезд).')
    remember_notes(session,'Телефон (наезд).')
    assert len(session['directorial_notes'])==1
    assert 'наезд' in direction_for_session(session)
    assert 'Телефон' in direction_for_session(session)


def test_reframe_title_uses_real_middle_alignment(monkeypatch):
    from app import story_generation
    monkeypatch.setattr(story_generation,'_word_timing_rows',lambda *a:[('Начало',0,1),('А',9,9.2),('что',9.2,9.4),('если',9.4,9.8),('устал',10,11)])
    cues=reframe_title_cues('x',20,{'available':True})
    assert cues[0]['start_sec']==9
    assert cues[0]['text']=='А что если?'
    assert cues[0]['word_timing'][2]['text']=='если?'
    assert reframe_title_cues('x',20,None)==[]
    style={'extra_title_cues_by_lang':{'ru':cues}}
    assert _extra_title_cues(style,'ru',10)[0]['end_sec']==10
    assert _extra_title_cues(style,'en',20)==[]
    assert _extra_title_cues(style,'ru',8)==[]


def test_middle_title_renders_at_its_own_word_times_with_same_style():
    from app.editorial_approved import ass_events
    style={'editorial_style_id':'1','word_timing_by_lang':{'ru':[
        {'text':'У','start':0,'end':.2},{'text':'меня','start':.2,'end':.4},
        {'text':'нет','start':.4,'end':.6},{'text':'энергии','start':.6,'end':1}]}}
    cue={'text':'А что если?','language':'ru','start_sec':9,'end_sec':10.5,'word_timing':[
        {'text':'А','start':9,'end':9.2},{'text':'что','start':9.2,'end':9.4},
        {'text':'если?','start':9.4,'end':10}]}
    events=ass_events(cue,style,720,1280,'Default')
    assert len(events)>3
    starts=[line.split(',')[1] for line in events]
    assert '0:00:09.00' in starts and '0:00:09.40' in starts
    assert all(start.startswith('0:00:09.') for start in starts)
    assert style['word_timing_by_lang']['ru'][0]['start']==0


def test_direction_notes_are_cached_and_copied_on_session_transition():
    from app.session_store import _chat_session_cache_payload
    from app.handlers.handlers_state_start import _copy_generation_mode_context
    source={'generation_mode':'advice'}
    remember_notes(source,'Подача (камера проходит через окно).')
    target={}
    _copy_generation_mode_context(source,target)
    assert target['directorial_notes']==source['directorial_notes']
    assert _chat_session_cache_payload(target)['directorial_notes']==source['directorial_notes']


def test_large_title_replaces_words_without_echo_in_later_caption_blocks():
    from app.post_production import build_word_synced_subtitle_cues, _without_large_title_words
    words=[('А',9,9.2),('что',9.2,9.4),('если',9.4,9.7),('я',9.7,10),('устал',10,10.4)]
    cues=build_word_synced_subtitle_cues(words,window_end_sec=11)
    title={'kind':'title','text':'А что если?','word_timing':[
        {'text':w,'start':a,'end':b} for w,a,b in words[:3]]}
    result=_without_large_title_words(cues,[title],{'replace_subtitle_phrases':True},'ru')
    assert result and result[0]['start_sec']==9.7
    assert result[-1]['end_sec']==cues[-1]['end_sec']
    assert all(not any(w in c['fixed_block_words'] for w in ['А','что','если']) for c in result)
    assert result[-1]['text'].endswith('устал')
    assert _without_large_title_words(cues,[title],{},'ru') is cues


def test_opening_title_uses_language_alignment_and_default_title_kind():
    from app.post_production import _build_title_overlay_cue, _without_large_title_words, build_word_synced_subtitle_cues
    words=[('Why',0,.3),('not',.3,.5),('you',.5,.8),('Today',1,1.3)]
    cues=build_word_synced_subtitle_cues(words,window_end_sec=2)
    style={'replace_subtitle_phrases':True,'word_timing_by_lang':{'en':[
        {'text':w,'start':a,'end':b} for w,a,b in words[:3]]}}
    title=_build_title_overlay_cue({'en':'Why not you?'},language='en',duration_sec=3)
    result=_without_large_title_words(cues,[title],style,'en')
    assert len(result)==1 and result[0]['text']=='Today'
    assert result[0]['start_sec']==1
