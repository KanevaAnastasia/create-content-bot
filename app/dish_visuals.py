"""Dish appearance guidance for prompt/image QA."""

from __future__ import annotations

import re


_KNOWN_DISH_VISUAL_BRIEFS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(чак[\s-]*чак|chak[\s-]*chak|chakchak)\b", re.IGNORECASE),
        (
            "Чак-чак / chak-chak: a Tatar/Bashkir honey dessert made from many small, irregular "
            "solid pieces or short uneven strips of deep-fried dough bound together with glossy honey "
            "syrup into a mound or compact pile. The pieces are golden yellow to golden brown, shiny, "
            "slightly porous on the fried surface, and visibly stuck together by honey. Reject substitutes "
            "where the piece shape, ingredient structure, surface texture, or serving form does not match "
            "authentic honey-bound fried dough."
        ),
    ),
    (
        re.compile(r"\b(baursak|baursaki|баурсак|баурсаки|баурсаки)\b", re.IGNORECASE),
        (
            "Баурсаки / baursaki: small puffy pieces of deep-fried dough, usually irregular "
            "round, oval, diamond, or pillow-like shapes. They are golden to deep golden brown "
            "with a slightly blistered fried surface, soft airy interior if cut or bitten, and are "
            "often served as a pile in a bowl or basket. They must not look like pancakes, flatbread, "
            "dumplings in soup, pasta, donuts with holes, cookies, or generic bread rolls."
        ),
    ),
    (
        re.compile(r"\b(лагман|lagman|laghman)\b", re.IGNORECASE),
        (
            "Лагман / lagman: a Central Asian noodle dish with long hand-pulled noodles, rich "
            "vegetable and meat gravy, red-orange broth or sauce, peppers, onions, carrots, herbs, "
            "and visible noodle strands. It must not look like dry pasta, plain soup without noodles, "
            "or unrelated stew."
        ),
    ),
)

_INGREDIENT_FIDELITY_RULE = (
    "Visible ingredients and garnishes must be supported by the recipe text, explicit user request, "
    "or this dish brief. Do not invent extra toppings, sides, garnishes, or ingredients just because "
    "they are common in some variants of the dish."
)

_CANONICAL_APPEARANCE_RULE = (
    "Before judging or generating any food frame, identify the dish's canonical real-world appearance "
    "from the dish name, recipe, cooking method, and culinary knowledge: expected shape, size, color, "
    "surface texture, interior/crumb/cut view if relevant, sauce/broth state, plating/serving form, "
    "and how the listed ingredients normally look after this step. Use that canonical appearance as "
    "the visual target; if the dish is unfamiliar, do not replace it with a generic nearby food."
)

_FRAMING_FIDELITY_RULE = (
    "The main food must be framed so its shape, texture, and ingredient state are readable. Do not "
    "crop off the main dish, key cut/open face, or required food object so severely that the viewer "
    "or QA model cannot verify the dish."
)


def build_dish_visual_brief(title: str | None, recipe_text: str | None) -> str:
    """Return a compact real-world visual target for image prompts and AI approval."""

    title_text = str(title or "").strip()
    recipe = str(recipe_text or "").strip()
    haystack = f"{title_text}\n{recipe}"
    for pattern, brief in _KNOWN_DISH_VISUAL_BRIEFS:
        if pattern.search(haystack):
            return f"{brief} {_CANONICAL_APPEARANCE_RULE} {_INGREDIENT_FIDELITY_RULE} {_FRAMING_FIDELITY_RULE}"

    ingredients_hint = ""
    for marker in ("ингредиент", "ingredients", "состав"):
        pos = recipe.lower().find(marker)
        if pos >= 0:
            ingredients_hint = recipe[pos : pos + 700].strip()
            break
    if not ingredients_hint:
        ingredients_hint = recipe[:700].strip()

    dish_name = title_text or "the recipe dish"
    return (
        f"Real-world visual target for {dish_name}: infer the final dish appearance from the recipe, "
        "ingredients, cooking method, and known culinary context. The generated frame must show the "
        "actual dish or current recipe state, with correct shape, color, texture, serving style, and "
        "ingredient form. If the dish is culturally specific or unfamiliar, do not substitute a generic "
        "similar-looking food; describe the likely authentic appearance explicitly. Do not add visible "
        "ingredients or garnishes that are not supported by the recipe text or explicit user request; "
        "reject frames that look like an unrelated dish or include invented ingredients. Recipe visual clues: "
        f"{ingredients_hint or '-'} {_CANONICAL_APPEARANCE_RULE} {_INGREDIENT_FIDELITY_RULE} {_FRAMING_FIDELITY_RULE}"
    )


def append_dish_visual_brief_to_prompt(prompt: str, dish_visual_brief: str | None) -> str:
    base = str(prompt or "").strip()
    brief = str(dish_visual_brief or "").strip()
    if not brief:
        return base
    if brief in base:
        return base
    return (
        f"{base}\n\n"
        "REAL DISH VISUAL REQUIREMENTS: "
        f"{brief}\n"
        "If this frame shows the cooked/final dish or a payoff serving, the food must match these real-world "
        "dish requirements exactly. If this frame shows an intermediate recipe step, show the correct current "
        "ingredient/process state and make it visually consistent with this dish; do not replace it with a generic "
        "or unrelated food. Do not add visible ingredients or garnishes that are absent from the recipe text, "
        "user request, or these dish requirements. Keep the main dish or key food object sufficiently inside "
        "the frame so its shape, texture, and ingredient state are verifiable; avoid severe crop of the food."
    ).strip()
