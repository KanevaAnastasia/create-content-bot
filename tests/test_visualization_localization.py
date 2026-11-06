from pathlib import Path

import pytest

from app import visualization_localization as vl


def test_restoration_copies_saved_masters_and_only_renders_title(tmp_path, monkeypatch):
    sources = {}
    for variant in ("clean", "upscaled_4k", "subtitled_music"):
        sources[variant] = tmp_path / f"{variant}.mp4"
        sources[variant].write_bytes(f"approved {variant}".encode())
    monkeypatch.setattr(vl.pp, "post_production_artifacts_dir", lambda: tmp_path / "out")
    monkeypatch.setattr(vl.pp, "_media_duration", lambda path: 14.8)
    monkeypatch.setattr(vl.pp, "_video_dimensions", lambda path: (2160, 3840))
    monkeypatch.setattr(vl.pp, "_resolve_ffmpeg", lambda: "ffmpeg")
    calls = []
    def render(**kwargs):
        calls.append(kwargs)
        kwargs["dest_path"].write_bytes(b"new title over saved video")
        return True
    monkeypatch.setattr(vl.pp, "_render_subtitled_music_master", render)
    post = vl.build_restored_visualization(
        sources=sources, language="en", duration=14.8, title="Why Not You?",
        title_duration=3, title_style={"title_color": "#ffc766"},
        variants=["clean", "upscaled_4k", "subtitled_music", "titled_subtitled_music"],
    )
    renders = post["renders_by_language"]["en"]
    for variant, source in sources.items():
        assert Path(renders[variant]["video_path"]).read_bytes() == source.read_bytes()
    assert len(calls) == 1
    assert calls[0]["clean_path"] == sources["subtitled_music"]
    assert calls[0]["music_path"] is None
    ass = calls[0]["subtitles_path"].read_text()
    assert r"Why\hNot\hYou?" in ass
    assert "&H0066C7FF" in ass
    assert "0:00:03.00,TitleOverlayEN" in ass
    assert ass.count("Dialogue:") == 1  # Saved subtitles are already in the source.


def test_restoration_rejects_changed_source_before_writing(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"old edit")
    monkeypatch.setattr(vl.pp, "_media_duration", lambda path: 20)
    with pytest.raises(ValueError, match="duration changed"):
        vl.build_restored_visualization(
            sources={"subtitled_music": source}, language="en", duration=14.8,
            title="Why Not You?", title_duration=3, title_style={}, variants=["titled_subtitled_music"],
        )


def test_translation_retains_source_intervals_and_pauses():
    segments = [
        {"start_sec": 0.449, "end_sec": 2.509, "text": "Почему не ты?"},
        {"start_sec": 3.018, "end_sec": 3.798, "text": "У тебя есть мозги."},
    ]
    cues = vl.translated_cues(segments, duration=14.8, language="ru")
    assert [(c["start_sec"], c["end_sec"], c["text"]) for c in cues] == [
        (s["start_sec"], s["end_sec"], s["text"]) for s in segments
    ]
    assert all("word_synced" not in c for c in cues)


@pytest.mark.parametrize("segments", [
    [], [{"start_sec": 0, "end_sec": 2, "text": "Why not you?"}],
    [{"start_sec": -1, "end_sec": 2, "text": "Почему?"}],
    [{"start_sec": 0, "end_sec": 20, "text": "Почему?"}],
    [{"start_sec": float("nan"), "end_sec": 2, "text": "Почему?"}],
    [{"start_sec": 0, "end_sec": 2, "text": "Почему?"},
     {"start_sec": 1, "end_sec": 3, "text": "Почему?"}],
])
def test_invalid_or_untranslated_captions_are_rejected(segments):
    with pytest.raises(ValueError):
        vl.translated_cues(segments, duration=14.8, language="ru")


@pytest.mark.parametrize("color", ["#ffc766", "#c72638"])
def test_localization_reuses_masters_without_music_or_audio_replacement(tmp_path, monkeypatch, color):
    clean, upscale = tmp_path / "clean.mp4", tmp_path / "upscale.mp4"
    clean.write_bytes(b"original clean")
    upscale.write_bytes(b"original 4k")
    monkeypatch.setattr(vl.pp, "post_production_artifacts_dir", lambda: tmp_path / "out")
    monkeypatch.setattr(vl.pp, "_media_duration", lambda path: 14.8)
    monkeypatch.setattr(vl.pp, "_video_dimensions", lambda path: (2160, 3840))
    monkeypatch.setattr(vl.pp, "_resolve_ffmpeg", lambda: "ffmpeg")
    ass_calls, render_calls = [], []
    def write_ass(path, cues, **kwargs):
        ass_calls.append((cues, kwargs))
        path.write_text("test")
    def render(**kwargs):
        render_calls.append(kwargs)
        kwargs["dest_path"].write_bytes(b"localized")
        return True
    monkeypatch.setattr(vl.pp, "_write_burned_subtitle_ass", write_ass)
    monkeypatch.setattr(vl.pp, "_render_subtitled_music_master", render)
    post = vl.build_localized_visualization(
        clean_path=clean, upscaled_path=upscale, language="ru",
        segments=[{"start_sec": .449, "end_sec": 2.509, "text": "Почему не ты?"}],
        duration=14.8, title="Почему не ты?", title_duration=3,
        variants=["clean", "upscaled_4k", "subtitled_music", "titled_subtitled_music"],
        title_style={"title_color": color, "top_margin_ratio": 0.06},
    )
    renders = post["renders_by_language"]["ru"]
    assert Path(renders["clean"]["video_path"]).read_bytes() == clean.read_bytes()
    assert Path(renders["upscaled_4k"]["video_path"]).read_bytes() == upscale.read_bytes()
    assert len(render_calls) == 2
    assert all(c["clean_path"] == upscale and c["music_path"] is None for c in render_calls)
    assert not ass_calls[0][1]["title_cues"]
    assert ass_calls[1][1]["title_cues"][0]["text"] == "Почему не ты?"
    assert ass_calls[1][1]["title_style"] == {"title_color": color, "top_margin_ratio": 0.06}
    assert post["title_overlay_style"] == ass_calls[1][1]["title_style"]
    assert post["title_overlay_by_language"]["ru"] == {
        "title": "Почему не ты?", "start_sec": 0, "end_sec": 3,
    }
    assert list(post["renders_by_language"]) == ["ru"]


def test_changed_master_duration_requires_review(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(vl.pp, "_media_duration", lambda path: 20)
    with pytest.raises(ValueError, match="duration changed"):
        vl.build_localized_visualization(
            clean_path=source, upscaled_path=source, language="ru",
            segments=[{"start_sec": 0, "end_sec": 1, "text": "Привет"}],
            duration=14.8, title="Привет", title_duration=3, variants=["clean"],
        )


def test_admin_localization_is_scoped_to_visualization_ru(tmp_path, monkeypatch):
    import os
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin_panel"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cf_admin.settings")
    import django
    django.setup()
    from pipeline_admin import admin

    source = tmp_path / "master.mp4"
    source.write_bytes(b"test")
    payload = {
        "source_language": "en", "source_artifact_ids": {"clean": "clean-id", "upscaled_4k": "4k-id"},
        "duration_sec": 14.8, "title_duration_sec": 3, "title": "Почему не ты?",
        "segments": [{"start_sec": .449, "end_sec": 2.509, "text": "Почему не ты?"}],
    }
    filters, gets, builds = [], [], []
    def filter_rows(**kwargs):
        filters.append(kwargs)
        return SimpleNamespace(order_by=lambda *args: SimpleNamespace(
            first=lambda: SimpleNamespace(payload_json=payload)))
    def get_row(**kwargs):
        gets.append(kwargs)
        return SimpleNamespace(uri=source.as_uri())
    monkeypatch.setattr(admin, "GenerationArtifact", SimpleNamespace(objects=SimpleNamespace(
        filter=filter_rows, get=get_row)))
    monkeypatch.setattr(vl, "build_localized_visualization", lambda **kw: builds.append(kw) or {"ok": True})
    run = SimpleNamespace(id="only-this-run")
    assert admin._build_visualization_translation_for_run(
        run, {"generation_mode": "story"}, language="ru", variants=["clean"],
    ) is None
    assert admin._build_visualization_translation_for_run(
        run, {"generation_mode": "visualization"}, language="en", variants=["clean"],
    ) is None
    assert not filters
    assert admin._build_visualization_translation_for_run(
        run, {"generation_mode": "visualization"}, language="ru", variants=["clean"],
    ) == {"ok": True}
    assert all(row["run_id"] == run.id and row["language"] == "en" for row in gets)
    assert [row["artifact_type"] for row in gets] == ["final_clean", "final_upscaled_4k"]
    assert builds[0]["segments"] == payload["segments"]
    assert builds[0]["title_style"] is None

    run.title_color = "#ffc766"
    session = {"generation_mode": "visualization", "production": {
        "overlay_title_style": {"title_color": "#fff7e8", "top_margin_ratio": 0.06},
    }}
    admin._build_visualization_translation_for_run(
        run, session, language="ru", variants=["titled_subtitled_music"],
    )
    assert builds[-1]["title_style"] == {"title_color": "#ffc766", "top_margin_ratio": 0.06}
