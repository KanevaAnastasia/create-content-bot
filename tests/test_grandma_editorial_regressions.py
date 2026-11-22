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


def test_editorial_rules_reach_both_brief_and_scene_plan():
    brief = build_viral_concept_brief_prompt(concept="Салат", full_recipe_text="Нарезать яйца")
    plan = build_scenes_plan_prompt({})
    for prompt in (brief, plan):
        assert "Последняя явная правка пользователя важнее" in prompt
        assert "660840" in prompt
        assert "не доказательство причины" in prompt


def test_review_does_not_reward_directorial_speech_or_truncate_user_line():
    awkward = review_first_phrase("Покажу яркий желток для крабового салата. А в конце поднесу ложку поближе.")
    natural = review_first_phrase("Ну какой же Новый год без крабового салата?")
    assert awkward["score"] < natural["score"]
    assert not natural["issues"]
    long = "Ну какой же Новый год без крабового салата с кукурузой? Сейчас я поделюсь своим любимым рецептом."
    assert _normalize_first_phrase(long, fallback="") == long
