from app.dish_visuals import build_dish_visual_brief


def test_dish_visual_brief_rejects_invented_ingredients_generically():
    brief = build_dish_visual_brief(
        "Бешбармак",
        "Отварить мясо, нарезать лук полукольцами, сварить пласты теста.",
    )

    assert "Отварить мясо" in brief
    assert "Do not invent extra toppings" in brief
    assert "boiled eggs" not in brief.lower()


def test_dish_visual_brief_requires_canonical_appearance_and_readable_framing():
    brief = build_dish_visual_brief("Баурсаки", "Нарезать тесто ромбами и обжарить во фритюре.")

    assert "canonical real-world appearance" in brief
    assert "The main food must be framed" in brief
    assert "small puffy pieces of deep-fried dough" in brief


def test_dish_visual_brief_has_chak_chak_canonical_structure():
    brief = build_dish_visual_brief("Чак-чак", "Обжарить кусочки теста и смешать с медовым сиропом.")

    assert "many small, irregular solid pieces" in brief
    assert "glossy honey syrup into a mound" in brief
    assert "piece shape, ingredient structure, surface texture, or serving form" in brief
