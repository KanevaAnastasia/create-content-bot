import base64
import re
import subprocess
from pathlib import Path

from app import post_production
from app.scene_media_candidates import register_video_candidate


def _fake_upscale_master_to_4k(*, ffmpeg, src_path, dest_path):
    _ = ffmpeg
    dest_path.write_bytes(Path(src_path).read_bytes() + b"|upscaled_4k")


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
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
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
    assert out["final_upscaled_4k_video"]["width"] == 1080
    assert out["final_upscaled_4k_video"]["height"] == 1920
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
    assert "|upscaled_4k" in ru_payload
    assert "Comic Sans" not in ru_payload
    assert "BorderStyle=3" not in ru_payload
    assert "Первая сцена" in ru_payload
    assert "Вторая сцена" in ru_payload
    assert "First scene line" in en_payload
    assert "Second line" in en_payload


def test_build_post_production_adds_title_overlay_for_first_segment_duration(monkeypatch):
    scenes = [
        {"duration_sec": 8, "text": "Первая сцена", "speech_by_lang": {"ru": "Первая сцена", "en": "First scene"}},
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru-clip")
    en = register_video_candidate(scenes[0], language="en", video_bytes=b"en-clip")
    ru["approved_for_production"] = True
    en["approved_for_production"] = True
    ru["speed_coef"] = 2.0
    en["speed_coef"] = 2.0
    ass_by_name: dict[str, str] = {}

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)

    def fake_render_subtitled_music_master(*, ffmpeg, clean_path, subtitles_path, music_path, music_volume, dest_path):
        _ = (ffmpeg, clean_path, music_path, music_volume)
        ass_by_name[subtitles_path.name] = subtitles_path.read_text(encoding="utf-8")
        dest_path.write_bytes(subtitles_path.read_bytes())

    monkeypatch.setattr(post_production, "_render_subtitled_music_master", fake_render_subtitled_music_master)

    progress_events = []
    out = post_production.build_post_production(
        scenes,
        overlay_title_by_lang={"ru": "Борщ", "en": "Borscht"},
        overlay_title_style={"title_color": "#c72638", "top_margin_ratio": 0.15, "side_margin_ratio": 0.11},
        overlay_series_title_by_lang={"ru": "Супы у бабушки", "en": "Grandma's Soups"},
        overlay_series_counter_by_lang={"ru": "день 2 из 7", "en": "day 2 of 7"},
        progress_callback=progress_events.append,
    )

    assert "TitleOverlay" not in ass_by_name["subtitles_ru.ass"]
    assert "TitleOverlay" not in ass_by_name["subtitles_en.ass"]
    assert "Style: TitleOverlayRU,TD Afisha" in ass_by_name["title_overlay_ru.ass"]
    assert "Style: TitleOverlayEN,Taller" in ass_by_name["title_overlay_en.ass"]
    assert "Style: TitleSeriesRU,Berezka" in ass_by_name["title_overlay_ru.ass"]
    assert "Style: TitleSeriesEN,Ravsten Demo" in ass_by_name["title_overlay_en.ass"]
    assert "Style: TitleOverlayRU,TD Afisha,118,&H003826C7" in ass_by_name["title_overlay_ru.ass"]
    assert "&H00000000,&H00000000" in ass_by_name["title_overlay_ru.ass"]
    assert ",0,1,2,5,7," in ass_by_name["title_overlay_ru.ass"]
    assert ",0,1,1,3,7," in ass_by_name["title_overlay_ru.ass"]
    assert r"\fsp" not in ass_by_name["title_overlay_ru.ass"]
    assert re.search(r",118,118,\d+,1", ass_by_name["title_overlay_ru.ass"])
    assert "Dialogue: 1,0:00:00.00,0:00:04.00,TitleOverlayRU" in ass_by_name["title_overlay_ru.ass"]
    assert "Dialogue: 1,0:00:00.00,0:00:04.00,TitleOverlayEN" in ass_by_name["title_overlay_en.ass"]
    assert "Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesRU" in ass_by_name["title_overlay_ru.ass"]
    assert "Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesEN" in ass_by_name["title_overlay_en.ass"]
    assert "Борщ" in ass_by_name["title_overlay_ru.ass"]
    assert "Borscht" in ass_by_name["title_overlay_en.ass"]
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesRU,,0,0,0,,\{\\q2\\fscx\d+\\fs\d+\}супы\\hу\\hбабушки",
        ass_by_name["title_overlay_ru.ass"],
    )
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesRU,,0,0,0,,\{\\q2\\pos\(\d+,-?\d+\)\}\{\\fnMarianna\\fscx\d+\\fs\d+\}день\\h\{\\fs\d+\}2\{\\fs\d+\}\\hиз\\h7",
        ass_by_name["title_overlay_ru.ass"],
    )
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesEN,,0,0,0,,\{\\q2\\fscx\d+\\fs\d+\}Grandma's\\hSoups",
        ass_by_name["title_overlay_en.ass"],
    )
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesEN,,0,0,0,,\{\\q2\\pos\(\d+,-?\d+\)\}\{\\fnMarianna\\fscx\d+\\fs\d+\}day\\h2\\hof\\h7",
        ass_by_name["title_overlay_en.ass"],
    )
    assert out["title_overlay_by_language"]["ru"] == {"title": "Борщ", "start_sec": 0.0, "end_sec": 4.0}
    assert out["title_overlay_by_language"]["en"] == {"title": "Borscht", "start_sec": 0.0, "end_sec": 4.0}
    assert out["series_overlay_by_language"]["ru"] == {
        "series_title": "Супы у бабушки",
        "counter": "день 2 из 7",
        "start_sec": 0.0,
        "end_sec": 4.0,
    }
    assert out["series_overlay_by_language"]["en"] == {
        "series_title": "Grandma's Soups",
        "counter": "day 2 of 7",
        "start_sec": 0.0,
        "end_sec": 4.0,
    }
    assert "titled_subtitled_music" in out["renders_by_language"]["ru"]
    assert out["final_titled_subtitled_music_video"] == out["renders_by_language"]["ru"]["titled_subtitled_music"]
    ru_stages = [item["stage"] for item in progress_events if item["language"] == "ru"]
    assert ru_stages[:4] == ["normalizing", "clean_building", "clean_ready", "upscale_building"]
    assert "subtitles_building" in ru_stages
    assert "title_building" in ru_stages
    assert ru_stages[-1] == "persisting"


def test_title_overlay_ass_defines_style_for_non_default_language(tmp_path):
    ass_path = tmp_path / "subtitles_with_title_es.ass"

    post_production._write_burned_subtitle_ass(
        ass_path,
        [],
        title_cues=[
            {
                "text": "Sopa de la abuela",
                "start_sec": 0.0,
                "end_sec": 6.0,
                "style": "TitleOverlayES",
                "language": "es",
            }
        ],
        video_width=1080,
        video_height=1920,
    )

    payload = ass_path.read_text(encoding="utf-8")
    assert "Style: TitleOverlayES,TD Afisha" in payload
    assert "Dialogue: 1,0:00:00.00,0:00:06.00,TitleOverlayES" in payload


def test_series_overlay_keeps_day_near_series_size_without_wrapping():
    layout = post_production._subtitle_layout_for_dimensions(1080, 1920)
    max_safe_width = 1080 - (108 * 2)

    for language, series_title, counter in (
        ("ru", "летние напитки", "день 8 из 10"),
        ("en", "Summer drinks", "day 8 of 10"),
    ):
        fit = post_production._fit_title_series_ass_text(
            series_title,
            counter,
            layout=layout,
            language=language,
            side_margin_ratio=0.10,
        )

        assert fit is not None
        assert "\n" not in fit.text
        assert fit.width <= max_safe_width
        assert fit.series_width == round(1080 * post_production._title_series_width_ratio(language))
        assert fit.counter_width == round(1080 * post_production.TITLE_SERIES_DAY_WIDTH_RATIO)
        assert post_production._title_series_day_start_ratio(language, side_margin_ratio=0.10) == 0.70
        assert fit.counter_font_size >= int(fit.series_font_size * 0.85)
        if language == "ru":
            assert fit.scale_x == post_production.TITLE_SERIES_ASS_SCALE_X_BY_LANGUAGE["ru"]
            assert fit.counter_scale_x == post_production.TITLE_SERIES_DAY_ASS_SCALE_X_BY_LANGUAGE["ru"]
        else:
            assert fit.scale_x == 100
            assert fit.counter_scale_x == post_production.TITLE_SERIES_DAY_ASS_SCALE_X_BY_LANGUAGE["en"]


def test_series_overlay_fit_scales_for_4k_title_overlay_canvas():
    layout = post_production._subtitle_layout_for_dimensions(2160, 3840)
    max_safe_width = 2160 - (216 * 2)

    fit = post_production._fit_title_series_ass_text(
        "летние напитки",
        "день 8 из 10",
        layout=layout,
        language="ru",
        side_margin_ratio=0.10,
    )

    assert fit is not None
    assert fit.width <= max_safe_width
    assert fit.series_width == round(2160 * post_production._title_series_width_ratio("ru"))
    assert fit.counter_width == round(2160 * post_production.TITLE_SERIES_DAY_WIDTH_RATIO)
    assert fit.width == fit.series_width + fit.counter_width
    assert fit.series_font_size > 63
    assert post_production._title_series_day_start_ratio("ru", side_margin_ratio=0.10) == 0.70


def test_ru_series_overlay_normalizes_uppercase_for_berezka_glyphs():
    layout = post_production._subtitle_layout_for_dimensions(1080, 1920)

    formatted = post_production._format_title_series_ass_text(
        "ЛЕТНИЕ НАПИТКИ",
        "день 8 из 10",
        layout=layout,
        language="ru",
    )

    assert "летние\\hнапитки" in formatted
    assert "ЛЕТНИЕ" not in formatted


def test_build_titled_post_production_reuses_existing_subtitled_master(tmp_path, monkeypatch):
    source = tmp_path / "final_subtitled_music_master_ru.mp4"
    source.write_bytes(b"subtitled-source")
    rendered_inputs = []
    progress_events = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 12.0)
    monkeypatch.setattr(post_production, "_persist_render_artifact", lambda path, **_kwargs: str(path))

    def fake_render_subtitled_music_master(*, ffmpeg, clean_path, subtitles_path, music_path, music_volume, dest_path):
        rendered_inputs.append((ffmpeg, Path(clean_path), music_path, music_volume))
        dest_path.write_bytes(Path(clean_path).read_bytes() + b"|title|" + Path(subtitles_path).read_bytes())
        return True

    monkeypatch.setattr(post_production, "_render_subtitled_music_master", fake_render_subtitled_music_master)
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no normalize")))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no upscale")))

    out = post_production.build_titled_post_production_from_subtitled_master(
        [{"duration_sec": 8, "text": "Первая сцена"}],
        subtitled_video_path=source,
        language="ru",
        overlay_title_by_lang={"ru": "Борщ"},
        overlay_series_title_by_lang={"ru": "Супы у бабушки"},
        overlay_series_counter_by_lang={"ru": "день 2 из 7"},
        progress_callback=progress_events.append,
    )

    assert rendered_inputs == [("/usr/bin/ffmpeg", source, None, 0.0)]
    payload = out["renders_by_language"]["ru"]["titled_subtitled_music"]
    assert payload["source_variant"] == "subtitled_music"
    assert payload["source_video_path"] == str(source)
    assert [item["stage"] for item in progress_events] == ["reuse_subtitled", "title_building", "title_ready", "persisting"]


def test_build_upscaled_post_production_reuses_existing_clean_master(tmp_path, monkeypatch):
    source = tmp_path / "final_clean_master_ru.mp4"
    source.write_bytes(b"clean-source")
    upscaled_inputs = []
    progress_events = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (2160, 3840))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_persist_render_artifact", lambda path, **_kwargs: str(path))

    def fake_upscale(*, ffmpeg, src_path, dest_path):
        upscaled_inputs.append((ffmpeg, Path(src_path)))
        dest_path.write_bytes(Path(src_path).read_bytes() + b"|4k")

    monkeypatch.setattr(post_production, "_upscale_master_to_4k", fake_upscale)

    out = post_production.build_upscaled_post_production_from_clean_master(
        clean_video_path=source,
        language="ru",
        progress_callback=progress_events.append,
    )

    assert upscaled_inputs == [("/usr/bin/ffmpeg", source)]
    payload = out["renders_by_language"]["ru"]["upscaled_4k"]
    assert payload["source_variant"] == "clean"
    assert payload["source_video_path"] == str(source)
    assert [item["stage"] for item in progress_events] == ["reuse_clean", "upscale_building", "upscale_ready", "persisting"]


def test_build_subtitled_post_production_reuses_existing_upscaled_master(tmp_path, monkeypatch):
    source = tmp_path / "final_upscaled_4k_master_ru.mp4"
    source.write_bytes(b"upscaled-source")
    rendered_inputs = []
    progress_events = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (2160, 3840))
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: ("/tmp/music.mp3", 0.2))
    monkeypatch.setattr(post_production, "_persist_render_artifact", lambda path, **_kwargs: str(path))

    def fake_render(*, ffmpeg, clean_path, subtitles_path, music_path, music_volume, dest_path):
        rendered_inputs.append((ffmpeg, Path(clean_path), music_path, music_volume))
        dest_path.write_bytes(Path(clean_path).read_bytes() + b"|subs|" + Path(subtitles_path).read_bytes())
        return True

    monkeypatch.setattr(post_production, "_render_subtitled_music_master", fake_render)
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no normalize")))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no upscale")))

    out = post_production.build_subtitled_post_production_from_upscaled_master(
        [{"duration_sec": 8, "text": "Первая сцена"}],
        upscaled_video_path=source,
        language="ru",
        progress_callback=progress_events.append,
    )

    assert rendered_inputs == [("/usr/bin/ffmpeg", source, "/tmp/music.mp3", 0.2)]
    payload = out["renders_by_language"]["ru"]["subtitled_music"]
    assert payload["source_variant"] == "upscaled_4k"
    assert payload["source_video_path"] == str(source)
    assert [item["stage"] for item in progress_events] == ["reuse_upscaled", "subtitles_building", "subtitles_ready", "persisting"]


def test_title_overlay_helpers_cover_fallbacks_and_invalid_style_values(tmp_path):
    ass_path = tmp_path / "subtitles_with_title.ass"

    assert post_production._overlay_title_for_language({"ru": "Борщ"}, "en") == "Борщ"
    assert post_production._overlay_title_for_language({"en": "Borscht"}, "ru") == "Borscht"
    assert post_production._build_title_overlay_cue({"ru": "Борщ"}, language="ru", duration_sec=0) is None
    assert post_production._build_title_overlay_cue({"ru": "  "}, language="ru", duration_sec=6) is None

    post_production._write_burned_subtitle_ass(
        ass_path,
        [],
        title_cues=[
            {
                "text": "Очень длинный заголовок который нужно аккуратно переносить",
                "start_sec": 0.0,
                "end_sec": 6.0,
                "style": "TitleOverlayRU",
                "language": "ru",
            }
        ],
        title_style={"title_color": "not-a-color", "top_margin_ratio": 99, "side_margin_ratio": 0.01},
        video_width=1080,
        video_height=1920,
    )

    payload = ass_path.read_text(encoding="utf-8")
    assert "Style: TitleOverlayRU,TD Afisha,118,&H00FFFFFF" in payload
    assert ",64,64,422,1" in payload
    match = re.search(r"\\fs(\d+)", payload)
    assert match is not None
    assert int(match.group(1)) <= post_production.TITLE_OVERLAY_VERTICAL_FONT_SIZE


def test_title_overlay_ass_style_defaults_to_five_percent_top_margin(tmp_path):
    ass_path = tmp_path / "default_title.ass"

    post_production._write_burned_subtitle_ass(
        ass_path,
        [],
        title_cues=[
            {
                "text": "Борщ",
                "start_sec": 0.0,
                "end_sec": 6.0,
                "style": "TitleOverlayRU",
                "language": "ru",
            }
        ],
        title_style={},
        video_width=1080,
        video_height=1920,
    )

    payload = ass_path.read_text(encoding="utf-8")
    assert ",108,108,96,1" in payload


def test_title_overlay_fit_caps_one_line_title_height_to_eight_percent():
    layout = post_production._subtitle_layout_for_dimensions(1080, 1920)
    fit = post_production._fit_title_overlay_ass_text(
        "Клубничный лимонад",
        layout=layout,
        language="ru",
        side_margin_ratio=0.10,
        top_margin_ratio=0.05,
        has_series_cue=True,
    )

    assert fit is not None
    assert "\n" not in fit.text
    max_safe_width = int(
        (
            1080
            - (108 * 2)
            - post_production.TITLE_OVERLAY_SHADOW
            - (post_production.TITLE_OVERLAY_OUTLINE * 2)
        )
        * post_production.TITLE_OVERLAY_ASS_SAFE_WIDTH_RATIO
    )
    max_safe_height = int(1920 * post_production.TITLE_OVERLAY_MAX_HEIGHT_RATIO)
    assert fit.width <= max_safe_width
    assert fit.height <= max_safe_height
    assert fit.font_size > post_production.TITLE_OVERLAY_VERTICAL_FONT_SIZE


def test_title_overlay_ass_uses_fitted_event_font_size(tmp_path):
    ass_path = tmp_path / "fitted_title.ass"

    post_production._write_burned_subtitle_ass(
        ass_path,
        [],
        title_cues=[
            {
                "text": "Клубничный лимонад",
                "start_sec": 0.0,
                "end_sec": 6.0,
                "style": "TitleOverlayRU",
                "language": "ru",
            }
        ],
        title_style={"side_margin_ratio": 0.10, "top_margin_ratio": 0.05},
        video_width=1080,
        video_height=1920,
    )

    payload = ass_path.read_text(encoding="utf-8")
    match = re.search(r"\\fs(\d+)", payload)
    assert match is not None
    assert int(match.group(1)) > post_production.TITLE_OVERLAY_VERTICAL_FONT_SIZE
    assert rf"\fscx{post_production.TITLE_OVERLAY_RU_SCALE_X}" in payload
    assert r"Клубничный\hлимонад" in payload
    assert r"\h\h\h\h" not in payload
    assert r"\N" not in payload


def test_run_ffmpeg_timeout_raises_user_visible_runtime_error(monkeypatch):
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=7)

    monkeypatch.setattr(post_production.subprocess, "run", _timeout)

    try:
        post_production._run_ffmpeg(["ffmpeg", "-i", "in.mp4"], timeout=7, stage="render")
    except RuntimeError as exc:
        assert "post_production timeout at ffmpeg:render after 7s" in str(exc)
    else:
        raise AssertionError("RuntimeError was not raised")


def test_upscale_master_to_4k_uses_long_timeout(monkeypatch, tmp_path):
    src = tmp_path / "in.mp4"
    dest = tmp_path / "out.mp4"
    src.write_bytes(b"video")
    captured = {}

    def fake_run_ffmpeg(cmd, *, timeout, stage):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        captured["stage"] = stage
        dest.write_bytes(b"out")

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run_ffmpeg)

    post_production._upscale_master_to_4k(ffmpeg="/usr/bin/ffmpeg", src_path=src, dest_path=dest)

    assert captured["stage"] == "upscale_master_to_4k"
    assert captured["timeout"] == post_production.FFMPEG_UPSCALE_TIMEOUT_SEC
    assert "2160:3840" in captured["cmd"][captured["cmd"].index("-vf") + 1]


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
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
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
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
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
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
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
        captured["timeout"] = timeout
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
    assert captured["timeout"] == post_production.FFMPEG_UPSCALE_TIMEOUT_SEC


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
