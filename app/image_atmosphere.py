"""Shared interior direction for image generation only."""

import re

SOVIET_IMAGE_ATMOSPHERE = (
    "Image interior direction: a cozy, visibly lived-in Soviet family home, never a blank studio, empty "
    "showroom or generic modern beige room. This direction overrides conflicting furniture, empty-wall "
    "and decorative tableware descriptions, while preserving the action, food, characters, weather, "
    "framing and functional cooking utensils. In every interior kitchen or dining-room frame, visibly "
    "include at least three recognizable home anchors: (1) small faded floral wallpaper or white openwork "
    "lace curtains on painted wooden window frames, (2) a crocheted Soviet lace tablecloth with scalloped "
    "edges visibly covering part of the wooden dining table, and (3) a polished wooden sideboard or an old "
    "CRT television on a wooden stand with a crocheted lace doily. Add supporting lived-in details when "
    "visible: modest vintage cabinets, bentwood chairs, a patterned rug, embroidered towels, potted plants "
    "and a fabric lampshade. Even in a close-up, retain at least two recognizable anchors at the frame edges "
    "or in the background without obscuring the food. For scene 1 with a title-safe upper area, use softly "
    "lit small floral wallpaper, a lace-curtained window, or another low-contrast authentic room surface "
    "behind the title; never turn that area into a plain empty wall or a featureless band. Place the "
    "sideboard or CRT television lower or to one side so the title remains readable. Tableware: Soviet "
    "floral porcelain cups and saucers, enamel pots with dark rims, faceted glasses and simple ceramic "
    "dishes, keeping their cooking function unchanged. Use a warm, cared-for domestic atmosphere and the "
    "project's existing cartoon style. Do not add interior furniture outdoors, obscure the food, or add "
    "text or logos. Frame grandma waist-up with her face intact, hands and food large in the foreground. "
    "The cat remains recognizable; lower bodies may be outside the frame. Recognizable room anchors "
    "may be partial details at the frame edges. Never widen the composition to show floor, legs, "
    "furniture legs or entire background objects. Holiday decorations supplement this home."
)

_EMPTY_TITLE_WALL_PATTERNS = (
    re.compile(
        r"Верхние\s+примерно\s+30%\s+кадра[^.]*?(?:стен\w*|однотонн\w+\s+фон\w*)[^.]*\.\s*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:The\s+)?(?:upper|top)\s+(?:roughly|approximately|about)?\s*30%[^.]*?"
        r"(?:plain|blank|evenly lit|featureless)[^.]*?(?:wall|background)[^.]*\.\s*",
        re.IGNORECASE,
    ),
)

_LIVED_IN_TITLE_AREA = (
    "Верхние примерно 30% кадра остаются свободными от персонажей, лиц, рук, еды, посуды и крупных "
    "предметов, но это часть живого советского интерьера: неконтрастные мелкие цветочные обои и край "
    "белой кружевной занавески создают узнаваемый фон под заголовок. Это не пустая однотонная стена. "
    "Сервант и старый телевизор с кинескопом стоят ниже либо сбоку и не мешают заголовку. "
)


def _replace_empty_title_wall(prompt: str) -> str:
    text = prompt
    for pattern in _EMPTY_TITLE_WALL_PATTERNS:
        text, count = pattern.subn(_LIVED_IN_TITLE_AREA, text, count=1)
        if count:
            break
    return text.strip()


def add_image_atmosphere(prompt: str) -> str:
    text = _replace_empty_title_wall(str(prompt or "").strip())
    if SOVIET_IMAGE_ATMOSPHERE in text:
        return text
    return f"{text}\n\n{SOVIET_IMAGE_ATMOSPHERE}".strip()
