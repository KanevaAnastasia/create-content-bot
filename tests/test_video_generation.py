from app import video_generation


def test_generate_video_bytes_passes_start_and_end_images(monkeypatch):
    captured = {}

    def fake_generate_video_via_vertex(**kwargs):
        captured.update(kwargs)
        return b"\x00\x00\x00\x18ftypisom"

    monkeypatch.setattr(video_generation, "generate_video_via_vertex", fake_generate_video_via_vertex)

    out = video_generation.generate_video_bytes(
        prompt="p",
        model="veo3_fast",
        duration_sec=4,
        start_image=b"start",
        end_image=b"end",
        quality="1080",
        aspect_ratio="9:16",
        audio_enabled=True,
    )

    assert out.startswith(b"\x00\x00\x00\x18ftyp")
    assert captured["start_image"] == b"start"
    assert captured["end_image"] == b"end"
    assert captured["prompt"] == "p"
from app import video_generation
from app.video_generation import _video_render_defaults


def test_veo3_fast_render_defaults():
    quality, aspect_ratio, audio_enabled = _video_render_defaults("veo3_fast")
    assert quality == "720"
    assert aspect_ratio == "9:16"
    assert audio_enabled is True


def test_non_veo_render_defaults():
    quality, aspect_ratio, audio_enabled = _video_render_defaults("kling")
    assert quality == "720"
    assert aspect_ratio == "9:16"
    assert audio_enabled is False


def test_generate_video_bytes_calls_vertex_function(monkeypatch):
    captured = {}

    def fake_generate_video_via_vertex(**kwargs):
        captured.update(kwargs)
        return b"mp4"

    monkeypatch.setattr(video_generation, "generate_video_via_vertex", fake_generate_video_via_vertex)
    out = video_generation.generate_video_bytes(
        prompt="hello",
        model="veo3_fast",
        duration_sec=4,
        start_image=b"start",
        end_image=b"end",
    )
    assert out == b"mp4"
    assert captured["prompt"] == "hello"
    assert captured["model"] == "veo3_fast"
    assert captured["duration_sec"] == 4
    assert captured["start_image"] == b"start"
    assert captured["end_image"] == b"end"
    assert captured["quality"] == "720"
    assert captured["aspect_ratio"] == "9:16"
    assert captured["audio_enabled"] is True
