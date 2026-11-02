from app.copy_mode_prompts import (
    COPY_MODE_PROMPT_RULES,
    build_copy_mode_seedance_prompt,
    format_copy_mode_seconds_range,
)
from app.handlers import handlers_state_scene
import asyncio


def test_family_cast_keeps_family_hands_and_attaches_project_references(monkeypatch, tmp_path):
    roles = ["granddaughter", "grandson", "grandfather", "daughter"]
    timeline = [{"start_sec": 0, "end_sec": 3, "action": "Family cooks together",
                 "character_roles": roles, "replace_with_grandma": "true"}]
    prompt = build_copy_mode_seedance_prompt(timeline)
    assert "Use matching project characters and their hands" in prompt
    assert "Replace visible hands or people with the grandmother." not in prompt
    assets = {}
    for role in ["grandma", "cat", *roles]:
        path = tmp_path / f"{role}.png"
        path.touch()
        assets[role] = {"base": str(path)}
    monkeypatch.setattr(handlers_state_scene, "ASSETS", {"characters": assets})
    paths, required = handlers_state_scene._copy_mode_reference_bundle(timeline)
    assert paths == [assets[role]["base"] for role in ["grandma", "cat", *roles]]
    assert required == list(range(6))


def test_format_copy_mode_seconds_range_uses_decimal_seconds():
    assert format_copy_mode_seconds_range(1, 2.3) == "1.00-2.30 sec"
    assert format_copy_mode_seconds_range(0, 1) == "0.00-1.00 sec"


def test_copy_mode_prompt_rules_exclude_ui_params():
    rules = COPY_MODE_PROMPT_RULES.lower()

    assert "do not include model" in rules
    assert "aspect ratio" in rules
    assert "resolution" in rules
    assert "duration" in rules
    assert "do not shorten" in rules
    assert "`0.00-1.00 sec`" in COPY_MODE_PROMPT_RULES
    assert "`1.00-2.30 sec`" in COPY_MODE_PROMPT_RULES
    assert "weather" in rules


def test_build_copy_mode_seedance_prompt_uses_second_by_second_timing_without_ui_params():
    prompt = build_copy_mode_seedance_prompt(
        [
            {
                "time_range": "00:10-00:11",
                "action": "Drizzle olive oil over tomatoes",
                "visible_ingredients": ["olive oil", "tomatoes", "garlic"],
                "cookware_props": ["white rectangular baking dish"],
                "camera": "close-up",
                "replace_with_grandma": "true",
                "cat_placement_hint": "Orange cat watches from the counter.",
            },
            {
                "time_range": "00:11-00:12.5",
                "action": "Grind pepper over the vegetables",
                "visible_ingredients": ["pepper", "tomatoes"],
                "cookware_props": ["pepper grinder"],
                "camera": "slight pan right",
                "replace_with_grandma": "true",
                "cat_placement_hint": "Orange cat stays visible beside the dish.",
            },
        ],
        chunk_start_sec=10,
    )

    assert "0.00-1.00 sec:" in prompt
    assert "1.00-2.50 sec:" in prompt
    assert "Replace visible hands or people with the grandmother." in prompt
    assert "Orange cat must be visible in every shot" in prompt
    assert "Seedance 2.0" not in prompt
    assert "9:16" not in prompt
    assert "480p" not in prompt
    assert "10 seconds" not in prompt
    assert "original source video is not attached" in prompt


def test_build_copy_mode_seedance_prompt_marks_continuation():
    prompt = build_copy_mode_seedance_prompt(
        [{"second": 0, "action": "Show soup pot", "cat_placement_hint": "Orange cat sits nearby."}],
        continuation=True,
    )

    assert "continues the previously generated video" in prompt


def test_build_copy_mode_seedance_prompt_preserves_weather_and_user_feedback():
    prompt = build_copy_mode_seedance_prompt(
        [
            {
                "second": 0,
                "action": "Grandmother sets tea beside the window",
                "visible_ingredients": ["tea"],
                "cookware_props": ["floral teacup", "rainy window"],
                "camera": "static medium shot",
                "weather_atmosphere": "heavy rain outside, warm candlelight inside",
                "replace_with_grandma": "true",
                "cat_placement_hint": "Orange cat sits on the windowsill.",
            }
        ],
        weather_atmosphere="heavy rain, gloomy autumn evening",
        user_feedback="make the kitchen darker and keep steam visible",
    )

    assert "Preserve weather and atmosphere: heavy rain, gloomy autumn evening." in prompt
    assert "User corrections to apply in every timing range: make the kitchen darker and keep steam visible." in prompt
    assert "Weather/atmosphere: heavy rain outside, warm candlelight inside." in prompt


def test_build_copy_mode_seedance_prompt_collapses_consecutive_removed_logo_moments():
    prompt = build_copy_mode_seedance_prompt(
        [
            {
                "second": 0,
                "action": "Grandmother pours tea",
                "visible_ingredients": ["tea", "apple cake"],
                "cookware_props": ["floral teacup", "wooden table"],
                "camera": "static medium shot",
                "replace_with_grandma": "true",
                "cat_placement_hint": "Orange cat sits beside the teacup.",
            },
            {
                "second": 1,
                "action": "Instagram logo end screen",
                "visible_ingredients": ["tea", "apple cake"],
                "cookware_props": ["floral teacup", "wooden table"],
                "text_or_watermark_to_remove": ["Instagram logo"],
            },
            {
                "second": 2,
                "action": "Black logo screen with username",
                "visible_ingredients": ["tea", "apple cake"],
                "cookware_props": ["floral teacup", "wooden table"],
                "text_or_watermark_to_remove": ["@username"],
            },
            {
                "second": 3,
                "action": "Grandmother lifts the teacup",
                "visible_ingredients": ["tea"],
                "cookware_props": ["floral teacup"],
                "camera": "small tilt up",
                "replace_with_grandma": "true",
                "cat_placement_hint": "Orange cat looks up from the table.",
            },
        ],
    )

    assert "1.00-3.00 sec:" in prompt
    assert "1.00-2.00 sec:" not in prompt
    assert "2.00-3.00 sec:" not in prompt
    assert prompt.count("Clean continuation of the previous cooking visual") == 1
    assert "Instagram logo end screen" not in prompt
    assert "Black logo screen with username" not in prompt


def test_build_copy_mode_seedance_prompt_keeps_action_when_only_overlay_is_marked_for_removal():
    prompt = build_copy_mode_seedance_prompt(
        [
            {
                "second": 3,
                "action": "Grandmother slices apples beside a teacup",
                "visible_ingredients": ["apples", "tea"],
                "cookware_props": ["knife", "cutting board", "floral teacup"],
                "camera": "static close-up",
                "replace_with_grandma": "true",
                "cat_placement_hint": "Orange cat watches from the table edge.",
                "text_or_watermark_to_remove": ["Instagram username watermark"],
            }
        ],
        chunk_start_sec=3,
    )

    assert "0.00-1.00 sec: Grandmother slices apples beside a teacup" in prompt
    assert "Clean continuation of the previous cooking visual" not in prompt
    assert "Instagram username watermark" not in prompt


def test_copy_mode_short_source_video_uses_one_chunk():
    chunks = asyncio.run(
        handlers_state_scene._build_copy_mode_chunks(
            None,
            None,
            chat_id=123,
            analysis={
                "source_duration_sec_estimate": 13.3,
                "weather_atmosphere": "rainy autumn day",
                "timeline": [
                    {
                        "second": i,
                        "action": "Instagram logo end screen" if i == 10 else f"Cooking action {i}",
                        "visible_ingredients": [] if i == 10 else ["potato"],
                        "cookware_props": [] if i == 10 else ["knife"],
                        "text_or_watermark_to_remove": ["Instagram logo"] if i == 10 else [],
                    }
                    for i in range(14)
                ],
            },
            weather_atmosphere="rainy autumn day",
            user_feedback="make all shots cozier",
        )
    )

    assert len(chunks) == 1
    assert chunks[0]["source_start_sec"] == 0
    assert chunks[0]["source_end_sec"] == 13.3
    assert chunks[0]["duration_sec"] == 14
    assert any(entry.get("second") == 10 for entry in chunks[0]["timeline"])
    assert "Instagram logo end screen" not in chunks[0]["prompt"]
    assert "Clean continuation of the previous cooking visual" in chunks[0]["prompt"]
    assert "Never show Instagram marks" in chunks[0]["prompt"]
    assert chunks[0]["weather_atmosphere"] == "rainy autumn day"
    assert chunks[0]["user_feedback"] == "make all shots cozier"
    assert "Preserve weather and atmosphere: rainy autumn day." in chunks[0]["prompt"]
    assert "User corrections to apply in every timing range: make all shots cozier." in chunks[0]["prompt"]


def test_copy_mode_variable_ranges_stay_variable_and_are_clipped_at_chunk_boundaries():
    chunks = asyncio.run(
        handlers_state_scene._build_copy_mode_chunks(
            None,
            None,
            chat_id=123,
            analysis={
                "source_duration_sec_estimate": 18.4,
                "timeline": [
                    {"start_sec": 0.0, "end_sec": 2.3, "action": "Wash apples"},
                    {"start_sec": 2.3, "end_sec": 11.75, "action": "Slice apples"},
                    {"start_sec": 11.75, "end_sec": 18.4, "action": "Arrange slices"},
                ],
            },
        )
    )

    assert len(chunks) == 2
    assert "0.00-2.30 sec: Wash apples" in chunks[0]["prompt"]
    assert "2.30-10.00 sec: Slice apples" in chunks[0]["prompt"]
    assert "0.00-1.75 sec: Slice apples" in chunks[1]["prompt"]
    assert "1.75-8.40 sec: Arrange slices" in chunks[1]["prompt"]


def test_copy_mode_analysis_prompt_requests_meaningful_decimal_ranges():
    prompt = handlers_state_scene._copy_mode_analysis_prompt()

    assert '"start_sec": number' in prompt
    assert '"end_sec": number' in prompt
    assert "not into fixed one-second rows" in prompt
    assert "Do not round boundaries to whole seconds" in prompt
    assert "Include every second" not in prompt


def test_copy_mode_previous_video_reference_only_for_continuation_chunks():
    session = {
        "copy_mode_generated_chunks": [
            {"chunk_index": 0, "file_id": "first-fixed"},
            {"chunk_index": 1, "file_id": "second-fixed"},
        ]
    }

    assert handlers_state_scene._copy_mode_previous_generated_file_id(session, chunk_index=0, total_chunks=3) is None
    assert handlers_state_scene._copy_mode_previous_generated_file_id(session, chunk_index=1, total_chunks=1) is None
    assert (
        handlers_state_scene._copy_mode_previous_generated_file_id(session, chunk_index=1, total_chunks=3)
        == "first-fixed"
    )
    assert (
        handlers_state_scene._copy_mode_previous_generated_file_id(session, chunk_index=2, total_chunks=3)
        == "second-fixed"
    )
