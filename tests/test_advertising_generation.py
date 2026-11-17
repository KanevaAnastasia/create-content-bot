import json

import pytest

from app import advertising_generation
from app.handlers.handlers_media_scene import _append_scene_identity_to_image_prompt
from app.handlers.handlers_state_media import (
    _ensure_advertising_product_video_lock,
    _fallback_social_copy,
    _social_copy_prompt,
)


PRODUCT = {
    "product_name": "Пена для обуви CleanStep",
    "short_product_lock": "белый флакон с синей крышкой и круглой синей этикеткой",
}


def test_advertising_campaigns_returns_five_selectable_variants(monkeypatch):
    monkeypatch.setattr(
        advertising_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "campaigns": [
                    {
                        "title": f"Идея {index}",
                        "hook": f"Хук {index}",
                        "idea": f"Отдельный сюжет {index}",
                        "product_role": "Товар крупно в центре",
                        "cta": "Попробуйте сами",
                    }
                    for index in range(1, 6)
                ]
            },
            ensure_ascii=False,
        ),
    )

    result = advertising_generation.generate_advertising_campaigns(
        source_text="Как почистить белые кроссовки",
        product_brief=PRODUCT,
    )

    assert len(result["campaigns"]) == 5
    assert [item["number"] for item in result["campaigns"]] == [1, 2, 3, 4, 5]
    assert "5 ВАРИАНТОВ РЕКЛАМНОЙ КАМПАНИИ" in result["text"]
    assert "5. Идея 5" in result["text"]


def test_advertising_post_package_has_one_large_product_image_and_clean_copy(monkeypatch):
    monkeypatch.setattr(
        advertising_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "post_text": "Бабушка знает простой секрет. Попробуйте и вы!\n\n#реклама #товар",
                "image_prompts": ["Хук", "Демонстрация", "Результат"],
            },
            ensure_ascii=False,
        ),
    )

    result = advertising_generation.generate_advertising_post_package(
        source_text="Как почистить белые кроссовки",
        product_brief=PRODUCT,
        campaign={"title": "Чистая победа", "idea": "Кот испачкал кроссовки"},
    )

    assert len(result["image_prompts"]) == 1
    assert all("35–50%" in prompt for prompt in result["image_prompts"])
    assert all("ОБЯЗАТЕЛЬНЫЙ ТОВАР В КАДРЕ" in prompt for prompt in result["image_prompts"])
    assert all("бабушка и рыжий кот" in prompt.lower() for prompt in result["image_prompts"])
    assert "#" not in result["post_text"]
    assert "реклама" not in result["post_text"].lower()


def test_advertising_post_text_feedback_is_applied_and_hashtags_are_removed(monkeypatch):
    captured: list[str] = []

    def fake_generate(prompt, **_kwargs):
        captured.append(prompt)
        return "Короткий новый текст\n\n#реклама #товар"

    monkeypatch.setattr(advertising_generation, "gemini_generate_sync", fake_generate)

    result = advertising_generation.regenerate_advertising_post_text(
        source_text="Как почистить белые кроссовки",
        product_brief=PRODUCT,
        campaign={"title": "Чистая победа"},
        current_text="Старый длинный текст",
        feedback="Сделай в два раза короче",
    )

    assert result == "Короткий новый текст"
    assert "Сделай в два раза короче" in captured[0]
    assert "Фидбэк пользователя обязателен" in captured[0]


def test_advertising_post_image_feedback_cannot_remove_product_lock():
    result = advertising_generation.ensure_advertising_post_image_prompt(
        "Новый светлый фон",
        product_brief=PRODUCT,
    )

    assert "Новый светлый фон" in result
    assert "35–50%" in result
    assert "ОБЯЗАТЕЛЬНЫЙ ТОВАР В КАДРЕ" in result
    assert "Вертикаль 4:5" in result


@pytest.mark.parametrize("source_count, expected_count", [(2, 3), (3, 3), (4, 4), (5, 5), (6, 6), (7, 6)])
def test_advertising_scene_plan_for_how_to_has_veo_scenes_and_product_everywhere(monkeypatch, source_count, expected_count):
    monkeypatch.setattr(
        advertising_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "Белые кроссовки снова чистые",
                "content_type": "how_to",
                "scenes": [
                    {
                        "speech_ru": f"Шаг {index}",
                        "action": f"Бабушка очищает кроссовки, этап {index}",
                        "visual_description": "Флакон стоит рядом с белыми кроссовками",
                        "shot_type": "close-up",
                        "characters": ["grandma", "cat"],
                        "product_use": "Флакон целиком виден справа от кроссовок",
                    }
                    for index in range(1, source_count + 1)
                ],
            },
            ensure_ascii=False,
        ),
    )

    result = advertising_generation.generate_advertising_scene_plan(
        source_text="Как почистить белые кроссовки",
        product_brief=PRODUCT,
    )

    scenes = result["json_plan"]
    assert result["content_type"] == "how_to"
    assert len(scenes) == expected_count
    assert scenes[-1]["retention_function"] == "payoff"
    assert all(scene["model"] == "veo3_fast" for scene in scenes)
    assert all(scene["duration_sec"] == 8 for scene in scenes)
    assert all(scene["advertising_product_required"] is True for scene in scenes)
    assert all("ОБЯЗАТЕЛЬНЫЙ ТОВАР В КАДРЕ" in scene["action"] for scene in scenes)


def test_advertising_production_keeps_product_description_only_in_start_prompt(monkeypatch):
    monkeypatch.setattr(
        advertising_generation,
        "gemini_generate_sync",
        lambda *_args, **_kwargs: json.dumps(
            {
                "prompts": [
                    {
                        "start_frame_prompt_ru": "Бабушка держит флакон рядом с кроссовками.",
                        "speech_by_lang": {"ru": "Покажу первый шаг.", "en": "Here is the first step."},
                        "video_prompt_by_lang": {"ru": "8-second Veo shot.", "en": "8-second Veo shot."},
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    result = advertising_generation.generate_advertising_production(
        [
            {
                "scene": 1,
                "text": "Покажу первый шаг.",
                "action": "Бабушка показывает товар.",
                "display_action": "Бабушка показывает товар.",
                "characters": ["grandma", "cat"],
            }
        ],
        product_brief=PRODUCT,
        target_languages=["ru", "en"],
    )
    scene = result["json_plan"][0]
    assert "ОБЯЗАТЕЛЬНЫЙ ТОВАР В КАДРЕ" in scene["start_frame_prompt"]
    assert all("approved start frame" in prompt for prompt in scene["video_prompt_by_lang"].values())
    assert all("MANDATORY PRODUCT CONTINUITY" not in prompt for prompt in scene["video_prompt_by_lang"].values())
    assert all(PRODUCT["short_product_lock"] not in prompt for prompt in scene["video_prompt_by_lang"].values())
    assert scene["video_prompt_by_lang"].keys() == {"ru"}
    assert scene["speech_by_lang"].keys() == {"ru"}
    assert scene["video_approvals_by_lang"] == {"ru": False}


def test_advertising_product_reference_survives_as_file_backed_session_value(tmp_path):
    path = tmp_path / "product.jpg"
    path.write_bytes(b"product-reference")
    session = {advertising_generation.ADVERTISING_PRODUCT_PATH_SESSION_KEY: str(path)}
    assert advertising_generation.advertising_product_reference_bytes(session) == [b"product-reference"]


def test_advertising_lock_is_reapplied_to_image_prompt_after_edits():
    lock = advertising_generation.advertising_product_lock_ru(PRODUCT)
    prompt = _append_scene_identity_to_image_prompt(
        "Бабушка начинает чистить белые кроссовки.",
        {
            "generation_mode": "advertising",
            "model": "veo3_fast",
            "characters": ["grandma", "cat"],
            "advertising_product_lock_ru": lock,
        },
    )
    assert lock in prompt
    assert "не создавать вторую упаковку" in prompt


def test_advertising_video_prompt_replaces_legacy_product_description_with_start_frame_reference():
    product_lock = advertising_generation.advertising_product_lock_en(PRODUCT)
    prompt = _ensure_advertising_product_video_lock(
        {
            "generation_mode": "advertising",
            "advertising_product_lock_en": product_lock,
        },
        f"Slow camera move. {product_lock}",
    )
    assert "approved start frame" in prompt
    assert "MANDATORY PRODUCT CONTINUITY" not in prompt
    assert PRODUCT["product_name"] not in prompt


def test_advertising_social_copy_is_not_forced_into_recipe_language():
    prompt = _social_copy_prompt(
        "Как почистить белые кроссовки",
        advertising=True,
        product_context="Пена CleanStep",
    )
    assert "Не превращай бытовую инструкцию или историю в рецепт" in prompt
    assert "Пена CleanStep" in prompt
    fallback = _fallback_social_copy("Как почистить белые кроссовки", advertising=True)
    assert "#реклама" in fallback["instagram_ru"]
    assert "#ad" in fallback["instagram_en"]


def test_advertising_editorial_rules_bind_comment_first_person_and_canonical_names(monkeypatch):
    captured = []
    def generate(prompt, **kwargs):
        captured.append(prompt)
        return json.dumps({'campaigns': [{'title': 'Оливье', 'idea': 'Я нарезаю оливье'}] * 5})
    monkeypatch.setattr(advertising_generation, 'gemini_generate_sync', generate)
    comment = 'Сначала оливье с нарезкой, затем смузи, тесто и крем; отличный подарок.'
    advertising_generation.generate_advertising_campaigns(source_text='Pioneer', product_brief={'owner_comment': comment})
    assert comment in captured[0]
    assert 'основа КАЖДОГО концепта' in captured[0]
    assert 'от её первого лица' in captured[0]
    assert 'Тимоша' in captured[0]
    assert 'Мурзик' not in captured[0]
