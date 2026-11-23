from app.visual_props import append_visual_prop_descriptions, matched_visual_props


def test_matched_visual_props_detects_russian_aliases():
    props = matched_visual_props("Бабушка ставит кастрюлю на плиту и держит ложку.")
    keys = [p.canonical_key for p in props]
    assert "stove" in keys
    assert "pot" in keys
    assert "spoon" in keys


def test_append_visual_prop_descriptions_adds_only_mentioned_props():
    out = append_visual_prop_descriptions("Крупный план: кастрюля на плите.")
    assert "Визуальные константы предметов:" in out
    assert "Кастрюля выглядит так:" in out
    assert "Плита выглядит так:" in out
    assert "Духовка выглядит так:" not in out


def test_append_visual_prop_descriptions_is_idempotent():
    once = append_visual_prop_descriptions("Тарелка стоит на столе.")
    twice = append_visual_prop_descriptions(once)
    assert twice == once


def test_visual_props_include_blender_and_mixer_presets():
    blender = append_visual_prop_descriptions("Бананы отправляем в блендер и взбиваем.")
    assert "Блендер выглядит так:" in blender
    assert "моторной базе" in blender
    assert "закрыта крышкой" in blender

    mixer = append_visual_prop_descriptions("Взбиваем сливки ручным миксером в миске.")
    assert "Миксер выглядит так:" in mixer
    assert "насадками-венчиками" in mixer


def test_soviet_gas_stove_and_room_rules_reach_both_planning_and_images(monkeypatch):
    from app import visual_props
    from app.grandma_kitchen import GAS_STOVE_DESCRIPTION, GAS_OVEN_DESCRIPTION, KITCHEN_LAYOUT_RULES
    from app.image_atmosphere import add_image_atmosphere
    from app.grandma_editorial import GRANDMA_STORY_RULES, GRANDMA_PRODUCTION_RULES
    monkeypatch.setattr(visual_props, 'load_visual_props', lambda: visual_props.DEFAULT_VISUAL_PROPS)
    prompt = append_visual_prop_descriptions('Кастрюля на плите, духовка закрыта.')
    assert GAS_STOVE_DESCRIPTION in prompt
    assert GAS_OVEN_DESCRIPTION in prompt
    for rules in (GRANDMA_STORY_RULES, GRANDMA_PRODUCTION_RULES, add_image_atmosphere(prompt)):
        assert KITCHEN_LAYOUT_RULES in rules
        assert '2 на 2' in rules
    once = add_image_atmosphere(prompt)
    assert add_image_atmosphere(once) == once


def test_episode_hob_cannot_restore_electric_hotplate():
    from app.grandma_scene_contract import lock_episode_props
    from app.grandma_kitchen import GAS_STOVE_DESCRIPTION
    scenes = [{'prop_descriptions': {'hob': 'Одна электрическая конфорка на столе.'}}]
    lock_episode_props(scenes)
    assert scenes[0]['prop_descriptions']['hob'] == GAS_STOVE_DESCRIPTION
    assert scenes[0]['episode_prop_catalog']['hob'] == GAS_STOVE_DESCRIPTION
