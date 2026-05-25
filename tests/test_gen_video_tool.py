import importlib.util
from pathlib import Path


def _load_gen_video_module():
    path = Path(__file__).resolve().parent.parent / "tools" / "gen_video.py"
    spec = importlib.util.spec_from_file_location("tools.gen_video", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_quality_and_mime_helpers():
    mod = _load_gen_video_module()
    assert mod._normalize_quality("1080") == "1080p"
    assert mod._guess_image_mime_type(b"\x89PNG\r\n\x1a\nabc") == "image/png"
    assert mod._guess_image_mime_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"


def test_model_alias_resolution_for_veo3_fast(monkeypatch):
    mod = _load_gen_video_module()
    monkeypatch.delenv("VERTEX_VIDEO_MODEL_VEO3_FAST", raising=False)
    assert mod._resolve_model("veo3_fast") == "veo-3.1-fast-generate-001"
    monkeypatch.setenv("VERTEX_VIDEO_MODEL_VEO3_FAST", "custom-veo,backup-veo")
    assert mod._resolve_model("veo3_fast") == "custom-veo"


def test_video_location_uses_video_specific_env_and_never_global(monkeypatch):
    mod = _load_gen_video_module()
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.delenv("VERTEX_VIDEO_LOCATION", raising=False)
    assert mod._resolve_video_location() == "us-central1"
    monkeypatch.setenv("VERTEX_VIDEO_LOCATION", "us-east5")
    assert mod._resolve_video_location() == "us-east5"


def test_build_predict_payload_contains_image_and_params():
    mod = _load_gen_video_module()
    payload = mod._build_predict_payload(
        prompt="p",
        start_image=b"\xff\xd8\xffabc",
        end_image=None,
        duration_sec=6,
        quality="1080",
        aspect_ratio="9:16",
        audio_enabled=True,
    )
    assert payload["parameters"]["durationSeconds"] == 6
    assert payload["parameters"]["generateAudio"] is True
    assert payload["instances"][0]["image"]["mimeType"] == "image/jpeg"
    assert set(payload["instances"][0]) == {"prompt", "image"}
    assert payload["parameters"]["safety_settings"][0]["threshold"] == "BLOCK_ONLY_HIGH"


def test_build_predict_payload_adds_only_last_frame_when_end_image_exists():
    mod = _load_gen_video_module()
    payload = mod._build_predict_payload(
        prompt="p",
        start_image=b"\xff\xd8\xffstart",
        end_image=b"\x89PNG\r\n\x1a\nend",
        duration_sec=6,
        quality="1080",
        aspect_ratio="9:16",
        audio_enabled=True,
    )
    instance = payload["instances"][0]
    assert set(instance) == {"prompt", "image", "lastFrame"}
    assert instance["image"]["mimeType"] == "image/jpeg"
    assert instance["lastFrame"]["mimeType"] == "image/png"


def test_build_predict_payload_uses_env_safety_override(monkeypatch):
    mod = _load_gen_video_module()
    monkeypatch.setenv(
        "VERTEX_VIDEO_SAFETY_SETTINGS_JSON",
        '[{"category":"HARM_CATEGORY_HATE_SPEECH","threshold":"BLOCK_NONE"}]',
    )
    payload = mod._build_predict_payload(
        prompt="p",
        start_image=b"\xff\xd8\xffabc",
        end_image=None,
        duration_sec=6,
        quality="1080",
        aspect_ratio="9:16",
        audio_enabled=True,
    )
    assert payload["parameters"]["safety_settings"] == [
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"}
    ]


def test_extract_video_bytes_from_nested_response():
    mod = _load_gen_video_module()
    fake_mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2avc1mp41"
    b64 = mod.base64.b64encode(fake_mp4).decode("utf-8")
    response = {"foo": [{"bar": {"video": b64}}]}
    out = mod._extract_video_bytes_from_response(response)
    assert out == fake_mp4
    assert mod.get_last_video_result_source() == "inline"


def test_decode_possible_base64_urlsafe_without_padding():
    mod = _load_gen_video_module()
    raw = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isom"
    enc = mod.base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    out = mod._decode_possible_base64(enc)
    assert out == raw


def test_decode_possible_base64_data_uri():
    mod = _load_gen_video_module()
    raw = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isom"
    enc = mod.base64.b64encode(raw).decode("utf-8")
    data_uri = f"data:video/mp4;base64,{enc}"
    out = mod._decode_possible_base64(data_uri)
    assert out == raw


def test_extract_video_bytes_from_gcs_uri(monkeypatch):
    mod = _load_gen_video_module()
    monkeypatch.setattr(mod, "_download_video_from_uri", lambda uri: b"\x00\x00\x00\x18ftypisom")
    response = {"videos": [{"gcsUri": "gs://bucket/path/video.mp4"}]}
    out = mod._extract_video_bytes_from_response(response)
    assert out.startswith(b"\x00\x00\x00\x18ftyp")
    assert mod.get_last_video_result_source() == "uri"
    assert mod.get_last_video_result_uri() == "gs://bucket/path/video.mp4"


def test_extract_video_bytes_from_common_videos_shape():
    mod = _load_gen_video_module()
    raw = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isom"
    b64 = mod.base64.b64encode(raw).decode("utf-8")
    response = {"videos": [{"bytesBase64Encoded": b64}]}
    out = mod._extract_video_bytes_from_response(response)
    assert out == raw


def test_extract_video_bytes_raises_detailed_provider_context():
    mod = _load_gen_video_module()
    payload = {
        "finish_reason": "SAFETY",
        "blocked_reason": "HARM_CONTENT",
        "safety_ratings": [{"category": "violence", "probability": "HIGH"}],
        "error": {"code": 400, "message": "Blocked by safety"},
    }
    try:
        mod._extract_video_bytes_from_response(payload)
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        msg = str(e)
        assert "did not return MP4 bytes" in msg
        assert "finish_reason=SAFETY" in msg
        assert "HARM_CONTENT" in msg
        assert "violence" in msg
        assert "Blocked by safety" in msg


def test_generate_video_via_vertex_uses_sdk_operation_polling(monkeypatch):
    mod = _load_gen_video_module()
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    called = {}

    def fake_post_predict_long_running(*, url, payload):
        called["url"] = url
        called["payload"] = payload
        return {"name": "projects/p/locations/us-central1/operations/123"}

    def fake_poll_operation(*, operation_name, project, location, model, timeout_sec=900):
        called["operation_name"] = operation_name
        called["project"] = project
        called["location"] = location
        called["model"] = model
        return {"video": mod.base64.b64encode(b"\x00\x00\x00\x18ftypisom").decode("utf-8")}

    monkeypatch.setattr(mod, "_post_predict_long_running", fake_post_predict_long_running)
    monkeypatch.setattr(mod, "_poll_operation", fake_poll_operation)

    import sys
    import types

    sys.modules["vertexai"] = types.SimpleNamespace(init=lambda **kwargs: called.setdefault("init", kwargs))

    out = mod.generate_video_via_vertex(
        prompt="p",
        model="veo3_fast",
        duration_sec=4,
        start_image=b"\xff\xd8\xffstart",
    )

    assert out.startswith(b"\x00\x00\x00\x18ftyp")
    assert called["init"] == {"project": "proj", "location": "us-central1"}
    assert called["operation_name"] == "projects/p/locations/us-central1/operations/123"
    assert called["project"] == "proj"
    assert called["location"] == "us-central1"
    assert called["model"] == "veo-3.1-fast-generate-001"
    assert "us-central1-aiplatform.googleapis.com" in called["url"]


def test_generate_video_via_vertex_maps_global_to_us_central1(monkeypatch):
    mod = _load_gen_video_module()
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    called = {}

    monkeypatch.setattr(
        mod,
        "_post_predict_long_running",
        lambda *, url, payload: called.setdefault("url", url)
        and {"name": "projects/p/locations/us-central1/operations/123"},
    )
    monkeypatch.setattr(
        mod,
        "_poll_operation",
        lambda **kwargs: called.setdefault("poll", kwargs)
        and {"video": mod.base64.b64encode(b"\x00\x00\x00\x18ftypisom").decode("utf-8")},
    )

    import sys
    import types

    sys.modules["vertexai"] = types.SimpleNamespace(init=lambda **kwargs: called.setdefault("init", kwargs))

    out = mod.generate_video_via_vertex(
        prompt="p",
        model="veo3_fast",
        duration_sec=4,
        start_image=b"\xff\xd8\xffstart",
    )

    assert out.startswith(b"\x00\x00\x00\x18ftyp")
    assert called["init"]["location"] == "us-central1"
    assert "us-central1-aiplatform.googleapis.com" in called["url"]
