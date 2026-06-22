import pytest

from app import llm_utils


def test_gemini_generate_sync_uses_vertexai_sdk(monkeypatch):
    calls = {"init": 0, "model": 0}

    class _Resp:
        text = "ok"

    class _Model:
        def __init__(self, model_name):
            calls["model"] += 1
            self.model_name = model_name

        def generate_content(self, _prompt, **_kwargs):
            return _Resp()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(llm_utils, "submit_llm", lambda fn: fn())

    import sys
    import types
    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: calls.__setitem__("init", calls["init"] + 1)
    fake_vm = types.ModuleType("vertexai.generative_models")
    fake_vm.GenerativeModel = _Model
    fake_vm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_vm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_vm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_vm)

    out = llm_utils.gemini_generate_sync("hello")
    assert out == "ok"
    assert calls["init"] >= 1
    assert calls["model"] >= 1


def test_gemini_generate_sync_sends_image_parts(monkeypatch):
    captured = {}

    class _Resp:
        text = "recipe"

    class _Part:
        @staticmethod
        def from_text(text):
            return {"text": text}

        @staticmethod
        def from_data(*, data, mime_type):
            return {"data": data, "mime_type": mime_type}

    class _Model:
        def __init__(self, _model_name):
            pass

        def generate_content(self, request, **_kwargs):
            captured["request"] = request
            return _Resp()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", "")
    monkeypatch.setattr(llm_utils, "submit_llm", lambda fn: fn())

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_vm = types.ModuleType("vertexai.generative_models")
    fake_vm.GenerativeModel = _Model
    fake_vm.Part = _Part
    fake_vm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_vm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_vm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_vm)

    result = llm_utils.gemini_generate_sync(
        "extract recipe",
        images=[b"\x89PNG\r\n\x1a\nimage"],
    )

    assert result == "recipe"
    assert captured["request"] == [
        {"text": "extract recipe"},
        {"data": b"\x89PNG\r\n\x1a\nimage", "mime_type": "image/png"},
    ]


def test_gemini_generate_sync_handles_multipart_candidate_text(monkeypatch):
    class _Part:
        def __init__(self, text):
            self.text = text

    class _Content:
        parts = [_Part("[{\"a\":1}]"), _Part("tail")]

    class _Candidate:
        content = _Content()

        @property
        def text(self):
            raise ValueError("Multiple content parts are not supported.")

    class _Resp:
        candidates = [_Candidate()]

        @property
        def text(self):
            raise ValueError("Cannot get the response text.")

    class _Model:
        def __init__(self, _model_name):
            pass

        def generate_content(self, _prompt, **_kwargs):
            return _Resp()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(llm_utils, "submit_llm", lambda fn: fn())

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_vm = types.ModuleType("vertexai.generative_models")
    fake_vm.GenerativeModel = _Model
    fake_vm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_vm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_vm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_vm)

    out = llm_utils.gemini_generate_sync("hello")
    assert "[{\"a\":1}]" in out


def test_gemini_generate_sync_reads_candidate_text_from_to_dict(monkeypatch):
    class _Resp:
        @property
        def text(self):
            raise ValueError("Cannot get the response text.")

        def to_dict(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok from dict"}]}}]}

    class _Model:
        def __init__(self, _model_name):
            pass

        def generate_content(self, _prompt, **_kwargs):
            return _Resp()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", "")
    monkeypatch.setattr(llm_utils, "submit_llm", lambda fn: fn())

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_vm = types.ModuleType("vertexai.generative_models")
    fake_vm.GenerativeModel = _Model
    fake_vm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_vm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_vm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_vm)

    assert llm_utils.gemini_generate_sync("hello") == "ok from dict"


def test_gemini_generate_sync_reports_empty_response_reason_and_tries_fallback(monkeypatch):
    calls = []

    class _Content:
        parts = []

    class _SafetyRating:
        category = "HARM_CATEGORY_DANGEROUS_CONTENT"
        probability = "HIGH"
        blocked = True

    class _EmptyCandidate:
        content = _Content()
        finish_reason = "SAFETY"
        safety_ratings = [_SafetyRating()]

    class _EmptyResp:
        candidates = [_EmptyCandidate()]

        @property
        def text(self):
            raise ValueError("Cannot get the response text.")

    class _OkResp:
        text = "ok"

    class _Model:
        def __init__(self, model_name):
            self.model_name = model_name

        def generate_content(self, _prompt, **_kwargs):
            calls.append(self.model_name)
            if self.model_name == "gemini-2.5-flash":
                return _EmptyResp()
            return _OkResp()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", "gemini-2.5-pro")
    monkeypatch.setattr(llm_utils, "submit_llm", lambda fn: fn())

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_vm = types.ModuleType("vertexai.generative_models")
    fake_vm.GenerativeModel = _Model
    fake_vm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_vm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_vm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_vm)

    assert llm_utils.gemini_generate_sync("hello") == "ok"
    assert calls == ["gemini-2.5-flash", "gemini-2.5-pro"]


def test_gemini_generate_sync_includes_empty_response_reason_when_all_models_fail(monkeypatch):
    class _Content:
        parts = []

    class _Candidate:
        content = _Content()
        finish_reason = "MAX_TOKENS"
        finish_message = "No text was produced."

    class _Resp:
        candidates = [_Candidate()]

        @property
        def text(self):
            raise ValueError("Cannot get the response text.")

    class _Model:
        def __init__(self, _model_name):
            pass

        def generate_content(self, _prompt, **_kwargs):
            return _Resp()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", "")
    monkeypatch.setattr(llm_utils, "submit_llm", lambda fn: fn())

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_vm = types.ModuleType("vertexai.generative_models")
    fake_vm.GenerativeModel = _Model
    fake_vm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_vm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_vm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_vm)

    with pytest.raises(RuntimeError) as exc:
        llm_utils.gemini_generate_sync("hello")
    assert "finish_reason=MAX_TOKENS" in str(exc.value)
    assert "finish_message=No text was produced." in str(exc.value)


def test_extract_json_array_and_object():
    arr = llm_utils.extract_json_array("```json\n[{\"a\":1}]\n```")
    assert arr == [{"a": 1}]
    obj = llm_utils.extract_json_object("text {\"x\": 2} tail")
    assert obj == {"x": 2}


def test_extract_json_array_uses_first_complete_array_when_model_returns_extra_json():
    raw = '[{"scene": 1}]\n[{"scene": 2}]'
    assert llm_utils.extract_json_array(raw) == [{"scene": 1}]


def test_extract_json_object_uses_first_complete_object_when_model_returns_extra_json():
    raw = '```json\n{"prompts": ["ok"]}\n```\n{"ignored": true}'
    assert llm_utils.extract_json_object(raw) == {"prompts": ["ok"]}


def test_backoff_delay_uses_hint(monkeypatch):
    monkeypatch.setattr(llm_utils.random, "uniform", lambda a, b: 1.0)
    err = RuntimeError("Please retry in 12.5s")
    d = llm_utils._backoff_delay_before_retry(0, err, backoff_initial_sec=1.0, backoff_max_sec=30.0)
    assert d == 12.5


def test_safe_llm_call_quota_immediate(monkeypatch):
    monkeypatch.setattr(llm_utils, "submit_llm", lambda fn: (_ for _ in ()).throw(RuntimeError("RESOURCE_EXHAUSTED")))
    with pytest.raises(llm_utils.GeminiQuotaError):
        llm_utils.safe_llm_call(lambda: "x")


def test_safe_llm_call_retries_rate_limit(monkeypatch):
    calls = {"n": 0}

    def fake_submit(fn):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 Too Many Requests")
        return "ok"

    monkeypatch.setattr(llm_utils, "submit_llm", fake_submit)
    monkeypatch.setattr(llm_utils.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm_utils.random, "uniform", lambda a, b: 1.0)
    assert llm_utils.safe_llm_call(lambda: "ok", max_attempts=3, backoff_initial_sec=0.1, backoff_max_sec=0.2) == "ok"


def test_runtime_notices_push_and_pop():
    llm_utils._push_runtime_notice("switch to vertex")
    notes = llm_utils.pop_runtime_notices()
    assert notes == ["switch to vertex"]
    assert llm_utils.pop_runtime_notices() == []
