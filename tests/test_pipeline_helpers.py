import asyncio
import json

import pytest

from app import pipeline
from app import pipeline_llm_steps
from app.pipeline import (
    START_FRAME_RECIPE_VISUAL_DETAIL_RULE,
    _bind_recipe_steps,
    _extract_production_prompts,
    _normalize_production_prompts,
    _normalize_frame_prompts,
    _ensure_scene_block_len,
    _enforce_voice_lock_if_needed,
    _humanize_scene_errors,
    _should_apply_voice_lock,
    _speaker_voice_params,
    _tts_payload_for_scene,
    append_veo3_video_framing_constraints,
    generate_frame_prompts_for_approval,
    generate_full_production,
    generate_recipe_from_source_text,
    generate_recipe_full_text,
    generate_recipe_steps,
    generate_viral_concept_brief,
    generate_script,
)


def test_generate_recipe_steps_prompt_states_length_is_model_only(monkeypatch):
    captured: list[str] = []

    def fake_gemini(prompt: str) -> str:
        captured.append(prompt)
        return json.dumps(
            [f"нарезать овощи мелко шаг номер {i} подробно" for i in range(1, 5)],
            ensure_ascii=False,
        )

    monkeypatch.setattr(pipeline, "get_recipe_steps", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "set_recipe_steps", lambda *a, **k: None)
    monkeypatch.setattr("app.pipeline.gemini_generate_sync", fake_gemini)
    out = asyncio.run(generate_recipe_steps("салат", dish_name="салат"))
    assert len(out) == 4
    assert captured and "не дополняют" in captured[0]
    assert "не валидирует" in captured[0]


def test_build_concepts_prompt_requests_simple_and_viral_sets():
    prompt = pipeline_llm_steps.build_concepts_prompt("ужин за 15 минут", "", [])
    assert "2–3 концепта" in prompt
    assert "максимально простым рецептом" in prompt
    assert "простыми, доступными ингредиентами" in prompt
    assert "2–3 максимально виральных концепта" in prompt


def test_speaker_voice_params_profiles():
    assert _speaker_voice_params("grandma")["voice_profile"] == "grandma_ru_warm_70yo"
    assert _speaker_voice_params("unknown")["voice_profile"] == "ru_neutral_default"


def test_tts_payload_native_and_elevenlabs(monkeypatch):
    native = _tts_payload_for_scene({"voice_provider": "native", "text": "привет"})
    assert native["provider"] == "native"
    assert native["voice_profile"] == "native_scene_voice"

    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice123")
    el = _tts_payload_for_scene({"voice_provider": "elevenlabs", "text": "привет", "characters": ["grandma"]})
    assert el["provider"] == "elevenlabs"
    assert el["voice_id"] == "voice123"
    assert "speaker_profiles" in el


def test_humanize_scene_errors():
    errs = _humanize_scene_errors(["scene_2: no_food", "banned_abstraction"])
    assert "сцена 2: не видно еды/ингредиентов" in errs
    assert "абстрактные формулировки в сценах" in errs


def test_humanize_scene_errors_new_contract_rules():
    errs = _humanize_scene_errors(
        [
            "scene_1: first_scene_hook_missing",
            "scene_1: first_scene_dish_name_missing",
            "scene_4: final_presentation_missing",
        ]
    )
    assert "сцена 1: в первой сцене нет хука" in errs
    assert "сцена 1: в первой сцене не названо, что готовим" in errs
    assert "сцена 4: в финальной сцене нет презентации блюда" in errs


def test_should_apply_voice_lock_conditions():
    assert _should_apply_voice_lock({"model": "veo3_fast", "text": ""}) is True
    assert _should_apply_voice_lock({"model": "kling", "voice_needed": True, "text": ""}) is True
    assert _should_apply_voice_lock({"model": "kling", "voice_needed": False, "text": "реплика"}) is True
    assert _should_apply_voice_lock({"model": "kling", "voice_needed": False, "text": ""}) is False


def test_enforce_voice_lock_if_needed():
    plain = "fixed camera, clean kitchen"
    out = _enforce_voice_lock_if_needed(plain, {"model": "veo3_fast"})
    assert "voice_lock_full:" in out

    same = _enforce_voice_lock_if_needed(out, {"model": "veo3_fast"})
    assert same.count("voice_lock_full:") == 1

    no_need = _enforce_voice_lock_if_needed(plain, {"model": "kling", "voice_needed": False, "text": ""})
    assert no_need == plain


def test_append_veo3_video_framing_constraints_idempotent_for_veo3():
    scene = {"model": "veo3_fast"}
    once = append_veo3_video_framing_constraints("kitchen close-up", scene)
    assert "veo3_framing:" in once.lower()
    assert "veo3_cartoon_character_rule:" not in once.lower()
    assert "kitchen_physics_rule:" not in once.lower()
    twice = append_veo3_video_framing_constraints(once, scene)
    assert twice == once


def test_append_veo3_video_framing_constraints_skips_non_veo_model():
    scene = {"model": "kling", "voice_needed": False, "text": ""}
    out = append_veo3_video_framing_constraints("wide shot", scene)
    assert out == "wide shot"
    assert "veo3_framing" not in out.lower()


def test_append_veo3_video_framing_constraints_adds_cooking_cast_rule_by_default(monkeypatch):
    monkeypatch.delenv("CONTENT_FABRIC_PROJECT_SCOPE", raising=False)
    out = append_veo3_video_framing_constraints("close-up kitchen", {"model": "veo3_fast"})
    assert "veo3_cast_rule:" in out.lower()
    assert "grandmother and the orange cat" in out.lower()
    assert "character_identity_rule:" not in out.lower()
    assert "Зая" not in out
    assert "Маркиз" not in out
    assert "cartoon grandmother" not in out
    assert "cartoon cat" not in out


def test_append_veo3_video_framing_constraints_skips_cooking_cast_rule_for_other_scope(monkeypatch):
    monkeypatch.setenv("CONTENT_FABRIC_PROJECT_SCOPE", "travel_blog")
    out = append_veo3_video_framing_constraints("close-up kitchen", {"model": "veo3_fast"})
    assert "veo3_framing:" in out.lower()
    assert "veo3_cast_rule:" not in out.lower()
    assert "veo3_cartoon_character_rule:" not in out.lower()
    assert "kitchen_physics_rule:" not in out.lower()


def test_finalize_scene_video_prompt_adds_voice_lock_and_veo_framing():
    scene = {"model": "veo3_fast", "text": "реплика сцены"}
    out = pipeline._finalize_scene_video_prompt("close-up kitchen", scene)
    assert "voice_lock_full:" in out
    assert "veo3_framing:" in out.lower()
    assert "cartoon cat" not in out
    assert "kitchen_physics_rule:" not in out.lower()
    assert out.startswith(
        pipeline._enforce_voice_lock_if_needed("close-up kitchen", scene)
    )


def test_pipeline_llm_steps_scenes_payload_carries_feedback_and_previous():
    p = pipeline_llm_steps.build_scenes_plan_payload(
        "сырники",
        "творог яйца мука",
        "больше хука",
        [{"scene": 1}],
        ["kitchen"],
    )
    assert p["feedback"] == "больше хука"
    assert p["previous_scenes"] == [{"scene": 1}]
    prompt = pipeline_llm_steps.build_scenes_plan_prompt(p)
    assert "больше хука" in prompt
    assert "previous_scenes" in prompt
    assert "speech_first_person_pov" in prompt
    assert "первого лица" in prompt
    assert "Готовка по рецепту" in prompt
    assert "начинается строго со СЦЕНЫ 2" in prompt
    assert "режим РЕДАКТИРОВАНИЯ" in prompt
    assert "romantic_drama_subplot" in prompt
    assert "CHARACTER KEYS AND SHORT DESCRIPTIONS" in prompt
    assert "payload.character_short_descriptions" in prompt
    assert "черно-белый кот может быть ТОЛЬКО ЗА ОКНОМ" in prompt
    assert "белая кошка преимущественно находится ЗА ОКНОМ" in prompt
    assert "CAT COUNT RULE" in prompt
    assert "Не создавай дополнительных котов" in prompt
    assert "Не добавляй лишних персонажей" in prompt
    assert "не вставляй в результат громоздкие служебные метки" in prompt
    assert "подробные описания внешности" in prompt
    assert "ВЕСЬ ролик" in prompt
    assert "ТЫ САМ придумываешь" in prompt
    assert "ОСМЫСЛЕННОЙ" in prompt
    assert "хэппи-энд" in prompt
    assert "happy_end" in prompt
    assert "romantic_drama_episode" in prompt
    assert pipeline_llm_steps.FINAL_SCENE_SUBSCRIBE_CTA in prompt
    assert pipeline_llm_steps.FINAL_SCENE_SUBSCRIBE_CTA_VEO3 in prompt


def test_scenes_payload_can_enable_romantic_drama(monkeypatch):
    monkeypatch.setattr(
        pipeline_llm_steps,
        "maybe_choose_romantic_subplot",
        lambda: {
            "enabled": True,
            "characters": ["zaya", "markiz"],
            "brief": "",
            "zaya_timosha_arc": "",
            "markiz_arc": "",
        },
    )
    payload = pipeline_llm_steps.build_scenes_plan_payload("пирог", "замесить и испечь", "", [])

    assert payload["romantic_drama_subplot"]["enabled"] is True
    assert payload["romantic_drama_subplot"]["characters"] == ["zaya", "markiz"]
    assert "character_descriptions" not in payload
    assert payload["character_short_descriptions"]["cat"] == "рыжий кот"


def test_generate_scenes_plan_text_reports_romantic_drama(monkeypatch):
    scenes_payload = [
        {
            "scene": i + 1,
            "shot_type": "closeup",
            "characters": ["grandma", "cat", "zaya", "markiz"],
            "action": f"действие {i+1} с едой",
            "text": "я мешАю [пауза] аккурАтно",
            "duration_sec": 6,
            "model": "veo3_fast",
            "voice_needed": True,
            "has_lipsync": True,
            "romantic_drama_episode": {
                "brief": "Маркиз ревнует к ложке, которую Зая похвалила у Тимоши.",
                "zaya_timosha_arc": "Тимоша старается показать Зае, что он аккуратный помощник у миски.",
                "markiz_arc": "Маркиз весь ролик пытается встать ближе к Зае и миске.",
                "happy_end": "Зая и Тимоша радуются у окна, а Маркиз спокойно улыбается снаружи.",
            },
            "romantic_drama_beat": f"beat {i+1}",
        }
        for i in range(4)
    ]

    monkeypatch.setattr(
        pipeline_llm_steps,
        "maybe_choose_romantic_subplot",
        lambda: {
            "enabled": True,
            "characters": ["zaya", "markiz"],
            "brief": "",
            "zaya_timosha_arc": "",
            "markiz_arc": "",
            "episode_rule": "whole episode",
        },
    )
    monkeypatch.setattr(pipeline, "get_scenes_plan", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "set_scenes_plan", lambda *a, **k: None)
    async def _fake_gemini_text(_prompt: str) -> str:
        return json.dumps(scenes_payload, ensure_ascii=False)

    monkeypatch.setattr("app.pipeline_llm_steps.gemini_text", _fake_gemini_text)

    out = asyncio.run(pipeline.generate_scenes_plan("пирог", full_recipe_text="замесить и испечь"))

    assert out["romantic_drama_subplot"]["enabled"] is True
    assert out["romantic_drama_subplot"]["brief"] == "черно-белый кот ревнует к ложке, которую белая кошка похвалила у рыжего кота."
    assert "Драма с персонажем: белая кошка - да" in out["text_plan"]
    assert "Дополнительный соседский кот: черно-белый кот - да" in out["text_plan"]
    assert "черно-белый кот ревнует к ложке" in out["text_plan"]
    assert "рыжий кот + белая кошка" in out["text_plan"]
    assert out["romantic_drama_subplot"]["happy_end"] == "белая кошка и рыжий кот радуются у окна, а черно-белый кот спокойно улыбается снаружи."
    assert "Хэппи-энд: белая кошка и рыжий кот радуются у окна" in out["text_plan"]
    assert "Зая" not in out["text_plan"]
    assert "Маркиз" not in out["text_plan"]
    assert "Сквозное правило" in out["text_plan"]


def test_generate_scenes_plan_infers_and_keeps_zaya_drama_from_refine(monkeypatch):
    scenes_payload = [
        {
            "scene": i + 1,
            "shot_type": "closeup",
            "characters": ["grandma", "cat"] if i == 0 else ["grandma", "cat", "zaya"],
            "action": f"действие {i+1} с едой",
            "text": "я мешАю [пауза] аккурАтно",
            "duration_sec": 6,
            "model": "veo3_fast",
            "voice_needed": True,
            "has_lipsync": True,
            "romantic_drama_episode": {
                "brief": "Зая ждёт знак внимания от Тимоши у окна.",
                "zaya_timosha_arc": "Тимоша старается впечатлить Заю аккуратной готовкой.",
                "markiz_arc": "",
                "happy_end": "Зая и Тимоша радуются вместе у окна.",
            },
        }
        for i in range(4)
    ]

    monkeypatch.setattr(pipeline, "get_scenes_plan", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "set_scenes_plan", lambda *a, **k: None)

    async def _fake_gemini_text(_prompt: str) -> str:
        return json.dumps(scenes_payload, ensure_ascii=False)

    monkeypatch.setattr("app.pipeline_llm_steps.gemini_text", _fake_gemini_text)

    out = asyncio.run(
        pipeline.generate_scenes_plan(
            "пирог",
            full_recipe_text="замесить и испечь",
            user_feedback="добавь драму с Заей",
            previous_scenes=[{"scene": 1}],
            romantic_drama_subplot={"enabled": False, "characters": []},
        )
    )

    assert out["romantic_drama_subplot"]["enabled"] is True
    assert out["romantic_drama_subplot"]["characters"] == ["zaya"]
    assert all("zaya" in scene["characters"] for scene in out["json_plan"])
    assert all(scene.get("romantic_drama_beat") for scene in out["json_plan"])


def test_viral_concept_brief_prompt_contract():
    prompt = pipeline_llm_steps.build_viral_concept_brief_prompt(
        concept="медовик",
        full_recipe_text="Ингредиенты: мед, мука. Собрать торт.",
        sponsored_content=True,
        brand_context="мед бренда",
    )
    assert "Viral Concept Brief" in prompt
    assert "first_frame_type" in prompt
    assert "hooks должен быть объектом" in prompt
    assert "brand_context" in prompt


def test_generate_viral_concept_brief_fallback_on_bad_json(monkeypatch):
    async def fake_text(prompt: str) -> str:
        return "not json"

    monkeypatch.setattr(pipeline_llm_steps, "gemini_text", fake_text)
    out = asyncio.run(
        generate_viral_concept_brief(
            "медовик",
            full_recipe_text="Ингредиенты: мед, мука. Собрать торт.",
        )
    )
    assert out["main_hook"]
    assert len(out["hooks"]["emotional"]) >= 3


def test_generate_recipe_full_text_mocked(monkeypatch):
    def fake_gemini(prompt: str) -> str:
        return "Ингредиенты: мука, вода.\n\nЗамесить тесто и испечь до готовности."

    monkeypatch.setattr("app.pipeline_llm_steps.gemini_generate_sync", fake_gemini)
    out = asyncio.run(generate_recipe_full_text("лепёшка на сковороде", dish_name="лепёшка"))
    assert "мука" in out.lower()


def test_generate_recipe_from_source_text_mocked(monkeypatch):
    captured: list[str] = []

    def fake_gemini(prompt: str) -> str:
        captured.append(prompt)
        return "```text\nИнгредиенты: творог, яйцо, мука.\n\nСмешайте ингредиенты и обжарьте до румяности.\n```"

    monkeypatch.setattr("app.pipeline_llm_steps.gemini_generate_sync", fake_gemini)
    out = asyncio.run(
        generate_recipe_from_source_text(
            "длинный текст страницы с рекламой и рецептом",
            source_url="https://example.com/recipe",
        )
    )
    assert "Ингредиенты" in out
    assert "обжарьте" in out.lower()
    assert captured
    assert "ИСТОЧНИК РЕЦЕПТА (URL)" in captured[0]
    assert "ТЕКСТ ИСТОЧНИКА" in captured[0]


def test_generate_recipe_from_source_text_rejects_model_extraction_failure(monkeypatch):
    def fake_gemini(_prompt: str) -> str:
        return (
            "К сожалению, я не могу извлечь рецепт из предоставленного текста, "
            "так как это сообщение об ошибке, указывающее на необходимость включить поддержку JavaScript."
        )

    monkeypatch.setattr("app.pipeline_llm_steps.gemini_generate_sync", fake_gemini)
    try:
        asyncio.run(
            generate_recipe_from_source_text(
                "Для доступа к сайту необходимо включить поддержку JavaScript.",
                source_url="https://example.com/recipe",
            )
        )
    except RuntimeError as exc:
        assert "не найден рецепт" in str(exc)
    else:
        raise AssertionError("Expected model extraction failure to be rejected")


def test_parse_video_stages_from_llm_json_and_legacy():
    j = '["один два три четыре пять шесть", "семь восемь девять десять одиннадцать двенадцать"]'
    out = pipeline._parse_video_stages_from_llm(j)
    assert len(out) == 2
    legacy = "ШАГ 1: один два три четыре пять шесть\nШАГ 2: семь восемь девять десять одиннадцать"
    out2 = pipeline._parse_video_stages_from_llm(legacy)
    assert len(out2) == 2


def test_postprocess_scenes_output_sets_recipe_step_from_text():
    scenes = [
        {
            "action": "English action only",
            "text": "рЕплика [пауза] одна",
            "model": "veo3_fast",
            "duration_sec": 4,
            "characters": ["grandma", "cat"],
            "location": "kitchen",
            "shot_type": "closeup",
        }
    ]
    out = pipeline._postprocess_scenes_output(scenes, "сырники")
    assert out[0]["recipe_step"].startswith("рЕплика [пауза] одна")
    assert out[0]["text"].startswith("рЕплика [пауза] одна")
    assert not out[0]["text"].endswith(pipeline.FINAL_SCENE_SUBSCRIBE_CTA_VEO3)
    assert out[0]["duration_sec"] == 4
    assert out[0]["action"] == "English action only"


def test_postprocess_scenes_output_keeps_final_cta_at_end_without_duplicate():
    scenes = [
        {"scene": 1, "text": "первый шаг", "model": "veo3_fast", "duration_sec": 6},
        {
            "scene": 2,
            "text": f"всё готОво [пауза] {pipeline.FINAL_SCENE_SUBSCRIBE_CTA_VEO3}",
            "model": "veo3_fast",
            "duration_sec": 8,
        },
    ]
    out = pipeline._postprocess_scenes_output(scenes, "концепт")
    assert out[-1]["text"].count(pipeline.FINAL_SCENE_SUBSCRIBE_CTA_VEO3) == 1
    assert out[-1]["text"].endswith(pipeline.FINAL_SCENE_SUBSCRIBE_CTA_VEO3)


def test_generate_scenes_plan_with_previous_bypasses_cache(monkeypatch):
    scenes_payload = [
        {
            "scene": i + 1,
            "shot_type": "closeup",
            "characters": ["grandma", "cat"],
            "action": f"живое действие {i+1}",
            "text": "тЕплая реплика [пауза] про шаг",
            "duration_sec": 6,
            "model": "veo3_fast",
            "voice_needed": True,
            "has_lipsync": True,
        }
        for i in range(4)
    ]
    calls = {"get": 0, "set": 0}

    def _cached(*_args, **_kwargs):
        calls["get"] += 1
        return {"json_plan": [{"scene": 1, "text": "старый кэш"}], "regeneration_events": []}

    def _set(*_args, **_kwargs):
        calls["set"] += 1

    monkeypatch.setattr(pipeline, "get_scenes_plan", _cached)
    monkeypatch.setattr(pipeline, "set_scenes_plan", _set)
    monkeypatch.setattr(
        "app.pipeline_llm_steps.gemini_generate_sync",
        lambda _prompt: json.dumps(scenes_payload, ensure_ascii=False),
    )

    out = asyncio.run(
        pipeline.generate_scenes_plan(
            "мороженое",
            full_recipe_text="Заморозить бананы. Взбить со сливками.",
            user_feedback="Исправь сцену 2",
            previous_scenes=[{"scene": 2, "text": "предыдущая версия"}],
        )
    )

    assert calls == {"get": 0, "set": 0}
    assert out["json_plan"][0]["text"] != "старый кэш"


def test_generate_scenes_plan_caps_steps_to_six(monkeypatch):
    scenes_payload = [
        {
            "scene": i + 1,
            "location": "kitchen",
            "shot_type": "closeup",
            "characters": ["grandma", "cat"],
            "action": f"действие {i+1} с едой",
            "text": "я мешАю [пауза] аккурАтно",
            "duration_sec": 4,
            "model": "veo3_fast",
            "voice_needed": True,
            "has_lipsync": True,
        }
        for i in range(10)
    ]

    captured_prompt = {"text": ""}

    def _fake_gemini(prompt: str) -> str:
        captured_prompt["text"] = prompt
        return json.dumps(scenes_payload, ensure_ascii=False)

    monkeypatch.setattr(pipeline, "get_scenes_plan", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "set_scenes_plan", lambda *a, **k: None)
    monkeypatch.setattr("app.pipeline_llm_steps.gemini_generate_sync", _fake_gemini)

    recipe_body = "Полный рецепт: " + "; ".join(f"шаг {i}" for i in range(1, 11))
    out = asyncio.run(
        pipeline.generate_scenes_plan("сырники", full_recipe_text=recipe_body)
    )

    assert len(out["json_plan"]) == 6
    assert "duration_sec * 14" in captured_prompt["text"]
    assert "full_recipe_text" in captured_prompt["text"]
    assert "frame_framing_rule" in captured_prompt["text"]
    assert "обязательным названием блюда в `text`" in captured_prompt["text"]


def test_normalize_production_prompts_accepts_string_items():
    prompts = ["video one", "video two"]
    out = _normalize_production_prompts(prompts, scenes_count=2, require_frames=False)
    assert out[0]["video_prompt_en"] == "video one"
    assert out[1]["video_prompt_en"] == "video two"


def test_normalize_production_prompts_fills_empty_frames_when_required():
    prompts = [{"video_prompt_en": "video only"}]
    out = _normalize_production_prompts(prompts, scenes_count=1, require_frames=True)
    assert out[0]["start_frame_prompt_ru"] == "пусто"
    assert out[0]["end_frame_prompt_ru"] == "пусто"


def test_extract_production_prompts_accepts_nested_production_object():
    raw = json.dumps(
        {"production": {"prompts": [{"video_prompt_en": "nested video"}]}},
        ensure_ascii=False,
    )
    out = _extract_production_prompts(raw)
    assert out == [{"video_prompt_en": "nested video"}]


def test_extract_production_prompts_accepts_top_level_array():
    raw = json.dumps(
        [
            {"video_prompt_en": "video one"},
            {"video_prompt_en": "video two"},
        ],
        ensure_ascii=False,
    )
    out = _extract_production_prompts(raw)
    assert [item["video_prompt_en"] for item in out] == ["video one", "video two"]


def test_ensure_image_prompt_framing_requirements_adds_closeup_only():
    out = pipeline._ensure_image_prompt_framing_requirements("Бабушка месит тесто", "kitchen")
    assert "Крупный план" in out
    assert "угол ракурса" not in out.lower()
    assert "способ нарезки" not in out
    assert "подробное описание видимых ингредиентов" not in out


def test_ensure_image_prompt_framing_requirements_forces_closeup_outside_exceptions():
    src = "Общий план кухни, бабушка у плиты."
    out = pipeline._ensure_image_prompt_framing_requirements(src, "kitchen")
    assert "Крупный план" in out
    assert "угол ракурса" not in out.lower()
    assert START_FRAME_RECIPE_VISUAL_DETAIL_RULE not in out


def test_ensure_image_prompt_framing_requirements_allows_medium_plan_for_fridge_scene():
    out = pipeline._ensure_image_prompt_framing_requirements("Бабушка открывает дверцу холодильника", "fridge")
    assert "Средний план" in out
    assert "угол ракурса" not in out.lower()
    assert "Крупный план" not in out


def test_ensure_image_prompt_framing_requirements_adds_full_visibility_for_veo3():
    out = pipeline._ensure_image_prompt_framing_requirements(
        "Бабушка перемешивает тесто",
        "kitchen",
        model="veo3_fast",
    )
    assert "Бабушка и кот должны быть видны полностью в кадре." in out


def test_ensure_image_prompt_framing_requirements_skips_full_visibility_for_non_veo3():
    out = pipeline._ensure_image_prompt_framing_requirements(
        "Бабушка перемешивает тесто",
        "kitchen",
        model="kling",
    )
    assert "Бабушка и кот должны быть видны полностью в кадре." not in out


def test_ensure_scene_block_len_pads_and_truncates():
    short = [{"scene": 1}]
    out_short = _ensure_scene_block_len(short, min_n=4, max_n=6)
    assert len(out_short) == 4
    assert out_short[-1]["scene"] == 1

    long = [{"scene": i} for i in range(1, 9)]
    out_long = _ensure_scene_block_len(long, min_n=4, max_n=6)
    assert len(out_long) == 6
    assert [x["scene"] for x in out_long] == [1, 2, 3, 4, 5, 6]


def test_bind_recipe_steps_binds_by_index_only():
    scenes = [{"scene": 1}, {"scene": 2}]
    steps = ["шаг один", "шаг два", "лишний шаг"]
    out = _bind_recipe_steps(scenes, steps)
    assert out[0]["recipe_step"] == "шаг один"
    assert out[1]["recipe_step"] == "шаг два"
    assert len(out) == 2


def test_normalize_frame_prompts_validates_shape_errors():
    scenes = [{"scene": 1}]
    with pytest.raises(RuntimeError, match="count mismatch"):
        _normalize_frame_prompts(scenes, [])
    with pytest.raises(RuntimeError, match="not an object"):
        _normalize_frame_prompts(scenes, ["not-an-object"])


def test_normalize_frame_prompts_adds_scene_metadata_and_framing():
    scenes = [
        {
            "scene": 1,
            "location": "kitchen",
            "shot_type": "closeup",
            "model": "veo3_fast",
            "duration_sec": 4,
            "characters": ["grandma", "cat", "zaya"],
            "romantic_drama_beat": "Белая кошка машет рыжему коту за окном",
        }
    ]
    raw = [{"start_frame_prompt_ru": "Бабушка у стола", "end_frame_prompt_ru": ""}]
    out = _normalize_frame_prompts(scenes, raw)
    assert out[0]["scene"] == 1
    assert "location" not in out[0]
    assert out[0]["characters"] == ["grandma", "cat", "zaya"]
    assert out[0]["romantic_drama_beat"] == "Белая кошка машет рыжему коту за окном"
    assert "Крупный план" in out[0]["start_frame_prompt_ru"]
    assert "Фоновая драма этой сцены: Белая кошка машет рыжему коту за окном" in out[0]["start_frame_prompt_ru"]
    assert "Бабушка и кот должны быть видны полностью в кадре." in out[0]["start_frame_prompt_ru"]
    assert START_FRAME_RECIPE_VISUAL_DETAIL_RULE not in out[0]["start_frame_prompt_ru"]
    assert out[0]["end_frame_prompt_ru"] == "пусто"


def test_generate_frame_prompts_for_approval_builds_plan(monkeypatch):
    scene = {"scene": 1, "location": "kitchen", "shot_type": "closeup", "model": "veo3_fast", "duration_sec": 4, "romantic_drama_beat": "Белая кошка улыбается за окном"}
    captured_prompt = {"text": ""}
    model_answer = json.dumps(
        [{"start_frame_prompt_ru": "Бабушка у плиты", "end_frame_prompt_ru": "пусто"}],
        ensure_ascii=False,
    )

    def _fake_gemini(prompt: str) -> str:
        captured_prompt["text"] = prompt
        return model_answer

    monkeypatch.setattr("app.pipeline.gemini_generate_sync", _fake_gemini)
    out = asyncio.run(generate_frame_prompts_for_approval([scene], "добавь света", []))
    assert "json_plan" in out
    assert len(out["json_plan"]) == 1
    assert "способ нарезки" in captured_prompt["text"]
    assert "описание блюда" in captured_prompt["text"]
    assert "подробные описания ингредиентов" in captured_prompt["text"]
    assert "romantic_drama_beat" in captured_prompt["text"]
    assert "include it visibly and clearly" in captured_prompt["text"]
    assert "text_plan" in out
    assert '"location"' not in captured_prompt["text"]


def test_generate_full_production_with_approved_frames(monkeypatch):
    prompts_obj = {"prompts": [{"video_prompt_en": "cinematic shot with Zaya watching Timosha while Markiz waits"}]}
    captured_prompt = {"text": ""}

    def _fake_gemini(prompt: str) -> str:
        captured_prompt["text"] = prompt
        return json.dumps(prompts_obj, ensure_ascii=False)

    monkeypatch.setattr("app.pipeline.gemini_generate_sync", _fake_gemini)
    monkeypatch.setattr("app.pipeline.target_languages_from_env", lambda: ["ru", "en"])
    async def _fake_translate(text: str, target_language: str) -> str:
        return f"{target_language}:{text}"
    monkeypatch.setattr("app.pipeline._translate_scene_text", _fake_translate)
    scenes = [{"scene": 1, "location": "kitchen", "model": "veo3_fast", "text": "реплика", "romantic_drama_beat": "Zaya машет Timosha за окном"}]
    approved = [{"start_frame_prompt_ru": "Бабушка у стола", "end_frame_prompt_ru": "пусто"}]
    out = asyncio.run(
        generate_full_production(
            scenes,
            approved,
            user_feedback="чуть динамичнее",
            previous_enriched_scenes=[{"video_prompt": "old"}],
        )
    )
    scene = out["json_plan"][0]
    assert scene["model"] == "veo3_fast"
    assert scene["voice_provider"] == "none"
    assert scene["start_frame_prompt"]
    assert "Фоновая драма этой сцены: белая кошка машет рыжий кот за окном" in scene["start_frame_prompt"]
    assert "voice_lock_full:" in scene["video_prompt"]
    assert "background romantic drama beat: белая кошка машет рыжий кот за окном" in scene["video_prompt"]
    assert "Zaya" not in scene["video_prompt"]
    assert "Timosha" not in scene["video_prompt"]
    assert "Markiz" not in scene["video_prompt"]
    assert "cartoon cat" not in scene["video_prompt"]
    assert set(scene["speech_by_lang"].keys()) == {"ru", "en"}
    assert set(scene["video_prompt_by_lang"].keys()) == {"ru", "en"}
    assert scene["video_approvals_by_lang"]["ru"] is False
    assert scene["video_approvals_by_lang"]["en"] is False
    assert "no background music" in captured_prompt["text"].lower()
    assert "kitchen_physics_rule" in captured_prompt["text"]
    assert "blender" in captured_prompt["text"]
    assert "do not invent extra characters" in captured_prompt["text"].lower()
    assert "scene1_spoken_dish_name_rule" in captured_prompt["text"]
    assert "viewer must hear the dish name in the first scene" in captured_prompt["text"]
    assert "romantic_drama_rule" in captured_prompt["text"]
    assert "character_identity_rule:" not in scene["video_prompt"]
    assert "character_descriptions" not in captured_prompt["text"]
    assert "Описание персонажей" not in scene["video_prompt"]
    assert START_FRAME_RECIPE_VISUAL_DETAIL_RULE not in scene["start_frame_prompt"]
    assert "Визуальные константы предметов:" not in scene["video_prompt"]
    assert '"location"' not in captured_prompt["text"]


def test_generate_full_production_rejects_bad_approved_frames_count():
    scenes = [{"scene": 1}]
    with pytest.raises(RuntimeError, match="Approved frame prompts count mismatch"):
        asyncio.run(generate_full_production(scenes, []))


def test_generate_full_production_without_approved_frames_fills_placeholders(monkeypatch):
    prompts_obj = {
        "prompts": [
            {"start_frame_prompt_ru": "", "end_frame_prompt_ru": "", "video_prompt_en": "video prompt only"}
        ]
    }
    captured_prompt = {"text": ""}

    def _fake_gemini(prompt: str) -> str:
        captured_prompt["text"] = prompt
        return json.dumps(prompts_obj, ensure_ascii=False)

    monkeypatch.setattr("app.pipeline.gemini_generate_sync", _fake_gemini)
    monkeypatch.setattr("app.pipeline.target_languages_from_env", lambda: ["ru", "en"])
    async def _fake_translate(text: str, target_language: str) -> str:
        return f"{target_language}:{text or 'empty'}"
    monkeypatch.setattr("app.pipeline._translate_scene_text", _fake_translate)
    scenes = [{"scene": 1, "location": "kitchen", "model": "veo3_fast", "text": ""}]
    out = asyncio.run(generate_full_production(scenes, None))
    scene = out["json_plan"][0]
    assert scene["start_frame_prompt"] == "пусто"
    assert scene["end_frame_prompt"] == "пусто"
    assert scene["video_prompt"]
    assert "cartoon cat" not in scene["video_prompt"]
    assert "speech_by_lang" in scene
    assert "video_prompt_by_lang" in scene
    assert "подробные описания ингредиентов" in captured_prompt["text"]
    assert "kitchen_physics_rule" in captured_prompt["text"]
    assert "do not invent extra characters" in captured_prompt["text"].lower()
    assert '"location"' not in captured_prompt["text"]


def test_generate_script_composes_full_text_and_scenes(monkeypatch):
    async def _fake_full_text(text: str, dish_name: str | None = None, **_kwargs):
        assert text == "сырники"
        assert dish_name == "сырники"
        return "полный рецепт"

    async def _fake_scenes(concept: str, *, full_recipe_text: str, **_kwargs):
        assert concept == "сырники"
        assert full_recipe_text == "полный рецепт"
        return {"json_plan": [{"scene": 1}]}

    monkeypatch.setattr("app.pipeline.generate_recipe_full_text", _fake_full_text)
    monkeypatch.setattr("app.pipeline.generate_scenes_plan", _fake_scenes)
    out = asyncio.run(generate_script("сырники"))
    assert out["json_plan"][0]["scene"] == 1
