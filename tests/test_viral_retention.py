from app.viral_retention import (
    enrich_scenes_with_retention,
    flatten_hooks,
    normalize_hooks,
    normalize_viral_concept_brief,
    review_first_phrase,
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
    assert brief["dish_audience_type"] == "neutral"
    assert brief["family_cast_plan"]
    assert brief["first_scene_dialogue_hint"] is False


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


def test_review_first_phrase_penalizes_generic_intro() -> None:
    review = review_first_phrase(
        "Сегодня мы готовим вкусное блюдо",
        scene={"action": "Бабушка нарезает овощи"},
    )

    assert review["score"] < 7
    assert review["issues"]


def test_review_first_phrase_rejects_empty_watch_to_end_bait() -> None:
    review = review_first_phrase(
        "Досмотрите до разреза медовика он слишком нежный",
        scene={"action": "Бабушка показывает готовый медовик и красивый разрез"},
    )

    assert review["score"] < 8
    assert review["issues"]


def test_review_first_phrase_accepts_non_secret_payoff_hook() -> None:
    review = review_first_phrase(
        "Не теряйте медовик, сейчас покажу красивый разрез",
        scene={"action": "Бабушка показывает готовый медовик и красивый разрез"},
    )

    assert review["score"] >= 8
    assert not review["issues"]


def test_enrich_scenes_adds_first_phrase_review_to_first_scene() -> None:
    scenes = [
        {"scene": 1, "action": "Бабушка показывает готовый торт и разрез", "text": "Сегодня мы готовим вкусное блюдо"},
        {"scene": 2, "action": "Бабушка смешивает крем", "text": "секрет крема"},
        {"scene": 3, "action": "Бабушка делает финальный разрез", "text": "сохрани"},
    ]
    brief = normalize_viral_concept_brief({}, concept="торт")

    enriched, review = enrich_scenes_with_retention(scenes, viral_concept_brief=brief)

    assert enriched[0]["first_phrase_score"] < 7
    assert enriched[0]["first_phrase_suggestion"]
    assert review["first_scene_review"]["issues"]


def test_normalize_viral_concept_brief_preserves_family_cast_fields() -> None:
    brief = normalize_viral_concept_brief(
        {
            "dish_audience_type": "child",
            "family_cast_plan": "Добавить внука в первую и последнюю сцену.",
            "first_scene_dialogue_hint": True,
        },
        concept="корн-доги",
    )

    assert brief["dish_audience_type"] == "child"
    assert "внука" in brief["family_cast_plan"].lower()
    assert brief["first_scene_dialogue_hint"] is True
