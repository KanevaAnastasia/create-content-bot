import base64
import re
import subprocess
from pathlib import Path

from app import post_production
from app.scene_media_candidates import register_video_candidate


def _fake_upscale_master_to_4k(*, ffmpeg, src_path, dest_path):
    _ = ffmpeg
    dest_path.write_bytes(Path(src_path).read_bytes() + b"|upscaled_4k")


def _character_alignment(text: str, *, step: float = 0.1) -> dict[str, list]:
    return {
        "characters": list(text),
        "character_start_times_seconds": [index * step for index in range(len(text))],
        "character_end_times_seconds": [(index + 1) * step for index in range(len(text))],
    }


def test_story_word_synced_subtitles_follow_exact_alignment_and_preserve_case():
    scenes = [
        {"scene": 1, "timeline_duration_sec": 1.0},
        {"scene": 2, "timeline_duration_sec": 1.0},
    ]
    spoken = "Я читаю. Книги здесь."

    post_production.apply_story_word_synced_subtitles(
        scenes,
        project_slug="discipline",
        voiceover_by_lang={"ru": spoken},
        alignment_by_lang={"ru": _character_alignment(spoken)},
        duration_by_lang={"ru": 2.2},
    )

    assert scenes[0]["speech_by_lang"]["ru"] == "Я читаю."
    assert scenes[1]["speech_by_lang"]["ru"] == "Книги здесь."
    assert scenes[0]["speech_cues_by_lang"]["ru"] == [
        {
            "text": "Я",
            "start_sec": 0.0,
            "end_sec": 0.2,
            "word_synced": True,
            "preserve_case": True,
            "fixed_block_words": ["Я", "читаю."],
            "visible_word_count": 1,
            "story_style_version": "metel_fixed_blocks_v2",
            "story_timing_source": "elevenlabs_alignment",
        },
        {
            "text": "Я читаю.",
            "start_sec": 0.2,
            "end_sec": 1.1,
            "word_synced": True,
            "preserve_case": True,
            "fixed_block_words": ["Я", "читаю."],
            "visible_word_count": 2,
            "story_style_version": "metel_fixed_blocks_v2",
            "story_timing_source": "elevenlabs_alignment",
        },
    ]
    assert scenes[1]["speech_cues_by_lang"]["ru"][0]["text"] == "Книги"
    assert scenes[1]["word_synced_subtitle_languages"] == ["ru"]


def test_story_word_synced_subtitles_recovers_legacy_story_without_saved_alignment():
    scenes = [
        {"duration_sec": 2.0, "speech_by_lang": {"ru": "Первое точное предложение."}},
        {"duration_sec": 2.0, "speech_by_lang": {"ru": "Второе предложение."}},
    ]

    post_production.apply_story_word_synced_subtitles(
        scenes,
        project_slug="grandma_kitchen",
        voiceover_by_lang={"ru": "Первое точное предложение. Второе предложение."},
        alignment_by_lang={},
        duration_by_lang={"ru": 4.0},
    )

    first = scenes[0]["speech_cues_by_lang"]["ru"]
    assert [cue["text"] for cue in first] == ["Первое", "Первое точное", "Первое точное предложение."]
    assert all(cue["word_synced"] is True for cue in first)
    assert all(cue["story_timing_source"] == "estimated_legacy_scene" for cue in first)


def test_story_word_synced_subtitles_recovers_legacy_language_from_voiceover_text():
    scenes = [{"duration_sec": 2.0}, {"duration_sec": 2.0}]

    post_production.apply_story_word_synced_subtitles(
        scenes,
        project_slug="grandma_kitchen",
        voiceover_by_lang={"en": "First exact sentence. Second exact sentence."},
        alignment_by_lang={},
        duration_by_lang={"en": 4.0},
    )

    recovered = " ".join(scene["speech_by_lang"]["en"] for scene in scenes)
    assert recovered == "First exact sentence. Second exact sentence."
    assert scenes[0]["speech_cues_by_lang"]["en"][0]["text"] == "First"


def test_story_word_synced_subtitles_uses_voiceover_when_visual_clips_duplicate_a_beat():
    scenes = [
        {"duration_sec": 2.0, "speech_by_lang": {"ru": "Первая фраза."}},
        {"duration_sec": 2.0, "speech_by_lang": {"ru": "Первая фраза."}},
        {"duration_sec": 2.0, "speech_by_lang": {"ru": "Вторая фраза."}},
    ]

    post_production.apply_story_word_synced_subtitles(
        scenes,
        project_slug="grandma_kitchen",
        voiceover_by_lang={"ru": "Первая фраза. Вторая фраза."},
        alignment_by_lang={},
        duration_by_lang={"ru": 6.0},
    )

    recovered = " ".join(scene["speech_by_lang"]["ru"] for scene in scenes)
    assert recovered == "Первая фраза. Вторая фраза."
    cues = [cue for scene in scenes for cue in scene["speech_cues_by_lang"]["ru"]]
    assert cues
    assert all(cue["story_timing_source"] == "estimated_legacy_voiceover" for cue in cues)


def test_all_modes_word_synced_subtitles_follow_transcribed_audio_timestamps():
    cues = post_production.build_word_synced_subtitle_cues(
        [
            {"word": "Готовим", "start_sec": 0.42, "end_sec": 0.91},
            {"word": "крем.", "start_sec": 1.08, "end_sec": 1.55},
        ],
        timing_source="video_audio_transcription",
    )
    scene = {
        "scene": 1,
        "generation_mode": "normal",
        "speech_cues_by_lang": {"ru": cues},
    }

    entries = post_production._scene_subtitle_entries(
        scene,
        "ru",
        start_offset=3.0,
        fallback_duration=6.0,
        speed_coef=1.0,
    )

    assert [entry["text"] for entry in entries] == ["Готовим", "Готовим крем."]
    assert entries[0]["start_sec"] == 3.42
    assert entries[0]["end_sec"] == 4.08
    assert entries[1]["start_sec"] == 4.08
    assert entries[1]["end_sec"] == 4.55
    assert all(entry["word_synced"] is True for entry in entries)


def test_guest_mode_has_no_subtitles():
    scene = {
        "scene": 1,
        "guest_mode": True,
        "speech_cues_by_lang": {
            "ru": [{"text": "Гостевой текст целиком", "start_sec": 0.4, "end_sec": 2.2}]
        },
    }

    entries = post_production._scene_subtitle_entries(
        scene,
        "ru",
        start_offset=0.0,
        fallback_duration=4.0,
        speed_coef=1.0,
    )

    assert entries == []


def test_word_synced_ass_uses_spoken_case_and_cream_colour(tmp_path):
    target = tmp_path / "subtitles.ass"

    post_production._write_burned_subtitle_ass(
        target,
        [
            {
                "text": "Я",
                "start_sec": 0.0,
                "end_sec": 0.5,
                "word_synced": True,
                "preserve_case": True,
                "fixed_block_words": ["Я", "читаю", "книги"],
                "visible_word_count": 1,
            },
            {
                "text": "Я читаю",
                "start_sec": 0.5,
                "end_sec": 1.0,
                "word_synced": True,
                "preserve_case": True,
                "fixed_block_words": ["Я", "читаю", "книги"],
                "visible_word_count": 2,
            },
        ],
        video_width=1080,
        video_height=1920,
    )

    payload = target.read_text(encoding="utf-8")
    assert r"\fnZyruaCitrus\fs66\b1" in payload
    assert payload.count(r"\pos(540,1555)") == 2
    assert r"Я {\alpha&HFF&}читаю книги{\alpha&H00&}" in payload
    assert r"Я читаю {\alpha&HFF&}книги{\alpha&H00&}" in payload
    assert "Я ЧИТАЮ" not in payload
    assert r"\c&H00D8EBF3&" in payload
    assert r"\3c&H00000000&\4c&H70000000&\bord3\shad2" in payload


def test_word_synced_fixed_block_is_not_split_by_dense_subtitle_fallback():
    entry = {
        "text": "Волшебное сливочное пиво прямо",
        "start_sec": 1.6,
        "end_sec": 1.9,
        "word_synced": True,
        "fixed_block_words": ["Волшебное", "сливочное", "пиво", "прямо"],
        "visible_word_count": 4,
    }

    assert post_production._expand_dense_subtitle_entry(entry) == [entry]


def test_story_series_title_stack_is_centered_in_three_rows(tmp_path):
    target = tmp_path / "story-title.ass"
    series_cue = post_production._build_title_series_overlay_cue(
        {"ru": "Истории любимых блюд"},
        {"ru": "история 8 из 10"},
        language="ru",
        duration_sec=3.0,
    )
    title_cue = post_production._build_title_overlay_cue(
        {"ru": "ОЛИВЬЕ"},
        language="ru",
        duration_sec=3.0,
    )

    assert series_cue is not None
    assert series_cue["layout_variant"] == "story"
    post_production._write_burned_subtitle_ass(
        target,
        [],
        title_cues=[series_cue, title_cue],
        title_style={"title_color": "#ff1a12"},
        video_width=1080,
        video_height=1920,
    )

    payload = target.read_text(encoding="utf-8")
    series_lines = [line for line in payload.splitlines() if ",TitleSeriesRU," in line]
    title_lines = [line for line in payload.splitlines() if ",TitleOverlayRU," in line]
    assert len(series_lines) == 2
    assert all(r"\pos(540," in line for line in series_lines)
    assert any(r"\an8\pos(540," in line for line in title_lines)
    assert "история" in payload
    assert "ОЛИВЬЕ" in payload


def test_story_caption_blocks_never_exceed_four_words():
    rows = [(word, index * 0.2, (index + 1) * 0.2) for index, word in enumerate(
        ["один", "два", "три", "четыре", "пять", "шесть.", "семь"]
    )]

    blocks = post_production._story_caption_word_blocks(rows)

    assert [[word for word, _start, _end in block] for block in blocks] == [
        ["один", "два", "три", "четыре"],
        ["пять", "шесть."],
        ["семь"],
    ]


def test_replace_master_audio_uses_approved_narration_and_exact_video_duration(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 57.0)

    def fake_run_ffmpeg(cmd, *, timeout, stage):
        captured.update(cmd=cmd, timeout=timeout, stage=stage)

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run_ffmpeg)
    video_path = tmp_path / "silent.mp4"
    narration_path = tmp_path / "approved.mp3"
    dest_path = tmp_path / "narrated.mp4"
    video_path.write_bytes(b"video")
    narration_path.write_bytes(b"voice")

    post_production._replace_master_audio_with_narration(
        ffmpeg="ffmpeg",
        video_path=video_path,
        narration_path=narration_path,
        dest_path=dest_path,
    )

    cmd = captured["cmd"]
    assert str(narration_path) in cmd
    assert "[a]" in cmd
    assert any("apad=whole_dur=57.000000" in item for item in cmd)
    assert captured["stage"] == "replace_master_audio_with_narration"


def test_replace_master_audio_can_boost_anastasia_narration_safely(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(
        post_production,
        "_run_ffmpeg",
        lambda cmd, *, timeout, stage: captured.update(cmd=cmd, timeout=timeout, stage=stage),
    )
    video = tmp_path / "video.mp4"
    narration = tmp_path / "anastasia.mp3"
    target = tmp_path / "out.mp4"
    video.write_bytes(b"video")
    narration.write_bytes(b"voice")

    post_production._replace_master_audio_with_narration(
        ffmpeg="ffmpeg",
        video_path=video,
        narration_path=narration,
        dest_path=target,
        narration_volume=post_production.DISCIPLINE_ANASTASIA_NARRATION_VOLUME,
    )

    filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "[1:a]volume=2.0000,alimiter=limit=0.97" in filter_complex


def test_replace_master_audio_keeps_discipline_intro_before_story_narration(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    master = tmp_path / "master.mp4"
    intro = tmp_path / "intro.mp4"
    narration = tmp_path / "story.mp3"
    target = tmp_path / "joined.mp4"
    for path in (master, intro, narration):
        path.write_bytes(b"media")

    monkeypatch.setattr(
        post_production,
        "_media_duration",
        lambda path: 68.0 if Path(path) == master else 8.0,
    )

    def fake_run(cmd, *, timeout, stage):
        captured.update(cmd=cmd, timeout=timeout, stage=stage)

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run)

    post_production._replace_master_audio_with_intro_and_narration(
        ffmpeg="ffmpeg",
        video_path=master,
        intro_video_path=intro,
        narration_path=narration,
        dest_path=target,
    )

    cmd = captured["cmd"]
    assert str(intro) in cmd
    assert str(narration) in cmd
    assert any("[intro][story]concat=n=2" in item for item in cmd)
    assert any("apad=whole_dur=68.000000" in item for item in cmd)
    assert captured["stage"] == "replace_master_audio_with_intro_and_narration"


def test_proportional_story_durations_match_shorter_narration_without_drift():
    durations = post_production._proportional_frame_aligned_durations(
        [3.0, 3.0, 3.0, 1.56],
        9.79,
        fps=30,
    )

    assert len(durations) == 4
    assert abs(sum(durations) - round(9.79 * 30) / 30) < 0.0001
    assert all(value < source for value, source in zip(durations, [3.0, 3.0, 3.0, 1.56]))


def test_all_english_narrated_workflows_trim_each_internal_clip(monkeypatch, tmp_path):
    scenes = [
        {"scene": 1, "duration_sec": 3.0, "speech_by_lang": {"en": "First idea"}},
        {"scene": 2, "duration_sec": 3.0, "speech_by_lang": {"en": "Second idea"}},
    ]
    for index, scene in enumerate(scenes, start=1):
        candidate = register_video_candidate(scene, language="en", video_bytes=f"clip-{index}".encode())
        candidate["approved_for_production"] = True
        candidate["speed_coef"] = 1.0

    narration_path = tmp_path / "english.mp3"
    narration_path.write_bytes(b"approved-english-audio")
    normalized_durations: list[float] = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: False)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(
        post_production,
        "_media_duration",
        lambda path: 4.0 if Path(path) == narration_path else None,
    )

    def fake_normalize(**kwargs):
        normalized_durations.append(float(kwargs["target_duration_sec"]))
        kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes())

    monkeypatch.setattr(post_production, "_normalize_segment", fake_normalize)
    monkeypatch.setattr(
        post_production,
        "_concat_segments",
        lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"),
    )
    monkeypatch.setattr(
        post_production,
        "_replace_master_audio_with_narration",
        lambda **kwargs: kwargs["dest_path"].write_bytes(b"narrated"),
    )

    post_production.build_post_production(
        scenes,
        target_languages=["en"],
        target_variants=["clean"],
        narration_audio_path_by_lang={"en": narration_path},
    )

    assert normalized_durations == [2.0, 2.0]


def test_speech_text_from_prompt_stops_before_inline_instruction_blocks():
    prompt = (
        "lang:ru Duration: 6 seconds. CHARACTERS SPEAKS: нежное тесто, "
        "хрустящая посыпка, каждый кусочек. voice_lock_full_grandma: female "
        "voice Russian language. speech_timing_plan: - 0.00-6.00s: нежное тесто. "
        "Render this video in vertical portrait 9:16 aspect ratio."
    )

    assert post_production._speech_text_from_prompt(prompt) == (
        "нежное тесто, хрустящая посыпка, каждый кусочек."
    )


def test_build_post_production_uses_horizontal_dimensions_for_long_mode(monkeypatch):
    scenes = [
        {
            "duration_sec": 8,
            "aspect_ratio": "16:9",
            "text": "Длинная сцена",
            "speech_by_lang": {"ru": "Длинная сцена"},
        }
    ]
    candidate = register_video_candidate(scenes[0], language="ru", video_bytes=b"scene-1")
    candidate["approved_for_production"] = True

    normalize_calls: list[tuple[int, int]] = []
    upscale_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1920, 1080))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: ("", 0.0))

    def fake_normalize_segment(*, ffmpeg, src_path, dest_path, speed_coef, target_width, target_height):
        _ = (ffmpeg, speed_coef)
        normalize_calls.append((target_width, target_height))
        dest_path.write_bytes(Path(src_path).read_bytes())

    def fake_concat_segments(*, ffmpeg, list_path, dest_path):
        _ = ffmpeg
        source = Path(Path(list_path).read_text(encoding="utf-8").split("'", 2)[1])
        dest_path.write_bytes(source.read_bytes())

    def fake_upscale(*, ffmpeg, src_path, dest_path, target_width, target_height):
        _ = ffmpeg
        upscale_calls.append((target_width, target_height))
        dest_path.write_bytes(Path(src_path).read_bytes() + b"|upscaled")

    monkeypatch.setattr(post_production, "_normalize_segment", fake_normalize_segment)
    monkeypatch.setattr(post_production, "_concat_segments", fake_concat_segments)
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", fake_upscale)

    out = post_production.build_post_production(
        scenes,
        target_languages=["ru"],
        target_variants=["clean", "upscaled_4k"],
    )

    assert normalize_calls == [(1920, 1080)]
    assert upscale_calls == [(3840, 2160)]
    assert out["final_clean_video"]["width"] == 1920
    assert out["final_upscaled_4k_video"]["height"] == 1080


def test_build_post_production_uses_selected_scene_videos_and_speed(monkeypatch):
    scenes = [
        {"duration_sec": 8, "text": "Первая сцена", "speech_by_lang": {"en": "First scene [пауза] line"}},
        {"duration_sec": 6, "text": "Вторая сцена", "speech_by_lang": {"en": "Second line"}},
    ]
    old_ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"old-1")
    new_ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"new-1")
    old_ru["liked"] = True
    old_ru["approved_for_production"] = True
    old_ru["speed_coef"] = 2.0
    new_ru["approved_for_production"] = True
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
    assert [item["text"] for item in subtitles] == ["Первая", "Первая сцена", "Вторая", "Вторая сцена"]
    assert all(item["word_synced"] is True for item in subtitles)
    assert subtitles[1]["end_sec"] == 8 / 1.1
    assert subtitles[2]["start_sec"] == 8 / 1.1
    assert subtitles[-1]["end_sec"] == (8 / 1.1) + 4.0
    assert out["final_clean_video"]["width"] == 1080
    assert out["final_clean_video"]["height"] == 1920
    assert out["final_upscaled_4k_video"]["width"] == 1080
    assert out["final_upscaled_4k_video"]["height"] == 1920
    assert out["final_subtitled_music_video"]["music_asset_uri"] == "/Users/anastasia/Downloads/Cooking-Show-chosic.com_.mp3"
    assert out["final_subtitled_music_video"]["source_audio_preserved"] is True
    assert sorted(out["languages"]) == ["en", "ru"]
    assert sorted(out["renders_by_language"]) == ["en", "ru"]
    assert [item["text"] for item in out["subtitle_entries_by_language"]["en"]] == [
        "First",
        "First scene",
        "line",
        "Second",
        "Second line",
    ]
    ru_payload = base64.b64decode(out["final_subtitled_music_video"]["video_b64"]).decode("utf-8", errors="ignore")
    en_payload = base64.b64decode(out["renders_by_language"]["en"]["subtitled_music"]["video_b64"]).decode(
        "utf-8", errors="ignore"
    )
    assert f"Style: CleanPremium,{post_production._subtitle_font_name()},88" in ru_payload
    assert "new-1|1.1" in ru_payload
    assert "old-1|2.0" not in ru_payload
    assert "|upscaled_4k" in ru_payload
    assert "Comic Sans" not in ru_payload
    assert "BorderStyle=3" not in ru_payload
    assert "Первая сцена" in ru_payload
    assert "Вторая сцена" in ru_payload
    assert "First scene" in en_payload
    assert "line" in en_payload
    assert "Second line" in en_payload


def test_normal_recipe_build_does_not_trim_provider_clip_to_planned_duration(monkeypatch):
    scenes = [{"duration_sec": 6.0, "text": "Полная сцена"}]
    candidate = register_video_candidate(scenes[0], language="ru", video_bytes=b"eight-second-provider-clip")
    candidate["approved_for_production"] = True
    candidate["speed_coef"] = 1.1
    seen_target_durations: list[float | None] = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 8.0)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))

    def fake_normalize_segment(**kwargs):
        seen_target_durations.append(kwargs.get("target_duration_sec"))
        kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes())

    monkeypatch.setattr(post_production, "_normalize_segment", fake_normalize_segment)
    monkeypatch.setattr(
        post_production,
        "_concat_segments",
        lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"),
    )

    post_production.build_post_production(
        scenes,
        target_languages=["ru"],
        target_variants=["clean"],
    )

    assert seen_target_durations == [None]


def test_build_post_production_ignores_speed_for_speechless_kling(monkeypatch):
    scenes = [
        {"duration_sec": 4, "text": "", "model": "kling_3_0", "generation_mode": "silence"},
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"kling-video")
    ru["approved_for_production"] = True
    ru["speed_coef"] = 2.0
    seen_speeds: list[float] = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: ("", 0.0))

    def fake_normalize_segment(*, ffmpeg, src_path, dest_path, speed_coef):
        _ = (ffmpeg, src_path)
        seen_speeds.append(speed_coef)
        dest_path.write_bytes(b"normalized")

    def fake_concat_segments(*, ffmpeg, list_path, dest_path):
        _ = (ffmpeg, list_path)
        dest_path.write_bytes(b"clean")

    monkeypatch.setattr(post_production, "_normalize_segment", fake_normalize_segment)
    monkeypatch.setattr(post_production, "_concat_segments", fake_concat_segments)
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)

    out = post_production.build_post_production(
        scenes,
        project_slug="grandma_kitchen",
        target_variants={"clean"},
    )

    assert seen_speeds == [1.0]
    assert out["renders_by_language"]["ru"]["clean"]["width"] == 1080


def test_production_languages_require_approved_scene_video_candidates():
    scenes = [
        {"duration_sec": 8, "text": "Первая сцена"},
        {"duration_sec": 6, "text": "Вторая сцена"},
    ]
    old_approved = register_video_candidate(scenes[0], language="ru", video_bytes=b"old-approved")
    old_approved["approved_for_production"] = True
    latest_unapproved = register_video_candidate(scenes[0], language="ru", video_bytes=b"latest-unapproved")
    second = register_video_candidate(scenes[1], language="ru", video_bytes=b"second")

    assert post_production.production_languages_for_scenes(scenes) == []

    second["approved_for_production"] = True
    assert post_production.production_languages_for_scenes(scenes) == ["ru"]
    assert latest_unapproved["approved_for_production"] is False


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
        overlay_subtitle_by_lang={"ru": "КБЖУ на 100 гр: 126/10/4/24", "en": "per 100g: 126/10/4/24"},
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
    assert "Style: TitleSubtitleRU,Berezka" in ass_by_name["title_overlay_ru.ass"]
    assert "Style: TitleSubtitleEN,Ravsten Demo" in ass_by_name["title_overlay_en.ass"]
    assert "Style: TitleSubtitleRU,Berezka,40,&H00FFFFFF" in ass_by_name["title_overlay_ru.ass"]
    assert "Style: TitleOverlayRU,TD Afisha,118,&H003826C7" in ass_by_name["title_overlay_ru.ass"]
    assert "&H00000000,&H00000000" in ass_by_name["title_overlay_ru.ass"]
    assert ",0,1,0,12,7," in ass_by_name["title_overlay_ru.ass"]
    assert ",0,1,5,10,7," in ass_by_name["title_overlay_ru.ass"]
    assert ",0,1,5,10,8," in ass_by_name["title_overlay_ru.ass"]
    assert r"\fsp" not in ass_by_name["title_overlay_ru.ass"]
    assert re.search(r",118,118,\d+,1", ass_by_name["title_overlay_ru.ass"])
    assert "Dialogue: 1,0:00:00.00,0:00:04.00,TitleOverlayRU" in ass_by_name["title_overlay_ru.ass"]
    assert "Dialogue: 1,0:00:00.00,0:00:04.00,TitleOverlayEN" in ass_by_name["title_overlay_en.ass"]
    assert "Dialogue: 1,0:00:00.00,0:00:04.00,TitleSubtitleRU" in ass_by_name["title_overlay_ru.ass"]
    assert "Dialogue: 1,0:00:00.00,0:00:04.00,TitleSubtitleEN" in ass_by_name["title_overlay_en.ass"]
    assert "Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesRU" in ass_by_name["title_overlay_ru.ass"]
    assert "Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesEN" in ass_by_name["title_overlay_en.ass"]
    assert "Борщ" in ass_by_name["title_overlay_ru.ass"]
    assert "Borscht" in ass_by_name["title_overlay_en.ass"]
    assert r"КБЖУ\hна\h100\hгр:\h126/10/4/24" in ass_by_name["title_overlay_ru.ass"]
    assert r"per\h100g:\h126/10/4/24" in ass_by_name["title_overlay_en.ass"]
    assert r"супы\hу\hбабушки" in ass_by_name["title_overlay_ru.ass"]
    assert r"Grandma's\hSoups" in ass_by_name["title_overlay_en.ass"]
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesRU,,0,0,0,,\{\\q2\\fscx100\\fs\d+\}",
        ass_by_name["title_overlay_ru.ass"],
    )
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesRU,,0,0,0,,\{\\q2\\pos\(\d+,-?\d+\)\}\{\\fnMarianna\\fscx\d+\\fs\d+\}день\\h\{\\fs\d+\}2\{\\fs\d+\}\\hиз\\h7",
        ass_by_name["title_overlay_ru.ass"],
    )
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesEN,,0,0,0,,\{\\q2\\fscx\d+\\fs\d+\}",
        ass_by_name["title_overlay_en.ass"],
    )
    assert re.search(
        r"Dialogue: 2,0:00:00.00,0:00:04.00,TitleSeriesEN,,0,0,0,,\{\\q2\\pos\(\d+,-?\d+\)\}\{\\fnMarianna\\fscx\d+\\fs\d+\}day\\h2\\hof\\h7",
        ass_by_name["title_overlay_en.ass"],
    )
    assert out["title_overlay_by_language"]["ru"] == {"title": "Борщ", "start_sec": 0.0, "end_sec": 4.0}
    assert out["title_overlay_by_language"]["en"] == {"title": "Borscht", "start_sec": 0.0, "end_sec": 4.0}
    assert out["subtitle_overlay_by_language"]["ru"] == {
        "subtitle": "КБЖУ на 100 гр: 126/10/4/24",
        "start_sec": 0.0,
        "end_sec": 4.0,
    }
    assert out["subtitle_overlay_by_language"]["en"] == {
        "subtitle": "per 100g: 126/10/4/24",
        "start_sec": 0.0,
        "end_sec": 4.0,
    }
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


def test_english_title_overlay_latinizes_ru_fallback_instead_of_rendering_cyrillic():
    assert post_production._overlay_title_for_language({"ru": "Лагман"}, "en") == "Lagman"
    assert post_production._overlay_title_for_language({"ru": "ЛАГМАН"}, "en") == "Lagman"
    assert post_production._overlay_title_for_language({"ru": "Лагман", "en": "Lagman"}, "en") == "Lagman"


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
        assert fit.series_actual_width >= int(fit.series_width * 0.97)
        assert fit.counter_actual_width >= int(fit.counter_width * 0.95)
        assert post_production._title_series_day_start_ratio(language, side_margin_ratio=0.10) == 0.60
        assert fit.counter_font_size >= int(fit.series_font_size * 0.55)
        assert fit.counter_scale_x == 100
        assert fit.counter_actual_width <= fit.counter_width
        assert fit.scale_x == 100


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
    assert fit.width == fit.series_actual_width + fit.counter_actual_width + post_production._title_series_day_gap(layout)
    assert fit.series_font_size > 63
    assert post_production._title_series_day_start_ratio("ru", side_margin_ratio=0.10) == 0.60


def test_series_day_position_follows_configured_start_ratio():
    layout = post_production._subtitle_layout_for_dimensions(1080, 1920)
    fit = post_production._fit_title_series_ass_text(
        "летние напитки",
        "день 8 из 10",
        layout=layout,
        language="ru",
        side_margin_ratio=0.10,
    )

    assert fit is not None
    day_x = post_production._title_series_day_x(
        layout=layout,
        fit=fit,
        series_left=108,
        title_margin_h=108,
        language="ru",
        side_margin_ratio=0.10,
    )

    expected_x = min(
        round(layout.width * post_production._title_series_day_start_ratio("ru", side_margin_ratio=0.10)),
        layout.width - 108 - fit.counter_actual_width,
    )

    assert day_x == expected_x
    assert day_x >= round(layout.width * 0.60) - 1


def test_short_ru_series_title_does_not_push_main_title_down_by_full_font_bbox():
    layout = post_production._subtitle_layout_for_dimensions(1080, 1920)
    fit = post_production._fit_title_series_ass_text(
        "Карта вкусов",
        "день 1 из 10",
        layout=layout,
        language="ru",
        side_margin_ratio=0.10,
    )

    assert fit is not None
    full_bbox_offset = max(0, int(round(fit.series_actual_height + (layout.height * post_production.TITLE_SERIES_ROW_GAP_RATIO))))
    capped_offset = post_production._title_series_main_row_offset(layout, fit)

    assert capped_offset < full_bbox_offset
    assert capped_offset <= int(fit.series_font_size * post_production.TITLE_SERIES_MAIN_TITLE_OFFSET_MAX_FONT_SCALE)


def test_ru_series_overlay_normalizes_uppercase_for_berezka_glyphs():
    layout = post_production._subtitle_layout_for_dimensions(1080, 1920)

    formatted = post_production._format_title_series_ass_text(
        "ЛЕТНИЕ НАПИТКИ",
        "день 8 из 10",
        layout=layout,
        language="ru",
    )

    assert r"летние\hнапитки" in formatted
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
    scene = {
        "duration_sec": 6,
        "text": "Первая сцена",
        "speech_cues_by_lang": {"ru": [{"text": "Первая сцена", "start_sec": 0.0, "end_sec": 8.0}]},
    }
    candidate = register_video_candidate(scene, language="ru", video_bytes=b"source-video")
    candidate["approved_for_production"] = True
    candidate["speed_coef"] = 2.0
    rendered_inputs = []
    progress_events = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (2160, 3840))
    monkeypatch.setattr(post_production, "_selected_video_source_duration", lambda _selected: 8.0)
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
        [scene],
        upscaled_video_path=source,
        language="ru",
        progress_callback=progress_events.append,
    )

    assert rendered_inputs == [("/usr/bin/ffmpeg", source, "/tmp/music.mp3", 0.2)]
    payload = out["renders_by_language"]["ru"]["subtitled_music"]
    assert payload["source_variant"] == "upscaled_4k"
    assert payload["source_video_path"] == str(source)
    assert [item["text"] for item in out["subtitle_entries"]] == ["Первая", "Первая сцена"]
    assert out["subtitle_entries"][0]["start_sec"] == 0.0
    assert out["subtitle_entries"][-1]["end_sec"] == 4.0
    assert all(item["word_synced"] is True for item in out["subtitle_entries"])
    assert all(item["speed_coef"] == 2.0 for item in out["subtitle_entries"])
    assert [item["stage"] for item in progress_events] == ["reuse_upscaled", "subtitles_building", "subtitles_ready", "persisting"]


def test_story_subtitled_4k_master_keeps_voice_and_omits_background_music(tmp_path, monkeypatch):
    source = tmp_path / "story_upscaled_4k_master_ru.mp4"
    source.write_bytes(b"4k-story-with-voice")
    scene = {
        "duration_sec": 4,
        "text": "Дословный текст",
        "speech_cues_by_lang": {
            "ru": [{"text": "Дословный текст", "start_sec": 0.0, "end_sec": 4.0}]
        },
    }
    candidate = register_video_candidate(scene, language="ru", video_bytes=b"source-video")
    candidate["approved_for_production"] = True
    rendered_inputs = []

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (2160, 3840))
    monkeypatch.setattr(
        post_production,
        "_resolve_music_path",
        lambda _slug: (_ for _ in ()).throw(AssertionError("Story must not resolve background music")),
    )
    monkeypatch.setattr(post_production, "_persist_render_artifact", lambda path, **_kwargs: str(path))

    def fake_render(*, ffmpeg, clean_path, subtitles_path, music_path, music_volume, dest_path):
        rendered_inputs.append((ffmpeg, Path(clean_path), music_path, music_volume))
        dest_path.write_bytes(Path(clean_path).read_bytes() + b"|subtitles|")
        return True

    monkeypatch.setattr(post_production, "_render_subtitled_music_master", fake_render)

    out = post_production.build_subtitled_post_production_from_upscaled_master(
        [scene],
        upscaled_video_path=source,
        language="ru",
        include_background_music=False,
    )

    assert rendered_inputs == [("/usr/bin/ffmpeg", source, None, 0.0)]
    payload = out["renders_by_language"]["ru"]["subtitled_music"]
    assert payload["music_asset_uri"] is None
    assert payload["music_volume"] == 0.0
    assert payload["background_music_included"] is False
    assert payload["source_audio_preserved"] is True
    assert out["background_music_included"] is False


def test_title_overlay_helpers_cover_fallbacks_and_invalid_style_values(tmp_path):
    ass_path = tmp_path / "subtitles_with_title.ass"

    assert post_production._overlay_title_for_language({"ru": "Борщ"}, "en") == "Borshch"
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
        * post_production._title_overlay_ass_safe_width_ratio("ru")
    )
    max_safe_height = int(1920 * post_production.TITLE_OVERLAY_MAX_HEIGHT_RATIO)
    assert fit.width <= max_safe_width
    assert post_production.TITLE_OVERLAY_RU_SCALE_X == 100
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
        ["для", "белого", "мусса", "нужен", "белый"],
        ["шоколад", "и", "немного", "холодных", "сливок"],
    ]
    first_block = post_production._format_burned_subtitle_block(pages[0])
    assert first_block == "ДЛЯ БЕЛОГО\nМУССА НУЖЕН БЕЛЫЙ"
    assert len(first_block.splitlines()) <= 2
    assert post_production._format_burned_subtitle_block(["в", "дом", "и", "сад"]) == "В ДОМ И САД"


def test_burned_subtitle_ass_style_adapts_to_video_shape(tmp_path):
    vertical = tmp_path / "vertical.ass"
    horizontal = tmp_path / "horizontal.ass"

    post_production._write_burned_subtitle_ass(vertical, [], video_width=1080, video_height=1920)
    post_production._write_burned_subtitle_ass(horizontal, [], video_width=1920, video_height=1080)

    vertical_text = vertical.read_text(encoding="utf-8")
    horizontal_text = horizontal.read_text(encoding="utf-8")
    font_name = post_production._subtitle_font_name()
    assert f"Style: CleanPremium,{font_name},88" in vertical_text
    assert "&H00000000,&H70000000" in vertical_text
    assert ",1,6,3,1,150,150,575,1" in vertical_text
    assert f"Style: CleanPremium,{font_name},78" in horizontal_text
    assert "&H00000000,&H70000000" in horizontal_text
    assert ",1,6,3,1,150,150,190,1" in horizontal_text


def test_burned_subtitle_ass_uses_tight_explicit_line_spacing(tmp_path):
    ass_path = tmp_path / "tight_lines.ass"

    post_production._write_burned_subtitle_ass(
        ass_path,
        [{"text": "МОЖЕТ\nСБЕЖИМ СЕГОДНЯ", "start_sec": 1.0, "end_sec": 3.0}],
        video_width=1080,
        video_height=1920,
    )

    payload = ass_path.read_text(encoding="utf-8")
    assert post_production._subtitle_line_advance(
        post_production._subtitle_layout_for_dimensions(1080, 1920)
    ) == 88
    assert "Dialogue: 0,0:00:01.00,0:00:03.00,CleanPremium,,0,0,0,,{\\an1\\pos(150,1257)}МОЖЕТ" in payload
    assert (
        "Dialogue: 0,0:00:01.00,0:00:03.00,CleanPremium,,0,0,0,,{\\an1\\pos(150,1345)}СБЕЖИМ СЕГОДНЯ"
        in payload
    )
    assert r"МОЖЕТ\NСБЕЖИМ" not in payload


def test_burned_subtitle_layout_keeps_visual_line_width_at_4k():
    layout_1080 = post_production._subtitle_layout_for_dimensions(1080, 1920)
    layout_4k = post_production._subtitle_layout_for_dimensions(2160, 3840)

    assert layout_1080.line_chars == 18
    assert layout_4k.line_chars == layout_1080.line_chars


def test_burned_subtitles_render_complete_block_without_progressive_jump():
    cues = post_production._build_burned_subtitle_cues(
        {"text": "для белого мусса нужен белый шоколад", "start_sec": 1.0, "end_sec": 4.0},
        video_width=1080,
        video_height=1920,
    )

    assert [cue["text"] for cue in cues] == ["ДЛЯ БЕЛОГО\nМУССА НУЖЕН БЕЛЫЙ", "ШОКОЛАД"]
    assert [cue["start_sec"] for cue in cues] == [1.0, 3.5]
    assert [cue["end_sec"] for cue in cues] == [3.5, 4.0]
    assert post_production.SUBTITLE_ASS_INVISIBLE_TOKEN not in cues[0]["text"]


def test_burned_subtitles_switch_pages_without_overlap_or_jump():
    cues = post_production._build_burned_subtitle_cues(
        {
            "text": "для белого мусса нужен белый шоколад и немного холодных сливок",
            "start_sec": 1.0,
            "end_sec": 5.0,
        },
        video_width=1080,
        video_height=1920,
    )

    assert [cue["text"] for cue in cues] == [
        "ДЛЯ БЕЛОГО\nМУССА НУЖЕН БЕЛЫЙ",
        "ШОКОЛАД И НЕМНОГО\nХОЛОДНЫХ СЛИВОК",
    ]
    assert [cue["start_sec"] for cue in cues] == [1.0, 3.0]
    assert [cue["end_sec"] for cue in cues] == [3.0, 5.0]


def test_burned_subtitles_split_4k_text_into_two_line_pages():
    cues = post_production._build_burned_subtitle_cues(
        {
            "text": "Don't lose this recipe! The most refreshing raspberry lemonade to beat the heat.",
            "start_sec": 1.0,
            "end_sec": 5.0,
        },
        video_width=2160,
        video_height=3840,
    )

    assert len(cues) > 1
    assert all(len(cue["text"].splitlines()) <= 2 for cue in cues)
    assert [cue["start_sec"] for cue in cues] == sorted(cue["start_sec"] for cue in cues)
    for previous, current in zip(cues, cues[1:]):
        assert previous["end_sec"] == current["start_sec"]


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
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 5.0)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes)

    assert [item["text"] for item in out["subtitle_entries_by_language"]["en"]] == [
        "Long",
        "Long english",
        "full",
        "full line",
    ]
    assert out["subtitle_entries_by_language"]["en"][2]["start_sec"] == 1.25
    assert out["speech_budget_by_language"]["en"] == []
    assert out["original_audio_preserved_by_language"]["en"] is True


def test_build_post_production_does_not_fallback_english_subtitles_to_russian(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6,
            "text": "Русская фраза",
            "speech_by_lang": {"ru": "Русская фраза"},
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

    def fake_render_subtitled_music_master(**kwargs):
        kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes())
        return True

    monkeypatch.setattr(post_production, "_render_subtitled_music_master", fake_render_subtitled_music_master)

    try:
        post_production.build_post_production(scenes, target_languages=["en"])
    except RuntimeError as exc:
        assert "Missing language-specific subtitle text" in str(exc)
    else:
        raise AssertionError("English build must not use Russian scene text as subtitles")


def test_build_post_production_uses_english_speech_timing_plan_for_subtitles(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6,
            "text": "Русская фраза",
            "speech_by_lang": {},
            "video_prompt_by_lang": {
                "en": (
                    "lang:en A 6-second clip.\n"
                    "voice_lock_full_grandma: English voice.\n"
                    "speech_timing_plan:\n"
                    "- 0.00-8.00s: Never throw away watermelon rinds! "
                    "They make such delicious watermelon jam."
                )
            },
        }
    ]
    en = register_video_candidate(scenes[0], language="en", video_bytes=b"en")
    en["approved_for_production"] = True

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes, target_languages=["en"])

    subtitles = out["subtitle_entries_by_language"]["en"]
    completed_blocks = [
        item["text"]
        for item in subtitles
        if item["visible_word_count"] == len(item["fixed_block_words"])
    ]
    assert " ".join(completed_blocks) == (
        "Never throw away watermelon rinds! They make such delicious watermelon jam."
    )
    assert all(item["word_synced"] is True for item in subtitles)


def test_build_post_production_uses_candidate_speech_snapshot_for_subtitles(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6,
            "text": "Новый текст сцены",
            "speech_by_lang": {"ru": "Новый текст сцены"},
        }
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru")
    ru["approved_for_production"] = True
    ru["speech_by_lang"] = {"ru": "Старый текст кандидата"}

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes, target_languages=["ru"])

    subtitles = out["subtitle_entries_by_language"]["ru"]
    assert subtitles[-1]["text"] == "Старый текст кандидата"
    assert all(item["word_synced"] is True for item in subtitles)


def test_build_post_production_rejects_english_candidate_snapshot_for_ru_subtitles(monkeypatch):
    scene = {
        "scene": 1,
        "duration_sec": 6,
        "text": "Русский текст сцены",
        "speech_by_lang": {"ru": "Русский текст сцены"},
        "speech_cues_by_lang": {
            "ru": [{"text": "Русский cue сцены", "start_sec": 0.0, "end_sec": 6.0}]
        },
    }
    selected = {
        "speech_by_lang": {"ru": "English candidate text"},
        "speech_cues_by_lang": {
            "ru": [{"text": "English candidate cue", "start_sec": 0.0, "end_sec": 6.0}]
        },
        "video_prompt_by_lang": {"ru": "CHARACTERS SPEAKS: English candidate prompt"},
    }

    merged = post_production._scene_with_candidate_subtitle_snapshot(scene, selected, "ru")
    entries = post_production._scene_subtitle_entries(
        merged,
        "ru",
        start_offset=0.0,
        fallback_duration=6.0,
        speed_coef=1.0,
    )

    assert entries[-1]["text"] == "Русский cue сцены"
    assert all(entry["word_synced"] is True for entry in entries)


def test_build_post_production_can_prefer_scene_speech_over_candidate_snapshot(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6,
            "text": "Новый текст сцены",
            "speech_by_lang": {"ru": "Новый текст сцены"},
        }
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru")
    ru["approved_for_production"] = True
    ru["speech_by_lang"] = {"ru": "Старый текст кандидата"}
    ru["speech_cues_by_lang"] = {
        "ru": [{"text": "Старый cue кандидата", "start_sec": 0.0, "end_sec": 6.0}]
    }

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(
        scenes,
        target_languages=["ru"],
        use_candidate_subtitle_snapshot=False,
    )

    subtitles = out["subtitle_entries_by_language"]["ru"]
    assert subtitles[-1]["text"] == "Новый текст сцены"
    assert all(item["word_synced"] is True for item in subtitles)


def test_build_post_production_splits_fallback_subtitles_on_pause_markers(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6,
            "text": "Первая часть [пауза] Вторая часть [пауза] Финал",
            "speech_by_lang": {"ru": "Первая часть [пауза] Вторая часть [пауза] Финал"},
        }
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru")
    ru["approved_for_production"] = True
    ru["speed_coef"] = 1.0

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes, target_languages=["ru"])
    subtitles = out["subtitle_entries_by_language"]["ru"]

    assert [item["text"] for item in subtitles] == [
        "Первая",
        "Первая часть",
        "Вторая",
        "Вторая часть",
        "Финал",
    ]
    assert all(item["word_synced"] is True for item in subtitles)
    assert subtitles[0]["start_sec"] == 0.0
    assert subtitles[-1]["end_sec"] == 6.0


def test_build_post_production_ignores_zero_duration_speech_cues(monkeypatch):
    scenes = [
        {
            "scene": 1,
            "duration_sec": 6,
            "speech_by_lang": {"ru": "Первая часть [пауза] Вторая часть"},
            "speech_cues_by_lang": {
                "ru": [
                    {"text": "Первая часть", "start_sec": 0.0, "end_sec": 0.0},
                    {"text": "Вторая часть", "start_sec": 0.0, "end_sec": 0.0},
                ]
            },
        }
    ]
    ru = register_video_candidate(scenes[0], language="ru", video_bytes=b"ru")
    ru["approved_for_production"] = True
    ru["speed_coef"] = 1.0

    monkeypatch.setattr(post_production, "_resolve_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(post_production, "_video_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(post_production, "_resolve_music_path", lambda _slug: (None, 0.18))
    monkeypatch.setattr(post_production, "_normalize_segment", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["src_path"].read_bytes()))
    monkeypatch.setattr(post_production, "_concat_segments", lambda **kwargs: kwargs["dest_path"].write_bytes(b"merged"))
    monkeypatch.setattr(post_production, "_upscale_master_to_4k", _fake_upscale_master_to_4k)
    monkeypatch.setattr(post_production, "_render_subtitled_music_master", lambda **kwargs: kwargs["dest_path"].write_bytes(kwargs["subtitles_path"].read_bytes()))

    out = post_production.build_post_production(scenes, target_languages=["ru"])
    subtitles = out["subtitle_entries_by_language"]["ru"]

    assert [item["text"] for item in subtitles] == ["Первая", "Первая часть", "Вторая", "Вторая часть"]
    assert all(item["word_synced"] is True for item in subtitles)
    assert subtitles[0]["start_sec"] == 0.0
    assert subtitles[0]["end_sec"] > subtitles[0]["start_sec"]
    assert subtitles[2]["start_sec"] == subtitles[1]["end_sec"]
    assert subtitles[-1]["end_sec"] == 6.0


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
    assert subtitles[1]["end_sec"] == 2.0
    assert subtitles[2]["start_sec"] == 2.0
    assert subtitles[-1]["end_sec"] == 4.0


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
    assert subtitles[1]["end_sec"] == 2.2
    assert subtitles[2]["start_sec"] == 2.2
    assert subtitles[-1]["end_sec"] == 4.4


def test_subtitle_text_removes_veo_stress_markers():
    assert post_production._normalize_subtitle_text("готОво [пауза] молоко́") == "готово молоко"
    assert post_production._normalize_subtitle_text("Ready [pause] now") == "Ready now"
    assert post_production._normalize_subtitle_text("Ready [ PAUSE ] now") == "Ready now"
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
    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 6.0)

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
    assert "[0:a]volume=1.0000,apad=whole_dur=6.000,atrim=duration=6.000,asetpts=N/SR/TB[voice]" in filter_complex
    assert "[1:a]volume=0.1274[music]" in filter_complex
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]" in filter_complex
    assert "-stream_loop" in captured["cmd"]
    assert "-shortest" in captured["cmd"]
    assert captured["stage"] == "render_subtitled_music_master_mix"
    assert captured["timeout"] == post_production.FFMPEG_UPSCALE_TIMEOUT_SEC


def test_normalize_segment_adds_silent_audio_for_mute_source(monkeypatch, tmp_path):
    src = tmp_path / "src.mp4"
    dest = tmp_path / "dest.mp4"
    src.write_bytes(b"video")
    captured = {}

    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 4.0)
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: False)

    def fake_run_ffmpeg(cmd, *, timeout, stage):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        captured["stage"] = stage
        dest.write_bytes(b"out")

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run_ffmpeg)

    post_production._normalize_segment(
        ffmpeg="/usr/bin/ffmpeg",
        src_path=src,
        dest_path=dest,
        speed_coef=1.0,
    )

    assert "-f" in captured["cmd"]
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in captured["cmd"]
    filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "setpts=PTS/1.000000,fps=30,tpad=stop_mode=clone:stop_duration=0.100000,"
        "trim=duration=4.000000,settb=AVTB,setpts=N/(30*TB)[v]"
    ) in filter_complex
    assert "[1:a]atrim=duration=4.000000,asetpts=N/SR/TB[a]" in filter_complex
    assert captured["cmd"][captured["cmd"].index("-map") + 1] == "[v]"
    assert "[a]" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-ar") + 1] == "48000"
    assert captured["cmd"][captured["cmd"].index("-ac") + 1] == "2"
    assert captured["cmd"][captured["cmd"].index("-video_track_timescale") + 1] == "30000"
    assert captured["stage"] == "normalize_segment"
    assert captured["timeout"] == post_production.FFMPEG_RENDER_TIMEOUT_SEC


def test_normalize_segment_pads_audio_to_video_duration(monkeypatch, tmp_path):
    src = tmp_path / "src.mp4"
    dest = tmp_path / "dest.mp4"
    src.write_bytes(b"video")
    captured = {}

    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 8.0)
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)

    def fake_run_ffmpeg(cmd, *, timeout, stage):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        captured["stage"] = stage
        dest.write_bytes(b"out")

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run_ffmpeg)

    post_production._normalize_segment(
        ffmpeg="/usr/bin/ffmpeg",
        src_path=src,
        dest_path=dest,
        speed_coef=2.0,
    )

    assert "anullsrc=channel_layout=stereo:sample_rate=48000" not in captured["cmd"]
    filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "setpts=PTS/2.000000,fps=30,tpad=stop_mode=clone:stop_duration=0.100000,"
        "trim=duration=4.000000,settb=AVTB,setpts=N/(30*TB)[v]"
    ) in filter_complex
    assert (
        "[0:a]atempo=2.00000,aresample=async=1:first_pts=0,"
        "apad=whole_dur=4.000000,atrim=duration=4.000000,asetpts=N/SR/TB[a]"
    ) in filter_complex
    assert captured["cmd"][captured["cmd"].index("-map") + 1] == "[v]"
    assert "[a]" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-ar") + 1] == "48000"
    assert captured["cmd"][captured["cmd"].index("-ac") + 1] == "2"
    assert captured["cmd"][captured["cmd"].index("-video_track_timescale") + 1] == "30000"
    assert captured["stage"] == "normalize_segment"
    assert captured["timeout"] == post_production.FFMPEG_RENDER_TIMEOUT_SEC


def test_normalize_segment_uses_frame_aligned_audio_duration(monkeypatch, tmp_path):
    src = tmp_path / "src.mp4"
    dest = tmp_path / "dest.mp4"
    src.write_bytes(b"video")
    captured = {}

    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 8.0)
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: True)

    def fake_run_ffmpeg(cmd, *, timeout, stage):
        captured["cmd"] = cmd
        dest.write_bytes(b"out")

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run_ffmpeg)

    post_production._normalize_segment(
        ffmpeg="/usr/bin/ffmpeg",
        src_path=src,
        dest_path=dest,
        speed_coef=1.1,
    )

    filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "trim=duration=7.266667" in filter_complex
    assert "apad=whole_dur=7.266667,atrim=duration=7.266667" in filter_complex


def test_normalize_story_segment_trims_three_second_clip_to_exact_timeline_window(monkeypatch, tmp_path):
    src = tmp_path / "story_clip.mp4"
    dest = tmp_path / "story_clip_trimmed.mp4"
    src.write_bytes(b"video")
    captured = {}

    monkeypatch.setattr(post_production, "_media_duration", lambda _path: 3.0)
    monkeypatch.setattr(post_production, "_has_audio_stream", lambda _path: False)

    def fake_run_ffmpeg(cmd, *, timeout, stage):
        captured["cmd"] = cmd
        dest.write_bytes(b"out")

    monkeypatch.setattr(post_production, "_run_ffmpeg", fake_run_ffmpeg)

    post_production._normalize_segment(
        ffmpeg="/usr/bin/ffmpeg",
        src_path=src,
        dest_path=dest,
        speed_coef=1.0,
        target_duration_sec=2.4,
    )

    filter_complex = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "trim=duration=2.400000" in filter_complex
    assert "atrim=duration=2.400000" in filter_complex


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


def test_selected_scene_video_fingerprint_changes_with_candidate():
    scenes = [
        {
            "scene": 1,
            "duration_sec": 4,
            "video_candidates_by_lang": {
                "ru": [
                    {"candidate_id": "old", "approved_for_production": True, "speed_coef": 1.0},
                ]
            },
        }
    ]

    first = post_production.selected_scene_video_fingerprint_for_language(scenes, "ru")
    scenes[0]["video_candidates_by_lang"]["ru"].append(
        {"candidate_id": "new", "approved_for_production": True, "speed_coef": 1.0}
    )

    assert post_production.selected_scene_video_fingerprint_for_language(scenes, "ru") != first


def test_dense_subtitle_entry_is_split_into_subwindows():
    item = {
        "text": "для белого мусса нужен белый шоколад и немного холодных сливок",
        "start_sec": 0.0,
        "end_sec": 2.0,
    }
    out = post_production._expand_dense_subtitle_entry(item)
    assert len(out) == 2
    assert out[0]["text"] == "для белого мусса нужен белый"
    assert out[0]["start_sec"] == 0.0
    assert 0.0 < out[0]["end_sec"] < 2.0
    assert out[1]["text"] == "шоколад и немного холодных сливок"
    assert out[1]["start_sec"] == out[0]["end_sec"]
    assert out[1]["end_sec"] == 2.0
