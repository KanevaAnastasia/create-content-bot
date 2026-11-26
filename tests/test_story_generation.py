import json
import base64
import math
import pytest
from pathlib import Path

from app import story_generation
from app.handlers.handlers_media_scene import _append_scene_identity_to_image_prompt
from app.handlers.handlers_media_scene import _load_scene_reference_images


def test_story_visual_style_catalog_contains_primary_and_all_named_choices():
    expected = {
        "primary": "Основной",
        "watercolor": "Сухая акварель",
        "retro_cartoon": "Ретро-мультфильм",
        "storybook_gouache": "Книжная гуашь",
        "claymation": "Пластилиновый мультфильм",
        "felt_stop_motion": "Кукольный стоп-моушен",
        "retro_fairytale": "Рисованная ретро-сказка",
        "twilight_watercolor": "Сумеречная акварель",
        "soft_animation": "Светлая анимационная пастель",
        "classic_cel_cartoon": "Классический рисованный мультфильм",
        "felt_applique": "Фетровая аппликация",
        "folk_fairytale": "Народная рисованная сказка",
        "oil_pastel": "Пастельные мелки",
        "crochet": "Вязаная иллюстрация",
        "minimal_retro_cartoon": "Минималистичный ретро-мультфильм",
    }

    from app.history_visual_styles import HISTORY_STYLES
    expected.update({key: item["label"] for key, item in HISTORY_STYLES.items()})
    assert {key: item["label"] for key, item in story_generation.STORY_VISUAL_STYLES.items()} == expected
    for key, label in expected.items():
        config = story_generation.story_visual_style_config(key)
        assert config["label"] == label
        if key == "primary":
            assert config["prompt"] == ""
            assert config["reference_path"] == ""
        else:
            assert config["prompt"]
            assert (Path(__file__).parents[1] / (config.get("preview_path") or config["reference_path"])).is_file()


def test_story_visual_style_aliases_normalize_new_russian_labels():
    assert story_generation.normalize_story_visual_style("Основной") == "primary"
    assert story_generation.normalize_story_visual_style("Книжная гуашь") == "storybook_gouache"
    assert story_generation.normalize_story_visual_style("Пластилиновый мультфильм") == "claymation"
    assert story_generation.normalize_story_visual_style("Кукольный стоп-моушен") == "felt_stop_motion"
    assert story_generation.normalize_story_visual_style("Рисованная ретро-сказка") == "retro_fairytale"
    assert story_generation.normalize_story_visual_style("Сумеречная акварель") == "twilight_watercolor"
    assert story_generation.normalize_story_visual_style("Светлая анимационная пастель") == "soft_animation"
    assert story_generation.normalize_story_visual_style("Классический рисованный мультфильм") == "classic_cel_cartoon"
    assert story_generation.normalize_story_visual_style("Фетровая аппликация") == "felt_applique"
    assert story_generation.normalize_story_visual_style("Народная рисованная сказка") == "folk_fairytale"
    assert story_generation.normalize_story_visual_style("Пастельные мелки") == "oil_pastel"
    assert story_generation.normalize_story_visual_style("Вязаная иллюстрация") == "crochet"
    assert story_generation.normalize_story_visual_style("Минималистичный ретро-мультфильм") == "minimal_retro_cartoon"


def test_visualization_short_tail_uses_one_four_second_final_clip():
    clips = [
        {
            "scene": index + 1,
            "start_sec": float(index * 3),
            "end_sec": float(min(15.3, (index + 1) * 3)),
            "generation_duration_sec": 3.0,
            "narration_excerpt": str(index),
        }
        for index in range(6)
    ]

    result = story_generation._apply_visualization_final_clip_policy(
        clips,
        {
            "preserve_short_tail_with_four_second_final_clip": True,
            "final_clip_base_sec": 3.0,
            "max_preserved_tail_sec": 0.5,
            "final_generation_duration_sec": 4.0,
        },
    )

    assert len(result) == 5
    assert result[-1]["start_sec"] == 12.0
    assert result[-1]["end_sec"] == 15.3
    assert result[-1]["generation_duration_sec"] == 4.0


def test_story_timed_audio_uses_eleven_v4_dialogue_api(monkeypatch):
    captured: dict = {}
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-123")

    class _Response:
        ok = True
        headers = {"request-id": "request-v4", "history-item-id": "history-v4"}

        @staticmethod
        def json():
            return {
                "audio_base64": base64.b64encode(b"v4-audio").decode("ascii"),
                "normalized_alignment": {
                    "characters": ["Д", "а"],
                    "character_start_times_seconds": [0.0, 0.2],
                    "character_end_times_seconds": [0.2, 0.4],
                },
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr("requests.post", fake_post)

    result = story_generation._timed_endpoint_result("Да")

    assert captured["url"].endswith("/v1/text-to-dialogue/with-timestamps")
    assert captured["json"]["model_id"] == "eleven_v4"
    assert captured["json"]["inputs"] == [{"text": "Да", "voice_id": "voice-123"}]
    assert captured["json"]["settings"] == {"stability": 0.5, "similarity_boost": 0.75}
    assert result.audio_bytes == b"v4-audio"
    assert result.duration_sec == 0.4
    assert result.model_id == "eleven_v4"


def test_story_timed_audio_sends_english_language_code(monkeypatch):
    captured: dict = {}
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-123")

    class _Response:
        ok = True
        headers = {}

        @staticmethod
        def json():
            return {
                "audio_base64": base64.b64encode(b"english-v4-audio").decode("ascii"),
                "normalized_alignment": {
                    "characters": ["H", "i"],
                    "character_start_times_seconds": [0.0, 0.2],
                    "character_end_times_seconds": [0.2, 0.4],
                },
            }

    def fake_post(_url, **kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr("requests.post", fake_post)

    story_generation._timed_endpoint_result("Hi", language="en")

    assert captured["json"]["language_code"] == "en"


def test_story_dialogue_audio_keeps_turn_order_and_voice_ids(monkeypatch):
    captured: dict = {}
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    class _Response:
        ok = True
        headers = {}

        @staticmethod
        def json():
            return {
                "audio_base64": base64.b64encode(b"dialogue-audio").decode("ascii"),
                "normalized_alignment": {
                    "characters": ["Д", "а"],
                    "character_start_times_seconds": [0.0, 0.2],
                    "character_end_times_seconds": [0.2, 0.4],
                },
                "alignment": {
                    "characters": ["Д", "а", "Д", "а"],
                    "character_start_times_seconds": [0.0, 0.2, 1.0, 1.2],
                    "character_end_times_seconds": [0.2, 0.4, 1.2, 1.4],
                },
                "voice_segments": [
                    {"dialogue_input_index": 0, "character_start_index": 0},
                    {"dialogue_input_index": 1, "character_start_index": 2},
                ],
            }

    def fake_post(_url, **kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr("requests.post", fake_post)

    result = story_generation.generate_story_dialogue_audio(
        [
            {"text": "  Первая реплика. ", "voice_id": "voice-a"},
            {"text": "Вторая реплика.", "voice_id": "voice-b"},
        ]
    )

    assert captured["json"]["inputs"] == [
        {"text": "Первая реплика.", "voice_id": "voice-a"},
        {"text": "Вторая реплика.", "voice_id": "voice-b"},
    ]
    assert result.audio_bytes == b"dialogue-audio"
    assert result.dialogue_turn_starts == (0, 2)
    assert result.alignment["character_start_times_seconds"] == [0.0, 0.2, 1.0, 1.2]


def test_story_english_voiceover_is_unabridged_translation_of_approved_russian_text(monkeypatch):
    prompts: list[str] = []

    def fake_gemini(prompt, **_kwargs):
        prompts.append(prompt)
        return json.dumps(
            {
                "title": "The Secret of Charlotte",
                "voiceover_text": "What connects a family apple cake, a queen, and an abandoned boy?",
            }
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)

    result = story_generation.generate_story_english_voiceover(
        "Тайна шарлотки",
        "Что связывает бабушкину шарлотку, королеву и мальчика?",
        target_duration_sec=54.2,
    )

    assert result["title"] == "The Secret of Charlotte"
    assert result["voiceover_text"].startswith("What connects")
    assert "approved_russian_voiceover" in prompts[0]
    assert "54.2" in prompts[0]
    assert "complete translation, never an adaptation" in prompts[0]
    assert "do not shorten the translation" in prompts[0]
    assert "Do not summarize, condense, merge sentences" in prompts[0]


def test_story_voiceover_returns_oversized_text_for_human_review_without_retry(monkeypatch):
    prompts: list[str] = []
    too_long = " ".join(["слишком"] * 260)

    def fake_gemini(prompt, **_kwargs):
        prompts.append(prompt)
        return json.dumps({"title": "Шарлотка", "voiceover_text": too_long}, ensure_ascii=False)

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)

    result = story_generation.generate_story_voiceover("История происхождения шарлотки")

    assert result["title"] == "Шарлотка"
    assert result["voiceover_text"] == too_long
    assert result["within_target_limit"] is False
    assert result["word_count"] == 260
    assert len(prompts) == 1


def test_story_voiceover_versions_archive_every_text_without_overwriting():
    session: dict = {}
    first = {
        "title": "Первая версия",
        "voiceover_text": "Первый текст.",
        "raw_response": '{"voiceover_text":"Первый текст."}',
    }
    second = {
        "title": "Вторая версия",
        "voiceover_text": "Второй текст.",
        "raw_response": '{"voiceover_text":"Второй текст."}',
    }

    story_generation.archive_story_voiceover_version(session, first, stage="initial")
    story_generation.archive_story_voiceover_version(
        session,
        second,
        stage="refinement",
        feedback="Сделай короче",
    )

    versions = session[story_generation.STORY_VOICEOVER_VERSIONS_SESSION_KEY]
    assert [item["voiceover_text"] for item in versions] == ["Первый текст.", "Второй текст."]
    assert versions[1]["feedback"] == "Сделай короче"
    assert versions[0]["version_id"] != versions[1]["version_id"]


def test_archive_story_subject_titles_use_primary_real_person_in_both_languages():
    titles = story_generation.archive_story_subject_titles(
        [
            {
                "id": "tabitha_king_1973",
                "display_name": "Табита Кинг",
                "same_person_group": "tabitha_king",
            },
            {
                "id": "stephen_king_1973",
                "display_name": "Стивен Кинг",
                "same_person_group": "stephen_king",
                "is_primary": True,
            },
            {"id": "prop_manuscript", "display_name": "Рукопись «Кэрри»"},
        ]
    )

    assert titles == {"ru": "Стивен Кинг", "en": "Stephen King"}


def test_build_narration_windows_uses_elevenlabs_character_timestamps():
    alignment = {
        "characters": list("раз два три"),
        "character_start_times_seconds": [i * 0.55 for i in range(11)],
        "character_end_times_seconds": [(i + 1) * 0.55 for i in range(11)],
    }
    windows = story_generation.build_narration_windows(
        "раз два три",
        6.05,
        alignment,
    )
    assert len(windows) == 3
    assert windows[0]["start_sec"] == 0.0
    assert windows[0]["end_sec"] == 3.0
    assert "раз" in windows[0]["narration_excerpt"]
    assert windows[-1]["end_sec"] == 6.05


def test_build_narration_windows_never_splits_a_word_at_scene_boundary():
    text = "первое длинноеслово третье"
    alignment = {
        "characters": list(text),
        "character_start_times_seconds": [i * 0.2 for i in range(len(text))],
        "character_end_times_seconds": [(i + 1) * 0.2 for i in range(len(text))],
    }
    windows = story_generation.build_narration_windows(text, len(text) * 0.2, alignment)
    excerpts = [window["narration_excerpt"] for window in windows]
    assert sum("длинноеслово" in excerpt for excerpt in excerpts) == 1
    assert all("длинноес" not in excerpt or "длинноеслово" in excerpt for excerpt in excerpts)


def test_build_narration_windows_for_intervals_follows_shorter_translation_timing():
    text = "one two three four"
    duration = 4.0
    windows = story_generation.build_narration_windows_for_intervals(
        text,
        duration,
        {},
        [(0.0, 1.5), (1.5, 3.0), (3.0, 4.0)],
    )

    assert [item["start_sec"] for item in windows] == [0.0, 1.5, 3.0]
    assert [item["end_sec"] for item in windows] == [1.5, 3.0, 4.0]
    assert " ".join(item["narration_excerpt"] for item in windows) == text


def test_semantic_story_beats_keep_complete_sentences_and_expand_to_three_second_clips():
    text = (
        "Что связывает бабушкину шарлотку, английскую королеву и мальчика, оставленного отцом? "
        "Ему было всего десять. Париж охвачен революцией, когда отец бросает маленького Антонена."
    )
    beats = story_generation.build_semantic_narration_beats(text, 15.0, {})

    assert beats[0]["narration_excerpt"].endswith("отцом?")
    assert "английскую королеву" in beats[0]["narration_excerpt"]
    assert all(not beat["narration_excerpt"].startswith("отцом") for beat in beats[1:])

    clips = story_generation._expand_semantic_beats_to_clips(beats)
    assert len(clips) == 5
    assert [clip["start_sec"] for clip in clips] == [0.0, 3.0, 6.0, 9.0, 12.0]
    assert all(clip["generation_duration_sec"] == 3.0 for clip in clips)


def test_story_dynamic_character_reference_does_not_force_grandma(tmp_path):
    ref_path = tmp_path / "antonin.png"
    ref_path.write_bytes(b"antonin-reference")
    scene = {
        "generation_mode": "story",
        "characters": ["antonin_young"],
        "references": {"antonin_young": str(ref_path)},
        "start_frame_prompt": "Young Antonin in revolutionary Paris",
    }

    assert _load_scene_reference_images(scene) == [b"antonin-reference"]


def test_story_scene_plan_keeps_exact_windows_and_forces_cat_majority(monkeypatch):
    def fake_gemini(_prompt, **_kwargs):
        return json.dumps(
            {
                "scenes": [
                    {
                        "visual": f"Кадр {i}",
                        "shot_type": "close-up",
                        "era_style": "ретро 1960-х",
                        "grandma_age": "young",
                        "cat_present": False,
                        "characters": ["grandma"],
                    }
                    for i in range(4)
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    result = story_generation.generate_story_scene_plan(
        source_story="История семьи",
        voiceover_text="Однажды всё изменилось и семья сохранила память.",
        duration_sec=12,
        alignment={},
    )
    scenes = result["json_plan"]
    assert len(scenes) == 4
    assert all(scene["duration_sec"] == 3.0 for scene in scenes)
    assert all(scene["model"] == "kling_3_0" for scene in scenes)
    assert sum(bool(scene["cat_present"]) for scene in scenes) >= 3
    assert "Озвучка рядом" in result["text_plan"]
    assert "Персонажи:" in result["text_plan"]
    assert "Время: прошлое" in result["text_plan"]


def test_grandma_project_forces_cat_majority_with_db_project_characters(monkeypatch):
    monkeypatch.setattr(
        story_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "scenes": [
                    {
                        "visual": f"Исторический кадр {index}",
                        "time_period": "past",
                        "characters": [] if index < 3 else ["historical_person"],
                        "cat_present": False,
                    }
                    for index in range(5)
                ]
            },
            ensure_ascii=False,
        ),
    )
    project_characters = [
        {
            "id": "grandma",
            "display_name": "Бабушка",
            "description": "бабушка проекта",
            "asset_uri": "assets/characters/grandma/base.jpeg",
        },
        {
            "id": "cat",
            "display_name": "Тимоша",
            "description": "рыжий кот проекта",
            "asset_uri": "assets/characters/cat/base.png",
        },
    ]

    scenes = story_generation.generate_story_scene_plan(
        source_story="История блюда",
        voiceover_text="Первая фраза. Вторая фраза. Третья фраза.",
        duration_sec=15,
        alignment={},
        project_characters=project_characters,
        project_slug="grandma_kitchen",
    )["json_plan"]

    assert sum("cat" in scene["characters"] for scene in scenes) >= math.ceil(len(scenes) * 0.60)
    assert sum(bool(scene["cat_present"]) for scene in scenes) >= math.ceil(len(scenes) * 0.60)
    assert all("cat" in scene["characters"] for scene in scenes[:3])


def test_story_scene_plan_requires_and_enforces_varied_camera_directions(monkeypatch):
    captured_prompt = ""

    def fake_gemini(prompt, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "scenes": [
                    {"visual": "Первый кадр", "shot_type": "wide", "camera_direction": "static shot"},
                    {"visual": "Второй кадр", "shot_type": "medium"},
                    {"visual": "Третий кадр", "shot_type": "close-up"},
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    result = story_generation.generate_story_scene_plan(
        source_story="История",
        voiceover_text="Первый эпизод. Второй эпизод. Третий эпизод.",
        duration_sec=9,
        alignment={},
    )

    scenes = result["json_plan"]
    assert "camera_direction" in captured_prompt
    assert "slow optical zoom-in within the existing frame" in captured_prompt
    assert "Никогда не используй tilt up" in captured_prompt
    assert "tracking shot" in captured_prompt
    assert "POV shot" in captured_prompt
    assert [scene["camera_direction"] for scene in scenes] == [
        "slow optical zoom-in within the existing frame",
        "tracking shot",
        "POV shot",
    ]
    assert "камера: slow optical zoom-in within the existing frame" in result["text_plan"]


def test_story_scene_plan_keeps_location_until_a_meaningful_transition(monkeypatch):
    captured_prompt = ""

    def fake_gemini(prompt, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "scenes": [
                    {
                        "visual": "Герой начинает дело",
                        "time_period": "present",
                        "continuity_anchors": "hero posture, window light",
                    },
                    {"visual": "Герой продолжает дело", "time_period": "present"},
                    {"visual": "Герой завершает дело", "time_period": "present"},
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    result = story_generation.generate_story_scene_plan(
        source_story="История действия",
        voiceover_text="Он начал. Затем продолжил. Наконец завершил.",
        duration_sec=9,
        alignment={},
    )

    scenes = result["json_plan"]
    assert "location, lighting, color_palette" in captured_prompt
    assert "Сменить место внутри ролика можно только как ясное сюжетное событие" in captured_prompt
    assert "не делай необъяснимых скачков" in captured_prompt
    assert all(scene["location"] and scene["lighting"] and scene["color_palette"] for scene in scenes)
    assert len({scene["location"] for scene in scenes}) == 1
    assert len({scene["lighting"] for scene in scenes}) == 1
    assert len({scene["color_palette"] for scene in scenes}) == 1
    assert scenes[0]["continuity_anchors"] == ["hero posture, window light"]
    assert "Локация:" in result["text_plan"]


def test_story_scene_plan_allows_explained_move_and_locks_new_location(monkeypatch):
    def fake_gemini(_prompt, **_kwargs):
        return json.dumps(
            {
                "scenario_environment": {
                    "location": "домашняя гостиная",
                    "lighting": "мягкий утренний свет",
                    "color_palette": "кремовый и голубой",
                },
                "scenes": [
                    {"visual": "Герой собирает сумку дома", "time_period": "present"},
                    {
                        "visual": "Герой приходит на тренировку",
                        "time_period": "present",
                        "location": "просторный спортивный зал",
                        "lighting": "яркий ровный дневной свет",
                        "color_palette": "синий, белый и графитовый",
                        "location_segment": "gym",
                        "location_transition": True,
                        "transition_note": "Герой выходит из дома и приходит в зал на тренировку.",
                    },
                    {
                        "visual": "Герой завершает тренировку",
                        "time_period": "present",
                        "location": "случайное кафе, которое не нужно сюжету",
                    },
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    scenes = story_generation.generate_story_scene_plan(
        source_story="Герой решил начать тренироваться.",
        voiceover_text="Он собрался. Пришёл в зал. Закончил тренировку.",
        duration_sec=9,
        alignment={},
    )["json_plan"]

    assert [scene["location"] for scene in scenes] == [
        "домашняя гостиная",
        "просторный спортивный зал",
        "просторный спортивный зал",
    ]
    assert scenes[1]["location_transition"] is True
    assert scenes[2]["location_transition"] is False
    assert scenes[2]["lighting"] == "яркий ровный дневной свет"
    assert scenes[2]["color_palette"] == "синий, белый и графитовый"


def test_story_production_always_keeps_approved_environment_in_start_prompt(monkeypatch):
    monkeypatch.setattr(
        story_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "prompts": [
                    {
                        "start_frame_prompt_ru": "Герой поднимает голову, вертикальный кадр 9:16.",
                        "video_prompt_en": (
                            "3-second Kling 3.0 image-to-video shot. No audio. No dialogue, no speech, "
                            "no lip sync, no music, no ambient sound, no on-screen text, no subtitles, no logos, no watermark."
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    scene = {
        "action": "Герой поднимает голову",
        "location": "зелёный городской парк",
        "lighting": "золотой вечерний свет",
        "color_palette": "зелёный, янтарный и небесно-голубой",
        "story_visual_style_key": "watercolor",
        "camera_direction": "tilt up",
    }

    result = story_generation.generate_story_production([scene])
    start_prompt = result["json_plan"][0]["start_frame_prompt"]
    assert "Локация: зелёный городской парк" in start_prompt
    assert "Свет: золотой вечерний свет" in start_prompt
    assert "Цветовая гамма: зелёный, янтарный и небесно-голубой" in start_prompt


def test_story_clips_request_end_frame_only_for_a_later_entrance(monkeypatch):
    def prompts(*_args, **_kwargs):
        return json.dumps({"prompts": [
            {"start_frame_characters": ["vasily", "knopka"], "end_frame_prompt_ru": "Лишний конечный кадр"},
            {"start_frame_characters": ["vasily"], "end_frame_prompt_ru": "Кнопка входит справа"},
        ]})

    monkeypatch.setattr(story_generation, "gemini_generate_sync", prompts)
    cast = {"characters": ["vasily", "knopka"], "action": "Вася смотрит в сторону двери"}
    first, second = story_generation.generate_story_production([cast, cast])["json_plan"]
    assert first["end_frame_prompt"] == "пусто" and not first["end_frame_requested"]
    assert "image-to-video" in first["video_prompt"]
    assert second["end_frame_requested"] and second["end_frame_prompt"] == "Кнопка входит справа"
    assert second["start_frame_characters"] == ["vasily"]
    assert "Keyframes" in second["video_prompt"] and "Ещё не появились: knopka" in second["start_frame_prompt"]


def test_story_historical_scene_drops_conflicting_contemporary_style(monkeypatch):
    def fake_gemini(_prompt, **_kwargs):
        return json.dumps(
            {
                "scenes": [
                    {
                        "visual": "Старинная корона и детская кепка на столе в Париже XVIII века.",
                        "shot_type": "close-up still life",
                        "era_style": "cinematic contemporary",
                        "time_period": "past",
                        "grandma_age": "none",
                        "cat_present": False,
                        "characters": [],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    result = story_generation.generate_story_scene_plan(
        source_story="История шарлотки",
        voiceover_text="Что связывает шарлотку, королеву и оставленного отцом мальчика?",
        duration_sec=3,
        alignment={},
    )

    era_style = result["json_plan"][0]["era_style"]
    assert "cinematic contemporary" not in era_style
    assert story_generation.STORY_RETRO_VISUAL_STYLE in era_style


def test_story_scene_plan_uses_selected_retro_cartoon_style(monkeypatch):
    captured_prompt = ""

    def fake_gemini(prompt, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "scenes": [
                    {
                        "visual": "Молодой повар ставит шарлотку на стол.",
                        "shot_type": "medium",
                        "era_style": "Paris, 1800s",
                        "time_period": "past",
                        "grandma_age": "none",
                        "cat_present": True,
                        "characters": ["cat"],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    result = story_generation.generate_story_scene_plan(
        source_story="История шарлотки",
        voiceover_text="Молодой повар изменил привычный десерт.",
        duration_sec=3,
        alignment={},
        story_visual_style="retro_cartoon",
    )

    scene = result["json_plan"][0]
    assert story_generation.STORY_RETRO_CARTOON_VISUAL_STYLE in captured_prompt
    assert story_generation.STORY_RETRO_CARTOON_VISUAL_STYLE in scene["era_style"]
    assert scene["story_visual_style_key"] == "retro_cartoon"
    assert scene["story_visual_style_label"] == "Ретро-мультфильм"
    assert scene["style_reference_paths"] == [story_generation.STORY_RETRO_CARTOON_REFERENCE_PATH]


def test_story_scene_plan_applies_crochet_to_present_scene_too(monkeypatch):
    monkeypatch.setattr(
        story_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "scenes": [
                    {
                        "visual": "Бабушка и внук беседуют за столом.",
                        "shot_type": "medium",
                        "time_period": "present",
                        "grandma_age": "current",
                        "characters": ["grandma"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )

    scene = story_generation.generate_story_scene_plan(
        source_story="История салата",
        voiceover_text="Бабушка начала рассказ.",
        duration_sec=3,
        alignment={},
        story_visual_style="crochet",
    )["json_plan"][0]

    assert story_generation.STORY_CROCHET_VISUAL_STYLE in scene["era_style"]
    assert scene["style_reference_paths"] == [story_generation.STORY_CROCHET_REFERENCE_PATH]


def test_story_scene_plan_primary_uses_only_character_reference_style(monkeypatch):
    captured_prompt = ""

    def fake_gemini(prompt, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "scenes": [
                    {
                        "visual": "Героиня входит в старинный дом.",
                        "shot_type": "medium",
                        "era_style": "cinematic sepia watercolor",
                        "time_period": "past",
                        "characters": ["hero"],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    result = story_generation.generate_story_scene_plan(
        source_story="История героини",
        voiceover_text="Она вернулась в дом своего детства.",
        duration_sec=3,
        alignment={},
        story_visual_style="primary",
        project_characters=[
            {
                "id": "hero",
                "display_name": "Героиня",
                "description": "утверждённый персонаж",
                "asset_uri": "/tmp/hero.png",
            }
        ],
        primary_character_id="hero",
    )

    scene = result["json_plan"][0]
    assert "selected_historical_style" not in captured_prompt
    assert story_generation.STORY_RETRO_VISUAL_STYLE not in captured_prompt
    assert scene["story_visual_style_key"] == "primary"
    assert scene["story_visual_style_label"] == "Основной"
    assert scene["era_style"] == ""
    assert scene["style_reference_paths"] == []


def test_discipline_present_scenes_keep_only_needed_characters_and_finish_the_task(monkeypatch):
    captured_prompt = ""

    def fake_gemini(prompt, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "scenes": [
                    {
                        "visual": "Архивный пример происхождения метода.",
                        "shot_type": "wide",
                        "time_period": "past",
                        "characters": [],
                    },
                    {
                        "visual": "Герой применяет метод из озвучки к своей задаче.",
                        "shot_type": "medium",
                        "time_period": "present",
                        "characters": ["assistant"],
                    },
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    project_characters = [
        {
            "id": "hero",
            "display_name": "Главный герой",
            "description": "утверждённая внешность героя",
            "asset_uri": "/tmp/hero.png",
            "is_primary": True,
        },
        {
            "id": "assistant",
            "display_name": "Второй основной герой",
            "description": "второй основной персонаж",
            "asset_uri": "/tmp/assistant.png",
            "is_primary": True,
        },
    ]
    result = story_generation.generate_story_scene_plan(
        source_story="Метод помогает не откладывать задачу.",
        voiceover_text="Сначала появился метод. Затем его применяют и заканчивают дело.",
        duration_sec=6,
        alignment={},
        story_visual_style="primary",
        project_characters=project_characters,
        primary_character_id="hero",
        project_slug="discipline",
    )

    past_scene, present_scene = result["json_plan"]
    assert "Это проект «Дисциплина»" in captured_prompt
    assert "В каждом clip_visual обязательно должен быть хотя бы один" in captured_prompt
    assert "предметные кадры без героев запрещены" in captured_prompt
    assert past_scene["characters"] == ["hero"]
    assert present_scene["characters"] == ["assistant"]
    assert present_scene["story_character_labels"] == {
        "assistant": "Второй основной герой",
    }
    assert "доводят начатые дела до конца" in present_scene["action"]
    assert "результат явно отмечен как выполненный" in present_scene["continuity_anchors"][-1]


def test_discipline_first_person_sentence_uses_anastasia_and_opening_productivity_books(monkeypatch):
    captured_prompt = ""

    def fake_gemini(prompt, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "beats": [
                    {
                        "beat": 1,
                        "clip_visuals": [
                            {
                                "visual": "Герои стоят в кабинете.",
                                "time_period": "present",
                                "characters": ["vasily", "discipline_knopka"],
                            },
                            {"visual": "Герои продолжают сцену.", "time_period": "present"},
                        ],
                    },
                    {
                        "beat": 2,
                        "clip_visuals": [
                            {"visual": "Герои выбирают задачу.", "time_period": "present"},
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    project_characters = [
        {
            "id": "discipline_knopka",
            "display_name": "Кнопка",
            "description": "утверждённая внешность Кнопки",
            "asset_uri": "/tmp/knopka.png",
            "is_primary": True,
        },
        {
            "id": "vasily",
            "display_name": "Василий",
            "description": "утверждённая внешность Василия",
            "asset_uri": "/tmp/vasily.png",
            "is_primary": True,
        },
        {
            "id": "discipline_anastasia",
            "display_name": "Анастасия",
            "description": "утверждённая внешность Анастасии",
            "asset_uri": "/tmp/anastasia.png",
            "is_primary": False,
        },
    ]

    result = story_generation.generate_story_scene_plan(
        source_story="История о техниках продуктивности.",
        voiceover_text=(
            "Я прочитала за вас десятки книг по продуктивности и собрала лучшие техники, которые точно работают. "
            "Сегодня разберём пять приёмов."
        ),
        duration_sec=9,
        alignment={},
        story_visual_style="primary",
        project_characters=project_characters,
        primary_character_id="discipline_knopka",
        project_slug="discipline",
    )

    first_beat = [scene for scene in result["json_plan"] if scene["semantic_beat"] == 1]
    later_scenes = [scene for scene in result["json_plan"] if scene["semantic_beat"] != 1]
    assert first_beat
    assert first_beat[0]["characters"] == ["discipline_anastasia", "vasily", "discipline_knopka"]
    assert first_beat[0]["story_character_labels"] == {
        "discipline_anastasia": "Анастасия",
        "vasily": "Василий",
        "discipline_knopka": "Кнопка",
    }
    assert all("discipline_anastasia" in scene["characters"] for scene in first_beat)
    assert all("Atomic Habits" in scene["action"] and "Deep Work" in scene["action"] for scene in first_beat)
    assert all(scene["shot_type"] == "tight medium close-up" for scene in first_beat)
    assert all("holds a fan of several physical productivity books" in scene["action"] for scene in first_beat)
    assert all("turns their covers toward the viewer" in scene["action"] for scene in first_beat)
    assert all("Анастасия" in scene["continuity_anchors"][0] for scene in first_beat)
    assert all(scene["characters"] for scene in later_scenes)
    assert all("vasily" in scene["characters"] and "discipline_knopka" in scene["characters"] for scene in later_scenes)
    assert "может быть одна или вместе с Василием и/или Кнопкой" in captured_prompt
    assert "Getting Things Done" in captured_prompt


def test_discipline_pairs_vasily_with_knopka_in_most_vasily_scenes(monkeypatch):
    monkeypatch.setattr(
        story_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "scenes": [
                    {
                        "visual": f"Василий выполняет шаг {index}.",
                        "time_period": "present",
                        "characters": ["vasily"],
                    }
                    for index in range(5)
                ]
            },
            ensure_ascii=False,
        ),
    )
    project_characters = [
        {
            "id": "discipline_knopka",
            "display_name": "Кнопка",
            "description": "шиншилла",
            "asset_uri": "/tmp/knopka.png",
            "is_primary": True,
        },
        {
            "id": "vasily",
            "display_name": "Василий",
            "description": "молодой человек",
            "asset_uri": "/tmp/vasily.png",
            "is_primary": True,
        },
        {
            "id": "discipline_anastasia",
            "display_name": "Анастасия",
            "description": "наставница",
            "asset_uri": "/tmp/anastasia.png",
        },
    ]

    scenes = story_generation.generate_story_scene_plan(
        source_story="Пять последовательных действий.",
        voiceover_text="Первое. Второе. Третье. Четвёртое. Пятое.",
        duration_sec=15,
        alignment={},
        project_characters=project_characters,
        primary_character_id="discipline_knopka",
        project_slug="discipline",
    )["json_plan"]

    vasily_scenes = [scene for scene in scenes if "vasily" in scene["characters"]]
    assert all(scene["characters"] for scene in scenes)
    assert sum("discipline_knopka" in scene["characters"] for scene in vasily_scenes) >= math.ceil(
        len(vasily_scenes) * 0.60
    )


def test_discipline_non_first_person_scene_keeps_anastasia_when_planned(monkeypatch):
    monkeypatch.setattr(
        story_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "beats": [
                    {
                        "beat": 1,
                        "clip_visuals": [
                            {
                                "visual": "Anastasia opens the resume and she calmly chooses one field.",
                                "time_period": "present",
                                "characters": ["discipline_anastasia"],
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    project_characters = [
        {
            "id": "vasily",
            "display_name": "Василий",
            "description": "молодой человек",
            "asset_uri": "/tmp/vasily.png",
            "is_primary": True,
        },
        {
            "id": "discipline_anastasia",
            "display_name": "Анастасия",
            "description": "утверждённая внешность Анастасии",
            "asset_uri": "/tmp/anastasia.png",
            "is_primary": False,
        },
    ]

    result = story_generation.generate_story_scene_plan(
        source_story="Пример техники продуктивности.",
        voiceover_text="Откройте резюме и выберите одно поле.",
        duration_sec=3,
        alignment={},
        story_visual_style="primary",
        project_characters=project_characters,
        primary_character_id="vasily",
        project_slug="discipline",
    )

    scene = result["json_plan"][0]
    assert scene["characters"] == ["discipline_anastasia"]
    assert "Anastasia" in scene["action"]


def test_story_production_primary_does_not_forward_generated_style_prompt(monkeypatch):
    captured_prompt = ""

    def fake_gemini(prompt, **_kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(
            {
                "prompts": [
                    {
                        "start_frame_prompt_ru": "Акварельная сепия в стиле ретро.",
                        "video_prompt_en": (
                            "3-second Kling 3.0 image-to-video shot. No audio. No dialogue, no speech, "
                            "no lip sync, no music, no ambient sound, no on-screen text, no subtitles, no logos, no watermark."
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(story_generation, "gemini_generate_sync", fake_gemini)
    result = story_generation.generate_story_production(
        [
            {
                "scene": 1,
                "generation_mode": "story",
                "story_visual_style_key": "primary",
                "action": "Героиня открывает дверь дома.",
                "characters": ["hero"],
                "era_style": "",
                "shot_type": "medium",
            }
        ]
    )

    assert story_generation.STORY_RETRO_VISUAL_STYLE not in captured_prompt
    assert "exact visual language" not in captured_prompt
    assert "Акварельная сепия" not in result["json_plan"][0]["start_frame_prompt"]


def test_story_production_is_silent_kling_with_external_tts(monkeypatch):
    monkeypatch.setattr(
        story_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "prompts": [
                    {
                        "start_frame_prompt_ru": "Ретро-кухня, молодая бабушка и рыжий кот.",
                        "video_prompt_en": (
                            "3-second Kling 3.0 image-to-video shot. No audio. No dialogue, no speech, "
                            "no lip sync, no music, no ambient sound, no on-screen text, no subtitles, no logos, no watermark."
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    result = story_generation.generate_story_production(
        [
            {
                "scene": 1,
                "action": "Молодая бабушка открывает старый альбом",
                "narration_excerpt": "Всё началось с одного снимка.",
                "grandma_age": "young",
                "cat_present": True,
                "characters": ["grandma", "cat"],
                "era_style": "ретро 1960-х",
                "shot_type": "close-up",
                "camera_direction": "tilt up",
            }
        ]
    )
    scene = result["json_plan"][0]
    assert scene["model"] == "kling_3_0"
    assert scene["duration_sec"] == 3.0
    assert scene["tts"]["mode"] == "external_story_master"
    assert scene["speech_by_lang"]["ru"] == "Всё началось с одного снимка."
    assert scene["video_prompt"].startswith("3-second Kling 3.0 image-to-video shot. No audio.")
    assert "slow optical zoom-in within the existing frame" in scene["video_prompt"]
    assert "tilt up" not in scene["video_prompt"].lower()
    assert "do not reveal or generate any area outside the frame" in scene["video_prompt"]


def test_story_young_grandma_prompt_overrides_elderly_reference_age():
    prompt = _append_scene_identity_to_image_prompt(
        "Молодая бабушка открывает дверь дома в 1917 году.",
        {
            "generation_mode": "story",
            "model": "kling_3_0",
            "characters": ["grandma", "cat"],
            "grandma_age": "young",
        },
    )
    assert "возрасте примерно 22–30 лет" in prompt
    assert "приоритет над возрастом и одеждой в базовом" in prompt
    assert "не рисовать пожилую женщину" in prompt.lower()


@pytest.mark.parametrize('redirected', [False, True])
def test_timed_audio_reports_geo_restriction_without_retry(monkeypatch, redirected):
    from types import SimpleNamespace
    from unittest.mock import Mock
    monkeypatch.setenv('ELEVENLABS_API_KEY','test-key')
    response=SimpleNamespace(ok=True,status_code=200 if redirected else 302,
        url='https://geo-restriction.elevenlabs.io/' if redirected else 'https://api.elevenlabs.io/v1/text-to-dialogue/with-timestamps',
        headers={} if redirected else {'Location':'https://geo-restriction.elevenlabs.io/'},
        json=Mock(side_effect=ValueError('Expecting value')))
    post=Mock(return_value=response)
    monkeypatch.setattr('requests.post',post)
    with pytest.raises(RuntimeError,match='ограничил доступ'):
        story_generation.generate_story_audio('Утверждённый текст.',voice_id='voice')
    assert post.call_count==1
    assert post.call_args.kwargs['allow_redirects'] is False
    response.json.assert_not_called()


def test_timed_audio_non_json_does_not_resubmit_paid_request(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    monkeypatch.setenv('ELEVENLABS_API_KEY','test-key')
    response=SimpleNamespace(ok=True,status_code=200,url='https://api.elevenlabs.io/',headers={},json=Mock(side_effect=ValueError('Expecting value')))
    post=Mock(return_value=response);monkeypatch.setattr('requests.post',post)
    with pytest.raises(RuntimeError,match='проверить историю'):
        story_generation.generate_story_audio('Утверждённый текст.',voice_id='voice')
    assert post.call_count==1
