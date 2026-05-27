from app.formatting import (
    build_human_readable_plan,
    cost_label,
    format_concepts,
    format_frame_prompts_for_approval,
    format_full_recipe_for_approval,
    format_json_for_telegram,
    format_production_bundle_for_approval,
    format_production_cards,
    format_recipe_step_scene_draft,
    format_recipe_steps,
    format_viral_application_summary,
)


def test_format_json_for_telegram_escapes_html():
    out = format_json_for_telegram({"x": "<tag>"})
    assert out.startswith("<pre>")
    assert "&lt;tag&gt;" in out


def test_cost_label_known_and_unknown():
    assert cost_label("kling") == "~0.005-0.01$"
    assert cost_label("unknown") == "~n/a"


def test_build_human_readable_plan_includes_defaults():
    scenes = [{"scene": 1, "text": "привет"}]
    out = build_human_readable_plan(scenes)
    assert "СЦЕНА 1" in out
    assert "Нужна ли озвучка: да" in out
    assert "Вид/ракурс: medium" in out
    assert "Примерная стоимость" in out


def test_build_human_readable_plan_supports_raw_scene_aliases():
    scenes = [
        {
            "scene_id": 3,
            "location": "kitchen",
            "lip_sync": True,
            "voice_needed": True,
            "text": "делаем шаг",
            "duration_sec": 6,
        }
    ]
    out = build_human_readable_plan(scenes)
    assert "СЦЕНА 3" in out
    assert "Нейросеть: veo3_fast" in out
    assert "Lip sync: да" in out


def test_format_viral_application_summary_explains_usage():
    out = format_viral_application_summary(
        {
            "strongest_first_frame": "крупный разрез торта",
            "reason_to_watch_end": "досмотреть до красивого разреза",
            "payoff_type": "cut",
        },
        [
            {
                "scene": 1,
                "text": "Досмотрите до разреза медовика он нежный",
                "action": "Бабушка показывает готовый медовик и разрез",
                "retention_function": "hook",
                "first_phrase_score": 9,
                "first_phrase_review": {"issues": []},
            },
            {
                "scene": 2,
                "action": "Бабушка смешивает крем",
                "retention_function": "recipe_progress",
            },
            {
                "scene": 3,
                "action": "Бабушка делает финальный разрез",
                "retention_function": "payoff",
                "payoff_type": "cut",
            },
        ],
        {"overall_retention_score": 8},
    )

    assert "КАК ПРИМЕНЕНО" in out
    assert "уже встроены в сценарий" in out
    assert "Первая фраза в сценарии" in out
    assert "1:hook" in out
    assert "3:payoff" in out


def test_concepts_recipe_and_production_formatting():
    concepts = [{"name": "Сырники", "why": "быстро", "hook": "кот мешает"}]
    assert "КОНЦЕПТ 1" in format_concepts(concepts)

    assert "ШАГ 1: a" in format_recipe_steps(["a", "b"])
    assert "РЕЦЕПТ:" in format_full_recipe_for_approval("Смешать творог с яйцом.")
    assert "—" in format_full_recipe_for_approval("   ")

    cards = format_production_cards([{"scene": 1, "model": "veo3", "text": "x"}])
    assert "PRODUCTION CARDS" in cards
    assert "СЦЕНА 1" in cards


def test_format_frame_prompts_numbers_scenes_by_list_order():
    """Заголовки СЦЕНА 1..N по позиции в массиве, даже если поле scene отсутствует или другое."""
    frames = [
        {
            "location": "kitchen",
            "shot_type": "medium",
            "start_frame_prompt_ru": "A",
            "end_frame_prompt_ru": "пусто",
        },
        {
            "scene": 99,
            "location": "stove",
            "shot_type": "closeup",
            "start_frame_prompt_ru": "B",
            "end_frame_prompt_ru": "пусто",
        },
    ]
    out = format_frame_prompts_for_approval(frames)
    assert "FRAME PROMPTS" in out
    assert "СЦЕНА 1" in out
    assert "СЦЕНА 2" in out
    assert "СЦЕНА 99" not in out


def test_format_production_bundle_contains_scene_and_prompts():
    scenes = [
        {
            "scene": 1,
            "action": "Месит тесто",
            "text": "смотрИте",
            "start_frame_prompt": "Старт",
            "end_frame_prompt": "пусто",
            "video_prompt": "fixed camera",
        }
    ]
    out = format_production_bundle_for_approval(scenes)
    assert "ПРОМПТЫ ПО ВСЕМ СЦЕНАМ" in out
    assert "START FRAME" in out
    assert "VIDEO PROMPT" in out


def test_recipe_scene_draft_formatting():
    out = format_recipe_step_scene_draft(
        ["тщательно смешать творог с яйцом и сахаром", "сформировать сырники"]
    )
    assert "ПРЕДВАРИТЕЛЬНЫЙ ЧЕРНОВИК СЦЕН" in out
    assert "СЦЕНА 1" in out
    assert "Модель: veo3_fast" in out
    assert "Длительность:" in out
    assert "Фраза (черновик):" not in out
    assert "Что происходит:" not in out
