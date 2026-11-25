from app.generation_modes import (
    GENERATION_MODE_ADVERTISING,
    GENERATION_MODE_COPY,
    GENERATION_MODE_LONG,
    GENERATION_MODE_NORMAL,
    GENERATION_MODE_REFERENCE,
    GENERATION_MODE_SESSION_KEY,
    GENERATION_MODE_SILENCE,
    GENERATION_MODE_STORY,
    GENERATION_MODE_VISUALIZATION,
    SYNTAX_PROVIDER_KLING,
    SYNTAX_PROVIDER_VEO,
    aspect_ratio_for_generation_mode,
    is_copy_generation,
    is_long_generation,
    is_reference_generation,
    is_story_generation,
    is_visualization_generation,
    is_advertising_generation,
    normalize_generation_mode,
    syntax_provider_for_scene_generation,
    target_languages_for_generation_mode,
)


def test_long_generation_mode_helpers():
    assert normalize_generation_mode("длинный") == GENERATION_MODE_LONG
    assert normalize_generation_mode("long") == GENERATION_MODE_LONG
    assert normalize_generation_mode("копия") == GENERATION_MODE_COPY
    assert normalize_generation_mode("copy") == GENERATION_MODE_COPY
    assert normalize_generation_mode("обычный") == GENERATION_MODE_NORMAL
    assert normalize_generation_mode("silent") == GENERATION_MODE_SILENCE
    assert normalize_generation_mode("история") == GENERATION_MODE_STORY
    assert normalize_generation_mode("визуализация") == GENERATION_MODE_VISUALIZATION
    assert normalize_generation_mode("реклама") == GENERATION_MODE_ADVERTISING
    assert normalize_generation_mode("По референсу") == GENERATION_MODE_REFERENCE

    session = {GENERATION_MODE_SESSION_KEY: GENERATION_MODE_LONG}
    assert is_long_generation(session) is True
    assert target_languages_for_generation_mode(session, ["ru", "en"]) == ["ru"]
    assert aspect_ratio_for_generation_mode(session) == "16:9"
    assert aspect_ratio_for_generation_mode(GENERATION_MODE_NORMAL) == "9:16"

    copy_session = {GENERATION_MODE_SESSION_KEY: GENERATION_MODE_COPY}
    assert is_copy_generation(copy_session) is True
    assert target_languages_for_generation_mode(copy_session, ["ru", "en"]) == ["ru"]

    story_session = {GENERATION_MODE_SESSION_KEY: GENERATION_MODE_STORY}
    assert is_story_generation(story_session) is True
    assert target_languages_for_generation_mode(story_session, ["ru", "en"]) == ["ru"]
    assert syntax_provider_for_scene_generation({}, story_session) == SYNTAX_PROVIDER_KLING

    visualization_session = {GENERATION_MODE_SESSION_KEY: GENERATION_MODE_VISUALIZATION}
    assert is_visualization_generation(visualization_session) is True
    assert is_story_generation(visualization_session) is True
    assert target_languages_for_generation_mode(visualization_session, ["ru", "en"]) == ["ru"]
    assert syntax_provider_for_scene_generation({}, visualization_session) == SYNTAX_PROVIDER_KLING

    advertising_session = {GENERATION_MODE_SESSION_KEY: GENERATION_MODE_ADVERTISING}
    assert is_advertising_generation(advertising_session) is True
    assert target_languages_for_generation_mode(advertising_session, ["ru", "en"]) == ["ru"]
    assert syntax_provider_for_scene_generation({}, advertising_session) == SYNTAX_PROVIDER_VEO

    reference_session = {GENERATION_MODE_SESSION_KEY: GENERATION_MODE_REFERENCE}
    assert is_reference_generation(reference_session) is True
    assert target_languages_for_generation_mode(reference_session, ["ru", "en"]) == ["ru"]
    assert aspect_ratio_for_generation_mode(reference_session) == "9:16"
    assert syntax_provider_for_scene_generation({}, reference_session) == SYNTAX_PROVIDER_VEO


def test_scene_level_syntax_provider_overrides_mode_default():
    assert syntax_provider_for_scene_generation({"model": "kling_3_0"}, GENERATION_MODE_LONG) == SYNTAX_PROVIDER_KLING
    assert syntax_provider_for_scene_generation({"model": "veo3_fast"}, GENERATION_MODE_SILENCE) == SYNTAX_PROVIDER_VEO
    assert syntax_provider_for_scene_generation({}, GENERATION_MODE_SILENCE) == SYNTAX_PROVIDER_KLING
