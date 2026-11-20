import pytest

from app import llm_utils as llm


@pytest.fixture(autouse=True)
def isolated_backend(monkeypatch):
    monkeypatch.setenv('TEXT_LLM_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-secret')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-6-astra')
    monkeypatch.delenv('OPENAI_CREATIVE_MODEL', raising=False)
    monkeypatch.setattr(llm, 'submit_llm', lambda fn: fn())
    monkeypatch.setattr(llm.time, 'sleep', lambda _: None)


def response(monkeypatch, data, status=200):
    calls = []

    class Response:
        ok = status < 400
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def json(self):
            return data

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(llm.requests, 'post', post)
    return calls


def completed(text='{"script": "hello"}'):
    return {'status': 'completed', 'output': [
        {'type': 'reasoning'},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]},
    ]}


def test_routes_existing_entrypoint_to_openai_with_images(monkeypatch):
    calls = response(monkeypatch, completed())
    monkeypatch.setenv('OPENAI_CREATIVE_MODEL', 'creative-model')
    image = b'\x89PNG\r\n\x1a\nimage'
    assert llm.gemini_generate_sync('script', images=[image, image], model_profile='creative') == '{"script": "hello"}'
    url, kwargs = calls[0]
    assert url == 'https://api.openai.com/v1/responses'
    assert kwargs['json']['model'] == 'creative-model'
    assert kwargs['json']['store'] is False
    content = kwargs['json']['input'][0]['content']
    from app.writing_style import with_writing_style
    assert content[0] == {'type': 'input_text', 'text': with_writing_style('script')}
    assert len(content) == 2
    assert content[1]['image_url'].startswith('data:image/png;base64,')


def test_explicit_gemini_preserves_arguments(monkeypatch):
    monkeypatch.setenv('TEXT_LLM_PROVIDER', 'gemini')
    captured = {}
    def generate(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return 'gemini result'
    monkeypatch.setattr(llm, '_gemini_generate_sync', generate)
    assert llm.gemini_generate_sync('edit', model_profile='edit', rate_limit_max_attempts=1) == 'gemini result'
    assert captured['model_profile'] == 'edit'
    assert captured['rate_limit_max_attempts'] == 1


def test_default_provider_and_invalid_config(monkeypatch):
    monkeypatch.delenv('TEXT_LLM_PROVIDER')
    assert llm.text_llm_provider() == 'openai'
    monkeypatch.delenv('OPENAI_API_KEY')
    assert llm.text_llm_provider() == 'gemini'
    monkeypatch.setenv('TEXT_LLM_PROVIDER', 'typo')
    with pytest.raises(RuntimeError, match='TEXT_LLM_PROVIDER'):
        llm.gemini_generate_sync('hello')


def test_missing_key_does_not_fall_back(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY')
    with pytest.raises(RuntimeError, match='OPENAI_API_KEY'):
        llm.gemini_generate_sync('hello')


@pytest.mark.parametrize('data', [
    {'status': 'incomplete', 'output': []},
    completed(''),
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]},
])
def test_rejects_incomplete_empty_and_refusal(monkeypatch, data):
    response(monkeypatch, data)
    with pytest.raises(RuntimeError):
        llm.gemini_generate_sync('hello')


def test_quota_does_not_retry(monkeypatch):
    calls = response(monkeypatch, {'error': {'code': 'insufficient_quota'}}, 429)
    with pytest.raises(llm.GeminiQuotaError, match='OpenAI'):
        llm.gemini_generate_sync('hello')
    assert len(calls) == 1


def test_auth_error_never_echoes_key(monkeypatch):
    calls = response(monkeypatch, {'error': {'message': 'sk-test-secret'}}, 401)
    with pytest.raises(RuntimeError, match='OpenAI HTTP 401') as error:
        llm.gemini_generate_sync('hello')
    assert 'sk-test' not in str(error.value)
    assert len(calls) == 1


def test_rate_limit_respects_attempt_override(monkeypatch):
    calls = response(monkeypatch, {'error': {'code': 'rate_limit_exceeded'}}, 429)
    with pytest.raises(llm.GeminiQuotaError):
        llm.gemini_generate_sync('hello', rate_limit_max_attempts=2)
    assert len(calls) == 2


def test_server_error_retries(monkeypatch):
    calls = response(monkeypatch, {}, 503)
    with pytest.raises(ConnectionError):
        llm.gemini_generate_sync('hello')
    assert len(calls) == llm.LLM_TRANSPORT_MAX_ATTEMPTS


def test_persistent_admin_choice_wins_over_profile_but_guest_keeps_original(monkeypatch, tmp_path):
    from app import text_settings as settings
    from app.writing_style import HUMAN_WRITING_RULES
    monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'model.json')
    settings.save_text_model('gpt-6.1-sol')
    monkeypatch.setenv('OPENAI_CREATIVE_MODEL', 'original-profile-model')
    calls = response(monkeypatch, completed())
    llm.gemini_generate_sync('script', model_profile='creative')
    assert calls[-1][1]['json']['model'] == 'gpt-6.1-sol'
    token = settings.LEGACY_GUEST_TEXT.set(True)
    try:
        llm.gemini_generate_sync('script', model_profile='creative')
    finally:
        settings.LEGACY_GUEST_TEXT.reset(token)
    payload = calls[-1][1]['json']
    assert payload['model'] == 'original-profile-model'
    assert payload['input'][0]['content'][0]['text'] == HUMAN_WRITING_RULES + '\n\nscript'
