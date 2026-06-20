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
    format_romantic_drama_status,
    format_viral_concept_brief,
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
    scenes = [{"scene": 1, "text": "привет", "characters": ["grandma", "cat", "zaya"], "action": "Zaya смотрит на Timosha", "romantic_drama_beat": "Zaya машет Timosha за окном"}]
    out = build_human_readable_plan(scenes)
    assert "СЦЕНА 1 [neutral]" in out
    assert "Нужна ли озвучка: да" in out
    assert "Вид/ракурс: medium" in out
    assert "Персонажи: бабушка, рыжий кот, белая кошка" in out
    assert "Драма в сцене: белая кошка машет рыжий кот за окном" in out
    assert "Zaya" not in out
    assert "Timosha" not in out
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
    assert "СЦЕНА 3 [neutral]" in out
    assert "Нейросеть: veo3_fast" in out
    assert "Lip sync: да" in out


def test_build_human_readable_plan_shows_scene_audience_type():
    scenes = [{"scene": 2, "dish_audience_type": "child", "text": "корн-доги готовы"}]
    out = build_human_readable_plan(scenes)
    assert "СЦЕНА 2 [child]" in out


def test_build_human_readable_plan_shows_dialogue_for_multi_speaker_scene():
    scenes = [
        {
            "scene": 1,
            "dish_audience_type": "male",
            "dialogue_mode": "multi",
            "dialogue": [
                {"speaker": "grandfather", "text": "Ммм, вот это я понимаю, настоящий ужин!"},
                {"speaker": "grandma", "text": "Одна сковорода и вкуснейшее жаркое на всю неделю готово."},
            ],
            "text": "Ммм [пауза] Одна сковорода",
        }
    ]
    out = build_human_readable_plan(scenes)
    assert "Диалог сцены (будет в озвучке):" in out
    assert "дедушка: Ммм, вот это я понимаю, настоящий ужин!" in out
    assert "бабушка: Одна сковорода и вкуснейшее жаркое на всю неделю готово." in out
    assert "Агрегированная фраза сцены:" in out


def test_format_viral_concept_brief_shows_audience_type_and_family_plan():
    out = format_viral_concept_brief(
        {
            "dish_audience_type": "male",
            "family_cast_plan": "Добавить дедушку в первую и последнюю сцену.",
            "main_hook": "хук",
            "hooks": {},
            "analysis": {},
        }
    )
    assert "Тип блюда для каста: male" in out
    assert "План семейных персонажей: Добавить дедушку в первую и последнюю сцену." in out


def test_format_romantic_drama_status_explicitly_reports_decision():
    off = format_romantic_drama_status({"enabled": False, "characters": [], "brief": ""})
    assert "Драма с персонажем: белая кошка - нет" in off
    assert "Дополнительный соседский кот: черно-белый кот - нет" in off

    on = format_romantic_drama_status(
        {
            "enabled": True,
            "characters": ["zaya", "markiz"],
            "brief": "Маркиз ревнует у миски.",
            "zaya_timosha_arc": "Тимоша волнуется, Зая выбирает стоять рядом с ним.",
            "markiz_arc": "Маркиз пытается понравиться Зае весь ролик.",
            "happy_end": "Зая и Тимоша улыбаются друг другу у окна, Маркиз спокойно получает кусочек.",
            "episode_rule": "whole episode",
        }
    )
    assert "Решение уровня ролика" in on
    assert "Драма с персонажем: белая кошка - да" in on
    assert "Дополнительный соседский кот: черно-белый кот - да" in on
    assert "черно-белый кот ревнует у миски" in on
    assert "рыжий кот + белая кошка" in on
    assert "Хэппи-энд: белая кошка и рыжий кот улыбаются друг другу у окна" in on
    assert "понятным хэппи-эндом" in on
    assert "Тимош" not in on
    assert "Зая" not in on
    assert "Маркиз" not in on


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
