from app.dish_visuals import build_dish_visual_brief


def test_dish_visual_brief_rejects_invented_ingredients_generically():
    brief = build_dish_visual_brief(
        "Бешбармак",
        "Отварить мясо, нарезать лук полукольцами, сварить пласты теста.",
    )

    assert "Отварить мясо" in brief
    assert "Do not invent extra toppings" in brief
    assert "boiled eggs" not in brief.lower()
