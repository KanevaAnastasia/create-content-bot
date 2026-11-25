import json
from copy import deepcopy
from app.llm_payload import scene_context
from app.openai_requests import text_request
from app.writing_style import HUMAN_WRITING_RULES, with_writing_style


def test_remove_transport_state_but_preserve_approved_content():
    speech = 'Подписывайтесь на бабушку, тут много вкусных и простых рецептов'
    scenes = [{'text': speech, 'speech_by_lang': {'en': 'Keep this exactly'},
               'characters': ['grandma', 'cat'], 'duration_sec': 8, 'camera_design': {'shot': 'close'},
               'references': ['approved-id'], 'media_costs': {'usd': 4},
               'start_frame_image_b64': 'very large payload',
               'video_results_by_lang': {'ru': {'path': 'private.mp4'}}}]
    original = deepcopy(scenes)
    cleaned = scene_context(scenes)
    assert scenes == original
    assert cleaned[0]['text'] == speech
    assert cleaned[0]['speech_by_lang'] == original[0]['speech_by_lang']
    assert cleaned[0]['references'] == ['approved-id']
    assert not {'media_costs', 'start_frame_image_b64', 'video_results_by_lang'} & cleaned[0].keys()
    assert json.loads(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))) == cleaned


def test_cached_rules_separate_from_exact_payload_and_bounded_short_output():
    raw = 'Return {"speech":"Keep  spaces.\\nAnd newline"} exactly.'
    req = text_request(with_writing_style(raw), model='gpt-6-luna', profile='short', images=[])
    assert req['input'][1]['content'][0]['text'] == raw
    assert req['input'][0]['content'][0]['text'].count(HUMAN_WRITING_RULES) == 1
    assert req['input'][0]['content'][0]['prompt_cache_breakpoint'] == {'mode': 'explicit'}
    assert req['prompt_cache_options'] == {'mode': 'explicit', 'ttl': '30m'}
    assert req['reasoning']['effort'] == 'low'
    assert req['max_output_tokens'] == 8192
    assert req['text']['verbosity'] == 'low'


def test_unknown_model_and_large_creative_plans_not_arbitrarily_capped():
    req = text_request('all scenes', model='gpt-6.2-sol', profile='creative', images=[])
    assert 'max_output_tokens' not in req
    assert 'reasoning' not in req
    req = text_request('all scenes', model='custom-model', profile='short', images=[])
    assert 'prompt_cache_options' not in req
    assert 'max_output_tokens' not in req
