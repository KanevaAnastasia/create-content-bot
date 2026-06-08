import base64
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
    assert "Первая\\Nсцена" in ru_payload
    assert "Вторая\\Nсцена" in ru_payload
    assert "First\\Nscene line" in en_payload
    assert "Second\\Nline" in en_payload


def test_burned_subtitle_layout_prefers_one_word_top_and_two_or_three_bottom():
    assert post_production._subtitle_pages("для белого мусса нужен белый шоколад") == [
        ["для", "белого", "мусса", "нужен"],
        ["белый", "шоколад"],
    ]
    assert (
        post_production._format_burned_subtitle_block(["для", "белого", "мусса", "нужен"])
        == "для\nбелого мусса нужен"
    )
    assert post_production._format_burned_subtitle_block(["в", "дом", "и", "сад"]) == "в дом\nи сад"


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
