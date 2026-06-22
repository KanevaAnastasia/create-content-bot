import base64
import subprocess
from pathlib import Path

from app import post_production
from app.scene_media_candidates import register_video_candidate


def test_build_post_production_uses_selected_scene_videos_and_speed(monkeypatch):
    scenes = [
        {"duration_sec": 8, "text": "Первая сцена", "speech_by_lang": {"en": "First scene [пауза] line"}},
        {"duration_sec": 6, "text": "Вторая сцена", "speech_by_lang": {"en": "Second line"}},
    ]
    old_ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"old-1")
    new_ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"new-1")
    old_ru["liked"] = True
    old_ru["speed_coef"] = 2.0
    new_ru["speed_coef"] = 1.1
    second_ru = register_video_candidate(scenes[1], language="ru", video_bytes=b"scene-2")
    second_ru["approved_for_production"] = True
    second_ru["speed_coef"] = 1.5
    first_en = register_video_candidate(scenes[0], language="en", video_bytes=b"en-1")
    first_en["approved_for_production"] = True
    first_en["speed_coef"] = 1.25
    second_en = register_video_candidate(scenes[1], language="en", video_bytes=b"en-2")
    second_en["approved_for_production"] = True
    second_en["speed_coef"] = 1.5

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: ("/Users/anastasia/Downloads/Cooking-Show-chosic.com_.mp3", 0.18))

    def fake_normalize_segment(*, ffmpeg, src_path, dest_path, speed_coef):
        assert ffmpeg == "/usr/bin/ffmpeg"
        payload = Path(src_path).read_bytes()
        dest_path.write_bytes(payload + f"|{speed_coef}".encode())

    def fake_concat_segments(*, ffmpeg, list_path, dest_path):
        lines = Path(list_path).read_text(encoding="utf-8").splitlines()
        merged = b"".join(Path(line.split("'", 2)[1]).read_bytes() for line in lines if line)
        dest_path.write_bytes(merged)

    def fake_render_subtitled_music_master(*, ffmpeg, clean_path, subtitles_path, music_path, music_volume, dest_path):
        assert ffmpeg == "/usr/bin/ffmpeg"
        assert music_path == "/Users/anastasia/Downloads/Cooking-Show-chosic.com_.mp3"
        assert music_volume == 0.18
        dest_path.write_bytes(clean_path.read_bytes() + b"|subtitled|" + subtitles_path.read_bytes())

    monkeypatch.setattr(post_production, "_normalize_segment", fake_normalize_segment)
    monkeypatch.setattr(post_production, "_concat_segments", fake_concat_segments)
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", fake_render_subtitled_music_master)

    out = post_production.build_post_production(scenes, project_slug="grandma_kitchen")

    subtitles = out["subtitle_entries"]
    assert len(subtitles) == 2
    assert subtitles[0]["text"] == "Первая сцена"
    assert subtitles[0]["end_sec"] == 4.0
    assert subtitles[1]["start_sec"] == 4.0
    assert subtitles[1]["end_sec"] == 8.0
    assert out["final_clean_video"]["width"] == 1080
    assert out["final_clean_video"]["height"] == 1920
    assert out["final_subtitled_music_video"]["music_asset_uri"] == "/Users/anastasia/Downloads/Cooking-Show-chosic.com_.mp3"
    assert out["final_subtitled_music_video"]["source_audio_preserved"] is True
    assert sorted(out["languages"]) == ["en", "ru"]
    assert sorted(out["renders_by_language"]) == ["en", "ru"]
    assert out["subtitle_entries_by_language"]["en"][0]["text"] == "First scene line"
    ru_payload = base64.b64decode(out["final_subtitled_music_video"]["video_b64"]).decode("utf-8", errors="ignore")
    en_payload = base64.b64decode(out["renders_by_language"]["en"]["subtitled_music"]["video_b64"]).decode(
        "utf-8", errors="ignore"
    )
    assert "Style: CleanPremium,Inter SemiBold,86" in ru_payload
    assert "Comic Sans" not in ru_payload
    assert "BorderStyle=3" not in ru_payload
    assert "Первая сцена" in ru_payload
    assert "Вторая сцена" in ru_payload
    assert "First scene line" in en_payload
    assert "Second line" in en_payload


def test_burned_subtitle_layout_uses_clean_premium_two_line_blocks():
    pages = post_production._subtitle_pages(
        "для белого мусса нужен белый шоколад и немного холодных сливок"
    )
    assert pages == [
        ["для", "белого", "мусса", "нужен", "белый", "шоколад"],
        ["и", "немного", "холодных", "сливок"],
    ]
    first_block = post_production._format_burned_subtitle_block(pages[0])
    assert first_block == "для белого мусса\nнужен белый шоколад"
    assert len(first_block.splitlines()) <= 2
    assert post_production._format_burned_subtitle_block(["в", "дом", "и", "сад"]) == "в дом и сад"


def test_burned_subtitle_ass_style_adapts_to_video_shape(tmp_path):
    vertical = tmp_path / "vertical.ass"
    horizontal = tmp_path / "horizontal.ass"

    post_production._write_burned_subtitle_ass(vertical, [], video_width=1080, video_height=1920)
    post_production._write_burned_subtitle_ass(horizontal, [], video_width=1920, video_height=1080)

    vertical_text = vertical.read_text(encoding="utf-8")
    horizontal_text = horizontal.read_text(encoding="utf-8")
    assert "Style: CleanPremium,Inter SemiBold,86" in vertical_text
    assert "&H80000000,&H40000000" in vertical_text
    assert ",1,2,5,2,80,80,420,1" in vertical_text
    assert "Style: CleanPremium,Inter SemiBold,67" in horizontal_text
    assert "&H80000000,&H40000000" in horizontal_text
    assert ",1,2,5,2,80,80,145,1" in horizontal_text


def test_burned_subtitles_reveal_lines_progressively_with_clean_style():
    cues = post_production._build_burned_subtitle_cues(
        {"text": "для белого мусса нужен белый шоколад", "start_sec": 1.0, "end_sec": 4.0},
        video_width=1080,
        video_height=1920,
    )

    assert [cue["text"] for cue in cues] == [
        f"для белого мусса\n{post_production.SUBTITLE_ASS_INVISIBLE_TOKEN}",
        "для белого мусса\nнужен белый шоколад",
    ]
    assert [cue["start_sec"] for cue in cues] == [1.0, 2.5]
    assert [cue["end_sec"] for cue in cues] == [2.5, 4.0]


def test_build_post_production_prefers_language_specific_speech_cues(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6,
            "text": "Русская полная фраза",
            "speech_by_lang": {"en": "Long english full line"},
            "speech_cues_by_lang": {
                "ru": [{"text": "Русская", "start_sec": 0.0, "end_sec": 2.0}],
                "en": [
                    {"text": "Long english", "start_sec": 0.0, "end_sec": 1.5},
                    {"text": "full line", "start_sec": 1.5, "end_sec": 4.0},
                ],
            },
        }
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru")
    en = register_video_candidate(scenes[0], language="en", video_bytes=b"en")
    ru["approved_for_production"] = True
    en["approved_for_production"] = True

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes)

    assert [item["text"] for item in out["subtitle_entries_by_language"]["en"]] == ["Long english", "full line"]
    assert out["subtitle_entries_by_language"]["en"][1]["start_sec"] == 1.25
    assert out["speech_budget_by_language"]["en"] == []
    assert out["original_audio_preserved_by_language"]["en"] is True


def test_build_post_production_scales_speech_cues_with_video_speed(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 8,
            "speech_cues_by_lang": {
                "ru": [
                    {"text": "Первая часть", "start_sec": 0.0, "end_sec": 4.0},
                    {"text": "Вторая часть", "start_sec": 4.0, "end_sec": 8.0},
                ]
            },
        }
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru")
    ru["approved_for_production"] = True
    ru["speed_coef"] = 2.0

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes)

    subtitles = out["subtitle_entries_by_language"]["ru"]
    assert subtitles[0]["start_sec"] == 0.0
    assert subtitles[0]["end_sec"] == 2.0
    assert subtitles[1]["start_sec"] == 2.0
    assert subtitles[1]["end_sec"] == 4.0


def test_build_post_production_scales_subtitles_to_actual_normalized_duration(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 8,
            "speech_cues_by_lang": {
                "ru": [
                    {"text": "Первая часть", "start_sec": 0.0, "end_sec": 4.0},
                    {"text": "Вторая часть", "start_sec": 4.0, "end_sec": 8.0},
                ]
            },
        }
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru")
    ru["approved_for_production"] = True
    ru["speed_coef"] = 2.0

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 4.4)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes)

    subtitles = out["subtitle_entries_by_language"]["ru"]
    assert subtitles[0]["start_sec"] == 0.0
    assert subtitles[0]["end_sec"] == 2.2
    assert subtitles[1]["start_sec"] == 2.2
    assert subtitles[1]["end_sec"] == 4.4


def test_subtitle_text_removes_veo_stress_markers():
    assert post_production._normalize_subtitle_text("готОво [пауза] молоко́") == "готово молоко"
    assert post_production._normalize_subtitle_text("Мягкий бЕлый мусс") == "Мягкий белый мусс"


def test_render_subtitled_music_mix_keeps_source_audio_leading(monkeypatch, tmp_path):
    clean = tmp_path / "clean.mp4"
    subtitles = tmp_path / "subtitles.ass"
    music = tmp_path / "music.mp3"
    dest = tmp_path / "out.mp4"
    clean.write_bytes(b"video")
    subtitles.write_text("", encoding="utf-8")
    music.write_bytes(b"music")
    captured = {}

    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)

    def fake_run_ffmpeg(cmd, *, timeout, stage):
        captured["cmd"] = cmd
        captured["stage"] = stage
        dest.write_bytes(b"out")

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run_ffmpeg)

    assert post_production._render_subtitled_music_master(
        ffmpeg="/usr/bin/ffmpeg",
        clean_path=clean,
        subtitles_path=subtitles,
        music_path=str(music),
        music_volume=0.18,
        dest_path=dest,
    ) is True
    filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "subtitles=filename=" in filter_complex
    assert ":fontsdir=" in filter_complex
    assert "[0:a]volume=1.0000[voice]" in filter_complex
    assert "[1:a]volume=0.1274[music]" in filter_complex
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]" in filter_complex
    assert captured["stage"] == "render_subtitled_music_master_mix"


def test_has_audio_stream_falls_back_to_ffmpeg_input_probe(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    monkeypatch.setattr(post_production, "_resolve_ffprobe", lambda: None)
    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")

    def fake_run(cmd, *, capture_output, text, timeout):
        assert cmd == ["/usr/bin/ffmpeg", "-hide_banner", "-i", str(video)]
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr=(
                "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mp4':\n"
                "  Duration: 00:00:08.00, start: 0.000000, bitrate: 1891 kb/s\n"
                "  Stream #0:0[0x1](und): Video: h264, yuv420p, 720x1280, 24 fps\n"
                "  Stream #0:1[0x2](und): Audio: aac (LC), 48000 Hz, stereo\n"
                "At least one output file must be specified\n"
            ),
        )

    monkeypatch.setattr(post_production.subprocess, "run", fake_run)

    assert post_production._has_audio_stream(video) is True
    assert post_production._video_dimensions(video) == (720, 1280)
    assert post_production._media_duration(video) == 8.0


def test_artifact_names_are_readable():
    scenes = [{"text": "Русский текст сцены"}]

    key = post_production._make_artifact_bundle_key(scenes, project_slug="grandma_kitchen")
    filename = post_production._artifact_filename(
        project_slug="grandma_kitchen",
        kind="subtitled_music_master",
        language="ru",
    )

    assert "_grandma_kitchen_" in key
    assert filename == "grandma_kitchen_subtitled_music_master_ru.mp4"


def test_dense_subtitle_entry_is_split_into_subwindows():
    item = {
        "text": "для белого мусса нужен белый шоколад и немного холодных сливок",
        "start_sec": 0.0,
        "end_sec": 2.0,
    }
    out = post_production._expand_dense_subtitle_entry(item)
    assert len(out) == 2
    assert out[0]["text"] == "для белого мусса нужен белый шоколад"
    assert out[0]["start_sec"] == 0.0
    assert 0.0 < out[0]["end_sec"] < 2.0
    assert out[1]["text"] == "и немного холодных сливок"
    assert out[1]["start_sec"] == out[0]["end_sec"]
    assert out[1]["end_sec"] == 2.0
