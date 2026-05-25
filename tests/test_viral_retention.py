from app.viral_retention import (
    enrich_scenes_with_retention,
    flatten_hooks,
    normalize_hooks,
    normalize_viral_concept_brief,
)


def test_normalize_viral_concept_brief_fills_required_hooks_and_scores() -> None:
    brief = normalize_viral_concept_brief(
        {
            "main_hook": "В конце покажу разрез",
            "first_frame_type": "cake_cut",
            "payoff_type": "cut",
            "hooks": {"emotional": ["домашнее тепло"]},
            "analysis": {"hook_strength": 99, "risk_of_boring_scene": -5},
        },
        concept="медовик",
    )

    hooks = brief["hooks"]
    assert len(hooks["emotional"]) >= 3
    assert len(hooks["intrigue"]) >= 3
    assert len(hooks["useful"]) >= 2
    assert len(hooks["cliffhanger"]) >= 2
    assert len(flatten_hooks(hooks)) >= 10
    assert brief["analysis"]["hook_strength"] == 10
    assert brief["analysis"]["risk_of_boring_scene"] == 1


def test_normalize_hooks_fallback_has_minimum_ten() -> None:
    hooks = normalize_hooks({}, "сырники")
    assert len(flatten_hooks(hooks)) >= 10


def test_enrich_scenes_with_retention_marks_hook_and_payoff() -> None:
    scenes = [
        {"scene": 1, "action": "Бабушка показывает готовый торт, кот нюхает запах", "text": "смотрите"},
        {"scene": 2, "action": "Бабушка смешивает крем, кот смотрит", "text": "секрет крема"},
        {"scene": 3, "action": "Бабушка делает финальный разрез и улыбается", "text": "сохрани"},
    ]
    brief = normalize_viral_concept_brief({}, concept="торт")

    enriched, review = enrich_scenes_with_retention(scenes, viral_concept_brief=brief)

    assert enriched[0]["retention_function"] == "hook"
    assert enriched[-1]["retention_function"] == "payoff"
    assert enriched[-1]["payoff_type"] == brief["payoff_type"]
    assert review["overall_retention_score"] >= 1
