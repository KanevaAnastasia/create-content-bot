import json

from app import advertising_generation as ads
from app.handlers import handlers_media_scene as media
from app.pipeline_llm_steps import build_viral_concept_brief_prompt, build_scenes_plan_prompt
from app.viral_retention import _normalize_first_phrase, review_first_phrase


def test_packaging_and_negative_dog_mentions_do_not_add_advertising_cast(monkeypatch):
    monkeypatch.setattr(media, "ASSETS", {"characters": {
        key: {"base": f"/{key}.png"} for key in ("grandma", "cat", "dog")
    }})
    scene = {"generation_mode": "advertising", "characters": ["grandma", "cat"],
             "start_frame_prompt": "На этикетке рисунок собаки. Собак в комнате нет.",
             "references": {"dog": "/dog.png"}}
    prompt = media._append_scene_identity_to_image_prompt(scene["start_frame_prompt"], scene)
    assert "Персонажи в кадре: бабушка, рыжий кот." in prompt
    assert "Домашний мультяшный пес" not in prompt
    refs = media._resolve_scene_references(scene)
    assert media._reference_keys_for_scene(scene, refs) == ["grandma", "cat"]
    scene["characters"].append("dog")
    assert "dog" in media._reference_keys_for_scene(scene, refs)


def test_production_preserves_long_approved_speech_when_model_rewrites_it(monkeypatch):
    approved = "Ну какой же Новый год без крабового салата с кукурузой? Сейчас я поделюсь своим любимым рецептом. Тимоша уже ждёт у стола!"
    monkeypatch.setattr(ads, "gemini_generate_sync", lambda *a, **k: json.dumps({"prompts": [{
        "start_frame_prompt_ru": "Бабушка у стола", "speech_by_lang": {"ru": "Новая короткая фраза"},
        "video_prompt_by_lang": {"ru": "CHARACTERS SPEAKS: Новая короткая фраза"}
    }]}))
    scene = ads.generate_advertising_production(
        [{"text": approved, "characters": ["grandma", "cat"], "action": "Ставит салат"}],
        product_brief={"product_name": "Миска"})["json_plan"][0]
    assert scene["speech_by_lang"]["ru"] == approved
    assert f"CHARACTERS SPEAKS: {approved}" in scene["video_prompt"]
    assert "Новая короткая фраза" not in scene["video_prompt"]
    from app.pipeline import GRANDMA_VOICE_LOCK_FULL
    assert f"voice_lock_full_grandma: {GRANDMA_VOICE_LOCK_FULL}" in scene["video_prompt"]
    assert "no instrumental score, songs, jingles" in scene["video_prompt"]
    assert "do not generate captions, titles, ingredient labels" in scene["video_prompt"]
    assert "voice_identity_continuity:" in scene["video_prompt"]


def test_editorial_rules_reach_both_brief_and_scene_plan():
    brief = build_viral_concept_brief_prompt(concept="Салат", full_recipe_text="Нарезать яйца")
    plan = build_scenes_plan_prompt({})
    for prompt in (brief, plan):
        assert "Последняя явная правка пользователя важнее" in prompt
        assert "660840" in prompt
        assert "не доказательство причины" in prompt
        assert "ПРАВИЛО БУКВЫ П ДЛЯ ПЕРВОЙ СЦЕНЫ" in prompt
        assert "Это не акроним из слов на П" in prompt
        assert "какой один вопрос у него остался и где сюжет ответит" in prompt
        assert "Утверждённое пользователем начало сохраняй дословно" in prompt


def test_review_does_not_reward_directorial_speech_or_truncate_user_line():
    awkward = review_first_phrase("Покажу яркий желток для крабового салата. А в конце поднесу ложку поближе.")
    natural = review_first_phrase("Ну какой же Новый год без крабового салата?")
    assert awkward["score"] < natural["score"]
    assert not natural["issues"]
    long = "Ну какой же Новый год без крабового салата с кукурузой? Сейчас я поделюсь своим любимым рецептом."
    assert _normalize_first_phrase(long, fallback="") == long


def test_episode_prop_design_is_locked_without_adding_invisible_objects():
    from app.grandma_scene_contract import lock_episode_props, append_episode_prop_locks
    scenes = [
        {"prop_descriptions": {"toy_fish": "Одна голубая плюшевая рыбка с жёлтым хвостом."}},
        {"prop_descriptions": {"toy_fish": "Красная пластиковая рыба."}},
        {"prop_descriptions": {}},
    ]
    lock_episode_props(scenes)
    assert scenes[1]["prop_descriptions"] == scenes[0]["prop_descriptions"]
    assert scenes[2]["prop_descriptions"] == {}
    assert scenes[2]["episode_prop_catalog"] == scenes[0]["prop_descriptions"]
    assert append_episode_prop_locks("Кадр", scenes[2]) == "Кадр"
    prompt = append_episode_prop_locks("Кадр", scenes[1])
    assert "Красная" not in prompt
    assert append_episode_prop_locks(prompt, scenes[1]) == prompt


def test_single_take_replaces_legacy_camera_movement_and_preserves_speech():
    from app.pipeline import append_veo3_video_framing_constraints, VEO3_FAST_VIDEO_FRAMING_RULE
    scene = {"single_take": True, "model": "veo3_fast", "characters": ["grandma", "cat"]}
    raw = 'CHARACTERS SPEAKS: Ну что, к столу! ' + VEO3_FAST_VIDEO_FRAMING_RULE
    result = append_veo3_video_framing_constraints(raw, scene)
    assert "angle shifts" not in result
    assert "locked-camera" in result
    assert "never eats" in result
    assert "CHARACTERS SPEAKS: Ну что, к столу!" in result
    assert append_veo3_video_framing_constraints(result, scene) == result


def test_cutting_verbs_are_not_mistaken_for_neighbor_cat_name():
    from app.pipeline import _strip_neighbor_cat_subplot_from_scenes
    from app.scene_utils import normalize_scenes_for_pipeline
    action = "Бабушка нарезает палочки, нарезая их мелкими кубиками."
    scene = {"text": "Я режу палочки.", "action": action, "characters": ["grandma", "cat"]}
    normalized = normalize_scenes_for_pipeline([dict(scene) for _ in range(3)])
    assert "zaya" not in normalized[0]["characters"]
    assert _strip_neighbor_cat_subplot_from_scenes(normalized)[0]["action"] == action
    with_zaya = {**scene, "action": action + " Белая кошка Зая сидит рядом.", "characters": ["grandma", "cat", "zaya"]}
    cleaned = _strip_neighbor_cat_subplot_from_scenes([with_zaya])[0]
    assert "нарезает" in cleaned["action"]
    assert "Зая" not in cleaned["action"]


def test_provider_prompt_keeps_canonical_grandma_voice_despite_generic_lock():
    from app.handlers.handlers_media_core import enforce_video_prompt_voice_requirements
    from app.pipeline import GRANDMA_VOICE_LOCK_FULL
    scene = {"model": "veo3_fast", "single_take": True, "characters": ["grandma", "cat"], "text": "Салат готов!"}
    prompt = "voice_lock_full: generic voice.\nCHARACTERS SPEAKS: Салат готов!"
    out = enforce_video_prompt_voice_requirements(prompt, scene)
    assert f"voice_lock_full_grandma: {GRANDMA_VOICE_LOCK_FULL}" in out
    assert "CHARACTERS SPEAKS: Салат готов!" in out
    assert "no instrumental score, songs, jingles" in out
    assert "voice_identity_continuity:" in out
    assert enforce_video_prompt_voice_requirements(out, scene) == out


def test_close_framing_reaches_plan_image_and_video_without_full_body_conflict():
    from app.pipeline_llm_steps import scenes_plan_requirements
    from app.pipeline import append_veo3_video_framing_constraints
    from app.image_atmosphere import add_image_atmosphere
    from app.series_theme import HOLIDAY_DIRECTION
    plan = scenes_plan_requirements()["frame_framing_rule"]
    image = add_image_atmosphere("Бабушка перемешивает салат.")
    for single_take in (True, False):
        video = append_veo3_video_framing_constraints("Cooking", {
            "model": "veo3_fast", "single_take": single_take, "characters": ["grandma", "cat"]})
        for prompt in (plan, image, video):
            assert "waist-up" in prompt
            assert "full figure" not in prompt
            assert "fully visible" not in prompt
    assert "Never widen" in HOLIDAY_DIRECTION
    assert "whole tree" in HOLIDAY_DIRECTION


def test_retention_and_camera_catalogs_reach_both_planning_prompts():
    from app.discipline_craft import TECHNIQUES
    for prompt in (build_viral_concept_brief_prompt(concept='Мандариновый десерт', full_recipe_text='Мандарины, сливки'), build_scenes_plan_prompt({})):
        for technique in TECHNIQUES['attention'] + TECHNIQUES['camera']:
            assert technique in prompt
        assert 'camera_design' in prompt
        assert 'новогодний стол' in prompt
        assert 'Неподвижная камера внутри сцены обязательна' in prompt


def test_explicit_family_plan_overrides_sweet_dish_classification():
    from app.pipeline import _apply_family_cast_rules
    scenes = [{'characters': ['grandma', 'cat', 'grandson']} for _ in range(3)]
    _apply_family_cast_rules(scenes, viral_concept_brief={'dish_audience_type': 'child', 'family_cast_plan': 'Только бабушка и Тимоша.'})
    assert all(s['characters'] == ['grandma', 'cat'] for s in scenes)


def test_single_take_veo_keeps_face_visible_even_during_silent_action():
    from app.pipeline import append_veo3_video_framing_constraints
    out = append_veo3_video_framing_constraints('Grandma lifts a lid.', {'model':'veo3_fast', 'single_take':True, 'characters':['grandma','cat']})
    assert 'EVERY frame, including silent moments' in out
    assert 'no face-free inserts or rear views' in out
