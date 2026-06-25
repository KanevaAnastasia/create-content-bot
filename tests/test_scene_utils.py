import pytest

from app.scene_utils import (
    apply_veo3_speech_format,
    extract_recipe_steps_text,
    normalize_recipe_steps,
    normalize_scenes_for_pipeline,
    pick_model,
    scene_matches_step,
    scenes_follow_recipe_steps,
    scenes_quality_errors,
    voice_provider_for_model,
)


def test_pick_model_variants():
    assert pick_model({"text": "реплика", "has_lipsync": True, "shot_type": "closeup"}) == "veo3_fast"
    assert pick_model({"text": "реплика"}) == "veo3_fast"
    assert pick_model({"text": ""}) == "veo3_fast"


def test_voice_provider():
    assert voice_provider_for_model("veo3_fast") == "native"
    assert voice_provider_for_model("kling") == "elevenlabs"


def test_extract_and_normalize_recipe_steps():
    text = "ШАГ 1: Нарезать лук\n2) Обжарить лук\n3. Подать"
    steps = extract_recipe_steps_text(text)
    assert steps == ["Нарезать лук", "Обжарить лук", "Подать"]
    assert normalize_recipe_steps(["  a  ", " ", "b."]) == ["a", "b"]


def test_normalize_scenes_for_pipeline_adds_contract_fields():
    raw = [{"location": "stove", "action": "Жарит лук", "text": "жарим"}]
    scenes = normalize_scenes_for_pipeline(raw)
    assert len(scenes) >= 3
    assert scenes[0]["goal"] == "хук"
    assert scenes[-1]["goal"] == "результат"
    assert all(s.get("voice_needed") is True for s in scenes)
    assert all("grandma" in s.get("characters", []) for s in scenes)
    assert all("cat" in s.get("characters", []) for s in scenes)


def test_normalize_scenes_for_pipeline_rejects_non_object_items():
    with pytest.raises(ValueError, match="must contain JSON objects"):
        normalize_scenes_for_pipeline(["grandma", "cat"])  # type: ignore[arg-type]


def test_normalize_scenes_for_pipeline_preserves_existing_references():
    raw = [
        {
            "location": "kitchen",
            "action": "Жарим сырники",
            "text": "жарим",
            "references": {
                "grandma": "custom/grandma.png",
                "cat": "custom/cat.png",
                "location": "custom/location.png",
                "extra_style": "custom/style.png",
            },
        }
    ]
    scenes = normalize_scenes_for_pipeline(raw)
    refs = scenes[0]["references"]
    assert refs["grandma"] == "custom/grandma.png"
    assert refs["cat"] == "custom/cat.png"
    assert "location" not in refs
    assert refs["extra_style"] == "custom/style.png"


def test_apply_veo3_speech_format_changes_only_veo3():
    scenes = [{"model": "veo3_fast", "text": "Привет, мир."}, {"model": "kling", "text": "A"}]
    out = apply_veo3_speech_format(scenes)
    assert out[0]["text"] == "Привет, мир."
    assert out[1]["text"] == "A"


def test_apply_veo3_speech_format_keeps_long_phrase_unchanged():
    long_text = (
        "смотрите сегодня готовим сырники из творога и очень долго подробно рассказываем каждый шаг "
        "добавляем яйцо сахар соль ваниль и муку потом еще долго мешаем до однородной массы"
    )
    out = apply_veo3_speech_format([{"model": "veo3_fast", "text": long_text}])
    assert out[0]["text"] == long_text


def test_apply_veo3_speech_format_keeps_garbled_text_unchanged():
    garbled = "Осмотр тЕ нА тИ [пауза] сЫрн кИ блАк [пауза] Они т Ют вО рт"
    out = apply_veo3_speech_format([{"model": "veo3_fast", "text": garbled}])
    assert out[0]["text"] == garbled


def test_scene_match_and_follow_steps():
    scene = {"action": "Нарезаем лук мелкими кубиками", "text": "режем лук"}
    assert scene_matches_step(scene, "Нарезаем лук")
    errs = scenes_follow_recipe_steps([scene], ["Варим суп"])
    assert errs == ["scene_1: step_not_reflected"]


def test_scenes_quality_errors_flags_basic_issues():
    bad_scene = {
        "characters": [],
        "text": "",
        "action": "",
        "model": "veo3_fast",
        "duration_sec": 5,
        "location": "stove",
    }
    errs = scenes_quality_errors([bad_scene], recipe_steps=["Нарезать лук"])
    assert "scene_1: no_speech" in errs
    assert "scene_1: no_cat" in errs
    assert "scene_1: invalid_duration_for_veo3" in errs


def test_scenes_quality_errors_first_scene_requires_hook_and_dish_name():
    scene = {
        "scene": 1,
        "characters": ["grandma", "cat"],
        "text": "начинаем готовить",
        "action": "бабушка перемешивает массу",
        "goal": "хук",
        "model": "kling",
        "duration_sec": 4,
        "location": "kitchen",
    }
    errs = scenes_quality_errors([scene], recipe_steps=["Сырники из творога с ванилью"])
    assert "scene_1: first_scene_hook_missing" in errs
    assert "scene_1: first_scene_dish_name_missing" in errs


def test_scenes_quality_errors_first_scene_requires_dish_name_in_speech():
    scene = {
        "scene": 1,
        "characters": ["grandma", "cat"],
        "text": "сегОдня будет вкусно [пауза] смотрИте",
        "action": "бабушка показывает готовые сырники с котом",
        "goal": "хук",
        "model": "veo3_fast",
        "duration_sec": 6,
        "shot_type": "closeup",
    }
    errs = scenes_quality_errors([scene], recipe_steps=["Сырники из творога с ванилью"])
    assert "scene_1: first_scene_dish_name_missing" in errs


def test_scenes_quality_errors_last_scene_requires_presentation():
    scenes = [
        {
            "scene": 1,
            "characters": ["grandma", "cat"],
            "text": "смотрите готовим сырники",
            "action": "Смотрите, сегодня готовим сырники и показываем аппетитный результат.",
            "goal": "хук",
            "model": "kling",
            "duration_sec": 4,
            "location": "kitchen",
        },
        {
            "scene": 2,
            "characters": ["grandma", "cat"],
            "text": "вот и все",
            "action": "бабушка ставит тарелку на стол",
            "goal": "процесс",
            "model": "kling",
            "duration_sec": 4,
            "location": "serving",
        },
    ]
    errs = scenes_quality_errors(scenes, recipe_steps=["Сделать тесто", "Подать блюдо"])
    assert "scene_2: final_presentation_missing" in errs


def test_scenes_quality_errors_forbidden_low_angle_and_transition():
    scenes = [
        {
            "scene": 1,
            "characters": ["grandma", "cat"],
            "text": "смотрите сегодня готовим сырники",
            "action": "Смотрите, сегодня готовим сырники и показываем аппетитный результат.",
            "goal": "хук",
            "model": "veo3_fast",
            "duration_sec": 6,
            "location": "kitchen",
            "shot_type": "medium",
        },
        {
            "scene": 2,
            "characters": ["grandma", "cat"],
            "text": "делаем шаг",
            "action": "бабушка смешивает ингредиенты",
            "goal": "процесс",
            "model": "veo3_fast",
            "duration_sec": 6,
            "location": "kitchen",
            "shot_type": "medium",
        },
    ]
    errs = scenes_quality_errors(scenes, recipe_steps=["Сырники", "Смешать ингредиенты"])
    assert "scene_2: no_location_or_shot_change" in errs

    scenes[1]["shot_type"] = "low angle"
    errs2 = scenes_quality_errors(scenes, recipe_steps=["Сырники", "Смешать ингредиенты"])
    assert "scene_2: low_angle_forbidden" in errs2


def test_veo3_allows_first_uppercase_letter():
    scene = {
        "scene": 1,
        "characters": ["grandma", "cat"],
        "text": "Сейчас гОтовим [пауза] начИнАем",
        "action": "Смотрите, сегодня готовим сырники и показываем аппетитный результат.",
        "goal": "хук",
        "model": "veo3_fast",
        "duration_sec": 6,
        "location": "kitchen",
        "shot_type": "medium",
    }
    errs = scenes_quality_errors([scene], recipe_steps=["Сырники"])
    assert "scene_1: veo3_text_has_non_vowel_uppercase" not in errs
