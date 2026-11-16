from app.writing_style import with_writing_style
from types import SimpleNamespace
from unittest.mock import Mock

from requests.exceptions import ConnectionError
from app import llm_utils


def test_flash_disconnection_retries_without_thinking(monkeypatch):
    monkeypatch.setenv('TEXT_LLM_PROVIDER', 'gemini')
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test')
    monkeypatch.setattr(llm_utils, '_load_vertex_generative_sdk', lambda: (None, None))
    monkeypatch.setattr(llm_utils, '_model_ids_for_profile', lambda _: ['gemini-2.5-flash'])
    monkeypatch.setattr(llm_utils, 'get_vertex_safety_settings', lambda: [])
    monkeypatch.setattr(llm_utils, 'submit_llm', lambda fn: fn())
    monkeypatch.setattr(llm_utils.time, 'sleep', lambda _: None)
    generate = Mock(side_effect=[ConnectionError('Connection aborted.'), SimpleNamespace(text='valid response')])
    monkeypatch.setattr(llm_utils, 'get_vertex_generative_model', lambda *args: SimpleNamespace(generate_content=generate))
    assert llm_utils.gemini_generate_sync('preserve this prompt') == 'valid response'
    assert 'generation_config' not in generate.call_args_list[0].kwargs
    assert generate.call_args_list[1].kwargs['generation_config'] == {'thinking_config': {'thinking_budget': 0}}
    assert [call.args for call in generate.call_args_list] == [(with_writing_style('preserve this prompt'),)] * 2
    generate.reset_mock(side_effect=True)
    generate.return_value = SimpleNamespace(text='next response')
    assert llm_utils.gemini_generate_sync('next prompt') == 'next response'
    assert 'generation_config' not in generate.call_args.kwargs
