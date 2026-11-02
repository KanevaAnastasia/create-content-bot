import os
from pathlib import Path

import pytest
from vertexai.generative_models import GenerationConfig as RealGenerationConfig

from app.vertex_images import (
    LATEST_IMAGE_GENERATION_MODEL,
    _enforce_aspect_ratio_prompt,
    _coerce_reference_images,
    _enforce_vertical_9_16_prompt,
    _image_size,
    _response_excerpt,
    image_generation_profile_models,
    next_image_generation_model,
    primary_image_generation_model,
)
from app.media_file_store import image_file_extension, image_mime_type


def test_enforce_vertical_prompt_and_russian_labels():
    out = _enforce_vertical_9_16_prompt("A cooking scene")
    assert "9:16" in out
    assert "все надписи на русском" in out.lower()


def test_enforce_vertical_prompt_forbids_sideways_content_even_when_ratio_is_already_present():
    out = _enforce_vertical_9_16_prompt("Strict vertical 9:16 frame with five scientists")

    assert "UPRIGHT PORTRAIT ORIENTATION IS MANDATORY" in out
    assert "Never rotate or tilt the artwork 90 degrees" in out
    assert "heads are above bodies" in out


def test_enforce_horizontal_prompt_for_long_mode():
    out = _enforce_aspect_ratio_prompt("A cooking scene with old aspect ratio 9:16", "16:9")
    assert "Horizontal composition" in out
    assert "aspect ratio 16:9" in out
    assert "все надписи на русском" in out.lower()


def test_vertex_transport_does_not_add_project_specific_interior():
    out = _enforce_aspect_ratio_prompt("Девочка стоит в школьном коридоре", "9:16")

    assert "CRT television" not in out
    assert "lace tablecloth" not in out
    assert "Soviet family home" not in out


def test_coerce_reference_images_priority():
    assert _coerce_reference_images(b"a", None) == [b"a"]
    assert _coerce_reference_images(None, [b"a", b"", b"b"]) == [b"a", b"b"]


def test_image_type_is_detected_from_bytes_not_file_suffix():
    jpeg = b"\xff\xd8\xff\xe0jpeg-payload"
    png = b"\x89PNG\r\n\x1a\npng-payload"

    assert image_mime_type(jpeg) == "image/jpeg"
    assert image_file_extension(jpeg) == "jpg"
    assert image_mime_type(png) == "image/png"
    assert image_file_extension(png) == "png"


def test_response_excerpt_omits_inline_binary_data():
    text = _response_excerpt('inline_data { mime_type: "image/png" data: "\\211PNG\\000abc" }')
    assert "<binary omitted>" in text
    assert "\\211PNG" not in text


def test_image_generation_defaults_to_latest_with_manual_fallbacks(monkeypatch):
    monkeypatch.delenv("IMAGE_GENERATION_PROFILE", raising=False)
    monkeypatch.delenv("VERTEX_IMAGE_MODEL", raising=False)
    monkeypatch.setenv("VERTEX_IMAGE_MODEL_FALLBACKS", "gemini-3.1-flash-image,gemini-2.5-flash-image")

    models = image_generation_profile_models()

    assert models == [LATEST_IMAGE_GENERATION_MODEL, "gemini-3.1-flash-image", "gemini-2.5-flash-image"]
    assert primary_image_generation_model() == LATEST_IMAGE_GENERATION_MODEL
    assert next_image_generation_model(LATEST_IMAGE_GENERATION_MODEL) == "gemini-3.1-flash-image"


def test_image_generation_profiles_are_pinned_to_latest(monkeypatch):
    monkeypatch.setenv("IMAGE_GENERATION_PROFILE", "cheap")
    monkeypatch.delenv("VERTEX_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("VERTEX_IMAGE_MODEL_FALLBACKS", raising=False)

    models = image_generation_profile_models()

    assert models == [LATEST_IMAGE_GENERATION_MODEL]
    assert next_image_generation_model("gemini-3.1-flash-image-preview") is None


def test_image_generation_legacy_uses_primary_env_model_with_manual_fallbacks(monkeypatch):
    monkeypatch.setenv("IMAGE_GENERATION_PROFILE", "legacy")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "custom-a")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL_FALLBACKS", "custom-b,custom-c")

    assert image_generation_profile_models() == ["custom-a", "custom-b", "custom-c"]
    assert primary_image_generation_model() == "custom-a"
    assert next_image_generation_model("custom-a") == "custom-b"


def test_image_size_caps_request_at_2k(monkeypatch):
    monkeypatch.setenv("VERTEX_IMAGE_SIZE", "4K")
    assert _image_size() == "2K"
    monkeypatch.setenv("VERTEX_IMAGE_SIZE", "1K")
    assert _image_size() == "1K"


def test_vertex_sdk_uses_vertexai_sdk(monkeypatch):
    from app import vertex_images

    calls = {"init": 0, "location": "", "generate_kwargs": {}}

    class _Response:
        class _Generated:
            _image_bytes = b"img"

        images = [_Generated()]

    class _Model:
        def __init__(self, _model):
            pass

        def generate_images(self, **_kwargs):
            calls["generate_kwargs"] = _kwargs
            return _Response()

    class _RawReferenceImage:
        def __init__(self, reference_id, image):
            self.reference_id = reference_id
            self.image = image

    import sys
    import types
    fake_vertexai = types.ModuleType("vertexai")
    def _fake_init(**kwargs):
        calls["init"] += 1
        calls["location"] = kwargs.get("location", "")

    fake_vertexai.init = _fake_init
    fake_gm = types.ModuleType("vertexai.generative_models")
    fake_gm.GenerativeModel = type(
        "GenerativeModel",
        (),
        {"__init__": lambda self, _m: None, "generate_content": lambda self, _p, **_kwargs: None},
    )
    fake_gm.GenerationConfig = type("GenerationConfig", (), {"__init__": lambda self, **kwargs: setattr(self, "kwargs", kwargs)})
    fake_gm.Part = type(
        "Part",
        (),
        {"from_text": staticmethod(lambda t: {"text": t}), "from_data": staticmethod(lambda data, mime_type: {"data": data, "mime_type": mime_type})},
    )
    fake_gm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_gm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_gm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    fake_vm = types.ModuleType("vertexai.preview.vision_models")
    fake_vm.ImageGenerationModel = type(
        "ImageGenerationModel",
        (),
        {"from_pretrained": staticmethod(lambda _model: _Model(_model))},
    )
    fake_vm.RawReferenceImage = _RawReferenceImage
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_gm)
    monkeypatch.setitem(sys.modules, "vertexai.preview.vision_models", fake_vm)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.delenv("VERTEX_IMAGE_LOCATION", raising=False)
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "imagen-3-pro-001")
    out = vertex_images._generate_image_vertex_sdk("prompt", reference_images=None)
    assert out == b"img"
    assert calls["init"] >= 1
    assert calls["location"] == "us-central1"
    assert calls["generate_kwargs"]["sample_image_size"] == "2K"


def test_vertex_image_location_prefers_image_specific_env(monkeypatch):
    from app import vertex_images

    calls = {"location": ""}

    class _Response:
        class _Generated:
            _image_bytes = b"img"

        images = [_Generated()]

    class _Model:
        def __init__(self, _model):
            pass

        def generate_images(self, **_kwargs):
            return _Response()

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **kwargs: calls.__setitem__("location", kwargs.get("location", ""))
    fake_gm = types.ModuleType("vertexai.generative_models")
    fake_gm.GenerativeModel = type(
        "GenerativeModel",
        (),
        {"__init__": lambda self, _m: None, "generate_content": lambda self, _p, **_kwargs: None},
    )
    fake_gm.GenerationConfig = type("GenerationConfig", (), {"__init__": lambda self, **kwargs: setattr(self, "kwargs", kwargs)})
    fake_gm.Part = type(
        "Part",
        (),
        {"from_text": staticmethod(lambda t: {"text": t}), "from_data": staticmethod(lambda data, mime_type: {"data": data, "mime_type": mime_type})},
    )
    fake_gm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_gm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_gm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    fake_vm = types.ModuleType("vertexai.preview.vision_models")
    fake_vm.ImageGenerationModel = type(
        "ImageGenerationModel",
        (),
        {"from_pretrained": staticmethod(lambda _model: _Model(_model))},
    )
    fake_vm.RawReferenceImage = type("RawReferenceImage", (), {"__init__": lambda self, reference_id, image: None})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_gm)
    monkeypatch.setitem(sys.modules, "vertexai.preview.vision_models", fake_vm)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("VERTEX_IMAGE_LOCATION", "global")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "imagen-3-pro-001")

    assert vertex_images._generate_image_vertex_sdk("prompt", reference_images=None) == b"img"
    assert calls["location"] == "global"


def test_vertex_sdk_imagen_refuses_to_drop_reference_images_on_typeerror(monkeypatch):
    from app import vertex_images

    class _Response:
        class _Generated:
            _image_bytes = b"img-no-refs"

        images = [_Generated()]

    class _Model:
        def __init__(self, _model):
            self.calls = 0

        def generate_images(self, **kwargs):
            self.calls += 1
            if "reference_images" in kwargs:
                raise TypeError(
                    "ImageGenerationModel.generate_images() got an unexpected keyword argument 'reference_images'"
                )
            return _Response()

    class _RawReferenceImage:
        def __init__(self, reference_id, image):
            self.reference_id = reference_id
            self.image = image

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_gm = types.ModuleType("vertexai.generative_models")
    fake_gm.GenerativeModel = type(
        "GenerativeModel",
        (),
        {"__init__": lambda self, _m: None, "generate_content": lambda self, _p, **_kwargs: None},
    )
    fake_gm.GenerationConfig = type("GenerationConfig", (), {"__init__": lambda self, **kwargs: setattr(self, "kwargs", kwargs)})
    fake_gm.Part = type(
        "Part",
        (),
        {"from_text": staticmethod(lambda t: {"text": t}), "from_data": staticmethod(lambda data, mime_type: {"data": data, "mime_type": mime_type})},
    )
    fake_gm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_gm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_gm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    fake_vm = types.ModuleType("vertexai.preview.vision_models")
    fake_vm.ImageGenerationModel = type(
        "ImageGenerationModel",
        (),
        {"from_pretrained": staticmethod(lambda _model: _Model(_model))},
    )
    fake_vm.RawReferenceImage = _RawReferenceImage
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_gm)
    monkeypatch.setitem(sys.modules, "vertexai.preview.vision_models", fake_vm)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "imagen-3.0-generate-002")
    with pytest.raises(RuntimeError, match="refusing to generate without character references"):
        vertex_images._generate_image_vertex_sdk("prompt", reference_images=[b"img-ref"])


def test_vertex_sdk_uses_gemini_image_path(monkeypatch):
    from app import vertex_images

    class _Inline:
        data = b"gimg"

    class _PartObj:
        inline_data = _Inline()

    class _Content:
        parts = [_PartObj()]

    class _Candidate:
        content = _Content()

    class _Resp:
        candidates = [_Candidate()]

    captured = {}

    class _GModel:
        def __init__(self, _model):
            pass

        def generate_content(self, _parts, **_kwargs):
            captured["parts"] = _parts
            captured["kwargs"] = _kwargs
            return _Resp()

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_gm = types.ModuleType("vertexai.generative_models")
    fake_gm.GenerativeModel = _GModel
    fake_gm.GenerationConfig = type("GenerationConfig", (), {"__init__": lambda self, **kwargs: setattr(self, "kwargs", kwargs)})
    fake_gm.GenerationConfig.from_dict = classmethod(lambda cls, data: cls(**data))
    fake_gm.Part = type(
        "Part",
        (),
        {"from_text": staticmethod(lambda t: {"text": t}), "from_data": staticmethod(lambda data, mime_type: {"data": data, "mime_type": mime_type})},
    )
    fake_gm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_gm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_gm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    fake_vm = types.ModuleType("vertexai.preview.vision_models")
    fake_vm.ImageGenerationModel = type("ImageGenerationModel", (), {"from_pretrained": staticmethod(lambda _m: None)})
    fake_vm.RawReferenceImage = type("RawReferenceImage", (), {"__init__": lambda self, reference_id, image: None})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_gm)
    monkeypatch.setitem(sys.modules, "vertexai.preview.vision_models", fake_vm)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "gemini-2.5-flash-image")
    jpeg_ref = b"\xff\xd8\xff\xe0img-ref"
    out = vertex_images._generate_image_vertex_sdk("prompt", reference_images=[jpeg_ref])
    assert out == b"gimg"
    assert len(captured["parts"]) == 2
    assert captured["parts"][1] == {"data": jpeg_ref, "mime_type": "image/jpeg"}
    assert captured["kwargs"]["generation_config"].kwargs == {
        "response_modalities": ["IMAGE"],
        "image_config": {"aspect_ratio": "9:16"},
    }


@pytest.mark.parametrize("aspect_ratio", ["9:16", "16:9"])
def test_vertex_sdk_gemini_image_generation_config_uses_supported_kwargs(monkeypatch, aspect_ratio):
    from app import vertex_images

    class _Inline:
        data = b"gimg"

    class _PartObj:
        inline_data = _Inline()

    class _Content:
        parts = [_PartObj()]

    class _Candidate:
        content = _Content()

    class _Resp:
        candidates = [_Candidate()]

    captured = {}

    class _GModel:
        def __init__(self, _model):
            pass

        def generate_content(self, _parts, **_kwargs):
            captured["kwargs"] = _kwargs
            return _Resp()

    import sys
    import types

    fake_vertexai = types.ModuleType("vertexai")
    fake_vertexai.init = lambda **_kwargs: None
    fake_gm = types.ModuleType("vertexai.generative_models")
    fake_gm.GenerativeModel = _GModel

    fake_gm.GenerationConfig = RealGenerationConfig
    fake_gm.Part = type(
        "Part",
        (),
        {"from_text": staticmethod(lambda t: {"text": t}), "from_data": staticmethod(lambda data, mime_type: {"data": data, "mime_type": mime_type})},
    )
    fake_gm.SafetySetting = type("SafetySetting", (), {"__init__": lambda self, category, threshold: None})
    fake_gm.HarmCategory = type(
        "HarmCategory",
        (),
        {
            "HARM_CATEGORY_HATE_SPEECH": "hs",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "dc",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sx",
            "HARM_CATEGORY_HARASSMENT": "hr",
        },
    )
    fake_gm.HarmBlockThreshold = type("HarmBlockThreshold", (), {"BLOCK_ONLY_HIGH": "high"})
    fake_vm = types.ModuleType("vertexai.preview.vision_models")
    fake_vm.ImageGenerationModel = type("ImageGenerationModel", (), {"from_pretrained": staticmethod(lambda _m: None)})
    fake_vm.RawReferenceImage = type("RawReferenceImage", (), {"__init__": lambda self, reference_id, image: None})
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_gm)
    monkeypatch.setitem(sys.modules, "vertexai.preview.vision_models", fake_vm)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "gemini-3-pro-image-preview")

    out = vertex_images._generate_image_vertex_sdk("prompt", reference_images=[b"img-ref"], aspect_ratio=aspect_ratio)

    assert out == b"gimg"
    assert captured["kwargs"]["generation_config"].to_dict() == {
        "response_modalities": ["IMAGE"],
        "image_config": {"aspect_ratio": aspect_ratio},
    }


def test_vertex_sdk_requires_project_even_with_api_key(monkeypatch):
    from app import vertex_images

    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "imagen-3-pro-001")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        vertex_images._generate_image_vertex_sdk("prompt", reference_images=None)


def test_generate_image_vertex_does_not_try_fallbacks_after_primary_fails(monkeypatch):
    """Image generation must fail loudly instead of silently using older fallback models."""
    from app import vertex_images

    monkeypatch.setenv("IMAGE_GENERATION_PROFILE", "legacy")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "bad-model")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL_FALLBACKS", "good-a,good-b")
    calls: list[str] = []

    def fake_sdk(prompt, reference_images=None, *, model=None, request_id="-"):
        m = (model or "").strip()
        calls.append(m)
        if m == "bad-model":
            raise RuntimeError("404 Publisher Model `bad-model` was not found")
        return b"img-ok"

    monkeypatch.setattr(vertex_images, "_generate_image_vertex_sdk", fake_sdk)

    with pytest.raises(RuntimeError, match="bad-model"):
        vertex_images.generate_image_vertex("p", reference_images=None, request_id="t1")
    assert calls == ["bad-model"]


def test_generate_image_vertex_403_on_primary_does_not_try_next_model(monkeypatch):
    from app import vertex_images

    monkeypatch.setenv("IMAGE_GENERATION_PROFILE", "legacy")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "first-model")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL_FALLBACKS", "second-model")
    calls: list[str] = []

    def fake_sdk(prompt, reference_images=None, *, model=None, request_id="-"):
        m = (model or "").strip()
        calls.append(m)
        if m == "first-model":
            raise RuntimeError("403 Permission denied on first-model")
        return b"ok-by-second"

    monkeypatch.setattr(vertex_images, "_generate_image_vertex_sdk", fake_sdk)

    with pytest.raises(RuntimeError, match="first-model"):
        vertex_images.generate_image_vertex("p", reference_images=None)
    assert calls == ["first-model"]


def test_generate_image_vertex_timeout_does_not_try_next_model(monkeypatch):
    from app import vertex_images
    from app.llm_queue import LLMCallTimeoutError

    monkeypatch.setenv("IMAGE_GENERATION_PROFILE", "legacy")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "slow-model")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL_FALLBACKS", "fast-model")
    monkeypatch.setenv("VERTEX_IMAGE_GENERATE_TIMEOUT_SEC", "75")
    timeouts: list[float] = []
    sdk_models: list[str] = []

    def fake_run(fn, timeout_sec):
        timeouts.append(timeout_sec)
        raise LLMCallTimeoutError("timed out")

    def fake_sdk(*_args, model=None, **_kwargs):
        sdk_models.append(model)
        return b"img-ok"

    monkeypatch.setattr(vertex_images, "_run_llm_call_with_deadline", fake_run)
    monkeypatch.setattr(vertex_images, "_generate_image_vertex_sdk", fake_sdk)

    with pytest.raises(RuntimeError, match="timed out"):
        vertex_images.generate_image_vertex("p")

    assert timeouts == [75.0]
    assert sdk_models == []


def test_image_timeout_is_independent_from_shorter_text_timeout(monkeypatch):
    from app import vertex_images

    monkeypatch.setenv("GEMINI_GENERATE_TIMEOUT_SEC", "45")
    monkeypatch.delenv("VERTEX_IMAGE_GENERATE_TIMEOUT_SEC", raising=False)

    assert vertex_images.resolve_image_generate_timeout_sec() == 480.0


def test_image_timeout_clamps_explicit_value(monkeypatch):
    from app import vertex_images

    monkeypatch.setenv("VERTEX_IMAGE_GENERATE_TIMEOUT_SEC", "10")
    assert vertex_images.resolve_image_generate_timeout_sec() == 60.0

    monkeypatch.setenv("VERTEX_IMAGE_GENERATE_TIMEOUT_SEC", "1200")
    assert vertex_images.resolve_image_generate_timeout_sec() == 900.0


def test_generate_image_vertex_retries_429_then_succeeds(monkeypatch):
    from app import vertex_images

    monkeypatch.setenv("IMAGE_GENERATION_PROFILE", "legacy")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL", "retry-model")
    monkeypatch.setenv("VERTEX_IMAGE_MODEL_FALLBACKS", "")
    monkeypatch.setattr(vertex_images.time, "sleep", lambda *_: None)
    attempts = {"n": 0}

    def fake_sdk(prompt, reference_images=None, *, model=None, request_id="-"):
        assert model == "retry-model"
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429 Resource exhausted")
        return b"after-retry"

    monkeypatch.setattr(vertex_images, "_generate_image_vertex_sdk", fake_sdk)

    out = vertex_images.generate_image_vertex("p", reference_images=None)
    assert out == b"after-retry"
    assert attempts["n"] == 3


def test_generate_image_vertex_to_file_saves_result(monkeypatch, tmp_path: Path):
    from app import vertex_images

    monkeypatch.setattr(vertex_images, "generate_image_vertex", lambda *a, **k: b"xyz")
    out_path = tmp_path / "img" / "one.png"
    saved = vertex_images.generate_image_vertex_to_file("p", str(out_path))

    assert saved == str(out_path)
    assert out_path.read_bytes() == b"xyz"


def test_generate_image_vertex_requires_env_models(monkeypatch):
    from app import vertex_images

    monkeypatch.setenv("IMAGE_GENERATION_PROFILE", "legacy")
    monkeypatch.delenv("VERTEX_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("VERTEX_IMAGE_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(vertex_images, "_generate_image_vertex_sdk", lambda *_args, **_kwargs: b"ok")
    assert vertex_images.generate_image_vertex("p") == b"ok"
