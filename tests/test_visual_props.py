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
