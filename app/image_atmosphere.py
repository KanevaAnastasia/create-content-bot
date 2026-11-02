"""Shared interior direction for image generation only."""

SOVIET_IMAGE_ATMOSPHERE = (
    "Image interior direction: cozy, lived-in Soviet family home. This direction replaces conflicting "
    "furniture and decorative tableware descriptions, while preserving the action, food, characters, "
    "weather, framing and functional cooking utensils. White openwork lace curtains on painted wooden "
    "window frames; a crocheted Soviet lace tablecloth with scalloped edges on a wooden dining table; "
    "small faded floral wallpaper; a polished wooden sideboard, modest vintage cabinets and bentwood "
    "chairs. Where the room and framing allow, include an old CRT television on a wooden stand with "
    "a crocheted lace doily on top, a patterned rug, embroidered towels, potted plants and a fabric "
    "lampshade. Tableware: Soviet floral porcelain cups and saucers, enamel pots with dark rims, "
    "faceted glasses and simple ceramic dishes, keeping their cooking function unchanged. Use a warm, "
    "cared-for domestic atmosphere and the project's existing cartoon style. Do not crowd close-ups "
    "with every furnishing, add furniture outdoors, obscure the food, or add text or logos."
)


def add_image_atmosphere(prompt: str) -> str:
    text = str(prompt or "").strip()
    if SOVIET_IMAGE_ATMOSPHERE in text:
        return text
    return f"{text}\n\n{SOVIET_IMAGE_ATMOSPHERE}".strip()
