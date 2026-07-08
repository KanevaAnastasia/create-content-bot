from app.character_catalog import CHARACTER_CATALOG
from app.character_catalog import character_identity_lines
from app.character_catalog import maybe_choose_romantic_subplot
from app.character_catalog import sanitize_character_names_and_keys


def test_maybe_choose_romantic_subplot_disabled():
    draws = iter([0.95])

    out = maybe_choose_romantic_subplot(random_fn=lambda: next(draws))

    assert out["enabled"] is False
    assert out["characters"] == []
    assert out["decision_scope"] == "episode"
    assert "series" in out["series_rule"].lower()


def test_maybe_choose_romantic_subplot_uses_forty_percent_thresholds():
    zaya_disabled = maybe_choose_romantic_subplot(random_fn=lambda: 0.40)
    assert zaya_disabled["enabled"] is False

    draws = iter([0.39, 0.40])
    zaya_only = maybe_choose_romantic_subplot(random_fn=lambda: next(draws))
    assert zaya_only["enabled"] is True
    assert zaya_only["characters"] == ["zaya"]

    draws = iter([0.39, 0.39])
    with_markiz = maybe_choose_romantic_subplot(random_fn=lambda: next(draws))
    assert with_markiz["characters"] == ["zaya", "markiz"]


def test_neighbor_cat_roles_are_encoded():
    assert "соседская" in CHARACTER_CATALOG["zaya"]["role"]
    assert "соседский" in CHARACTER_CATALOG["markiz"]["role"]
    assert "за окном" not in CHARACTER_CATALOG["zaya"]["description"]
    assert "за окном" not in CHARACTER_CATALOG["markiz"]["description"].lower()


def test_short_descriptions_are_encoded_separately():
    assert CHARACTER_CATALOG["cat"]["short_description"] == "рыжий кот"
    assert CHARACTER_CATALOG["zaya"]["short_description"] == "белая кошка"
    assert CHARACTER_CATALOG["markiz"]["short_description"] == "черно-белый кот"
    assert CHARACTER_CATALOG["grandma"]["short_description"] == "бабушка"


def test_individual_cat_descriptions_do_not_cross_reference_other_cats():
    forbidden_by_key = {
        "cat": ("Зая", "Заи", "Заю", "Маркиз", "Маркиза"),
        "zaya": ("Тимош", "Маркиз", "Маркиза"),
        "markiz": ("Зая", "Заи", "Заю", "Тимош"),
    }
    for key, forbidden in forbidden_by_key.items():
        text = f"{CHARACTER_CATALOG[key]['description']}"
        assert not any(token in text for token in forbidden)


def test_character_identity_lines_use_short_descriptions_not_names():
    out = "\n".join(character_identity_lines(["cat", "zaya", "markiz"]))

    assert "рыжий кот:" in out
    assert "белая кошка:" in out
    assert "черно-белый кот:" in out
    assert "zaya" not in out
    assert "markiz" not in out
    assert "Тимоша" not in out
    assert "Зая" not in out
    assert "Маркиз" not in out


def test_sanitize_character_names_and_keys_removes_generated_aliases():
    out = sanitize_character_names_and_keys("Zaya и zaya смотрят на Timosha, а Markiz и markiz стоят рядом. Зая зовёт Маркиза.")

    assert "Zaya" not in out
    assert "zaya" not in out
    assert "Timosha" not in out
    assert "Markiz" not in out
    assert "markiz" not in out
    assert "белая кошка" in out
    assert "рыжий кот" in out
    assert "черно-белый кот" in out


def test_maybe_choose_romantic_subplot_adds_zaya_only():
    draws = iter([0.20, 0.85])

    out = maybe_choose_romantic_subplot(random_fn=lambda: next(draws))

    assert out["enabled"] is True
    assert out["characters"] == ["zaya"]
    assert out["brief"] == ""
    assert out["decision_scope"] == "episode"
    assert out["llm_should_create_drama_case"] is True
    assert out["zaya_timosha_arc"] == ""
    assert "whole episode" in out["episode_rule"]


def test_maybe_choose_romantic_subplot_can_include_markiz():
    draws = iter([0.20, 0.05])

    out = maybe_choose_romantic_subplot(random_fn=lambda: next(draws))

    assert out["enabled"] is True
    assert out["characters"] == ["zaya", "markiz"]
    assert out["brief"] == ""
    assert out["markiz_arc"] == ""
