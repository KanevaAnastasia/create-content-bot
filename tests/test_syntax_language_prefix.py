from types import SimpleNamespace
import pytest
from app import syntax_telethon_fallback as syntax

@pytest.mark.parametrize('rewritten', ['short scene', 'lang:ru short scene', 'lang:en short scene'])
def test_shortening_restores_original_language(monkeypatch, rewritten):
    monkeypatch.setattr(syntax, '_shorten_prompt_for_syntax_with_gemini_sync', lambda *a, **k: rewritten)
    result = syntax._prepare_prompt_for_syntax('lang:en ' + 'scene ' * 100, max_len=80)
    assert result == ('lang:en short scene', True)
    assert len(result[0]) <= 80

def test_caption_prefix_overrides_stale_reply_binding():
    states = {lang: {'request':SimpleNamespace(expected_lang_token='lang:'+lang), 'sent_ids':{10 if lang=='ru' else 20},'pending_refetch':set()} for lang in ['ru','en']}
    message = SimpleNamespace(id=30,reply_to=SimpleNamespace(reply_to_msg_id=10),raw_text='Ваш запрос: lang:en scene',message='')
    assert syntax._batch_message_candidate_keys(message, states) == ['en']
    assert syntax._batch_message_candidate_keys(message, {'ru':states['ru']}) == []

def test_scene_cache_restores_language(monkeypatch):
    from app.handlers import handlers_state_media as media
    source='lang:en scene'
    scene={media.SYNTAX_PROMPT_BY_LANG_KEY:{'en':{'source_hash':media._syntax_prompt_hash(source),'prompt':'scene','shortened':True}}}
    assert media._scene_syntax_prompt_for_language(scene,'en',source)=='lang:en scene'
    assert scene[media.SYNTAX_PROMPT_BY_LANG_KEY]['en']['prompt']=='lang:en scene'
