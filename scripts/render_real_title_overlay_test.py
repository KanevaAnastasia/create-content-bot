"""Render title overlay variants on an external generated image."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import post_production
from render_title_overlay_refs import (
    COLOR_VARIANTS,
    DAY_COUNTER_FONT_SIZE,
    FONT_SPECS,
    HEIGHT,
    MARGIN_X,
    OUTPUT_DIR,
    SAFE_TOP_HEIGHT,
    TEXT_MARGIN_X,
    TEXT_MAX_WIDTH,
    TITLE_TOP_PADDING,
    SERIES_TITLE_FONT_SIZE,
    SERIES_MAIN_TITLE_OVERLAP,
    WIDTH,
    OverlayBox,
    _draw_centered_multiline,
    _load_font,
    _multiline_bbox,
    _overlay_top_scrim,
    _series_title_for_render,
    _write_trial_contact_sheet,
)


REAL_OUTPUT_DIR = OUTPUT_DIR / "real_image_overlay"
REAL_TITLE_BAND_HEIGHT = int(HEIGHT * 0.40)
REAL_TITLE_MAX_HEIGHT = int(HEIGHT * 0.08)

DISH_TITLE_COLORS = {
    "малин": "#ff2f75",
    "raspberry": "#ff2f75",
    "клубнич": "#ff4f7b",
    "strawberry": "#ff4f7b",
    "лимонад": "#ffd84a",
    "lemonade": "#ffd84a",
    "борщ": "#c72638",
    "borscht": "#c72638",
    "щи": "#7dbb5d",
    "сырники": "#f2b84b",
    "морожен": "#ff9fc2",
}


def title_colors_for_dish(title: str) -> tuple[str, str]:
    """Choose a dish-colored title fill and a fixed black title shadow."""
    normalized = str(title or "").casefold()
    for needle, fill in DISH_TITLE_COLORS.items():
        if needle in normalized:
            return fill, "#000000"
    return "#fff7e8", "#000000"


def _prepare_background(path: Path) -> Image.Image:
    source = Image.open(path).convert("RGB")
    return ImageOps.fit(source, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS, centering=(0.5, 0.42))


def _draw_centered_title(
    draw: ImageDraw.ImageDraw,
    title_lines: list[str],
    title_font: ImageFont.FreeTypeFont,
    *,
    title_y: int,
    title_fill: str,
    title_echo: str,
) -> OverlayBox:
    spacing = max(8, int(title_font.size * 0.08))
    echo_offset = max(6, int(title_font.size * 0.04))
    _ = title_echo
    shadow_box = _draw_centered_multiline(
        draw,
        title_lines,
        title_font,
        y=title_y + echo_offset,
        fill="#000000",
        spacing=spacing,
        x_offset=echo_offset,
        stroke_width=0,
    )
    title_box = _draw_centered_multiline(
        draw,
        title_lines,
        title_font,
        y=title_y,
        fill=title_fill,
        spacing=spacing,
    )
    return OverlayBox(
        min(title_box.left, shadow_box.left),
        min(title_box.top, shadow_box.top),
        max(title_box.right, shadow_box.right),
        max(title_box.bottom, shadow_box.bottom),
    )


def _two_line_title(title: str) -> list[str]:
    words = " ".join(str(title or "").split()).split()
    if len(words) <= 1:
        return [" ".join(words)]
    best_index = 1
    best_score: tuple[int, int] | None = None
    for index in range(1, len(words)):
        first = " ".join(words[:index])
        second = " ".join(words[index:])
        score = (abs(len(first) - len(second)), abs(index - (len(words) // 2)))
        if best_score is None or score < best_score:
            best_score = score
            best_index = index
    return [" ".join(words[:best_index]), " ".join(words[best_index:])]


def _fit_fixed_title_lines(
    draw: ImageDraw.ImageDraw,
    title_lines: list[str],
    *,
    language: str,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], OverlayBox]:
    clean_lines = [line.strip() for line in title_lines if line.strip()]
    if not clean_lines:
        clean_lines = [""]
    title_spec = FONT_SPECS["ru_title" if language == "ru" else "en_title"]
    for size in range(start_size, min_size - 1, -2):
        font = _load_font(title_spec, size)
        spacing = max(8, int(size * 0.08))
        echo_offset = max(6, int(size * 0.04))
        bbox = _multiline_bbox(draw, clean_lines, font, spacing)
        if bbox.width <= max_width and bbox.height + echo_offset <= max_height:
            return font, clean_lines, bbox
    font = _load_font(title_spec, min_size)
    spacing = max(8, int(min_size * 0.08))
    return font, clean_lines, _multiline_bbox(draw, clean_lines, font, spacing)


def _draw_unscaled_series_row(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    row_y: int,
    row_x: int,
    series_text: str,
    series_font: ImageFont.FreeTypeFont,
    day_text: str,
    day_font: ImageFont.FreeTypeFont,
    gap: int,
    scale_x: int = 100,
) -> OverlayBox:
    series_bbox = _multiline_bbox(draw, [series_text], series_font, 0)
    day_bbox = _multiline_bbox(draw, [day_text], day_font, 0)
    row_width = series_bbox.width + gap + day_bbox.width
    row_height = max(series_bbox.height, day_bbox.height)
    row = Image.new("RGBA", (max(1, row_width), max(1, row_height)), (0, 0, 0, 0))
    row_draw = ImageDraw.Draw(row)
    row_draw.text(
        (-series_bbox.left, -series_bbox.top),
        series_text,
        font=series_font,
        fill="#ffffff",
        stroke_width=2,
        stroke_fill="#000000",
    )
    row_draw.text(
        (series_bbox.width + gap - day_bbox.left, -day_bbox.top),
        day_text,
        font=day_font,
        fill="#ffffff",
        stroke_width=2,
        stroke_fill="#000000",
    )
    target_width = max(1, int(round(row_width * (max(1, min(100, scale_x)) / 100.0))))
    if target_width != row_width:
        row = row.resize((target_width, row_height), Image.Resampling.LANCZOS)
    x = row_x
    y = row_y
    image.paste(row, (x, y), row)
    return OverlayBox(x, y, x + target_width, y + row_height)


def _assert_real_safe_bounds(name: str, boxes: list[OverlayBox]) -> None:
    for box in boxes:
        if box.left < MARGIN_X or box.right > WIDTH - MARGIN_X:
            raise AssertionError(f"{name}: overlay outside 10% horizontal margin: {box}")
        if box.top < 0 or box.bottom > REAL_TITLE_BAND_HEIGHT:
            raise AssertionError(f"{name}: overlay outside top 40% title band: {box}")


def _assert_real_visible_safe_bounds(name: str, before: Image.Image, after: Image.Image) -> None:
    diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB")).crop(
        (0, 0, WIDTH, REAL_TITLE_BAND_HEIGHT)
    )
    bbox = diff.getbbox()
    if bbox is None:
        raise AssertionError(f"{name}: no visible overlay pixels")
    left, top, right, bottom = bbox
    if left < MARGIN_X or right > WIDTH - MARGIN_X:
        raise AssertionError(f"{name}: visible overlay outside 10% horizontal margin: {(left, top, right, bottom)}")
    if top < 0 or bottom > REAL_TITLE_BAND_HEIGHT:
        raise AssertionError(f"{name}: visible overlay outside top 40% title band: {(left, top, right, bottom)}")


def render_real_variant(
    background: Image.Image,
    *,
    name: str,
    title: str,
    language: str,
    series_title: str,
    day_text: str,
    title_fill: str,
    title_echo: str,
    scrim_alpha: float,
    title_lines: list[str],
) -> None:
    image = _overlay_top_scrim(background, alpha=scrim_alpha)
    before = image.copy()
    draw = ImageDraw.Draw(image)

    language = str(language or "ru").strip().lower()
    series_text = _series_title_for_render(language, series_title)
    layout = post_production._subtitle_layout_for_dimensions(WIDTH, HEIGHT)
    title_font, fitted_title_lines, _title_bbox = _fit_fixed_title_lines(
        draw,
        title_lines,
        language=language,
        max_width=TEXT_MAX_WIDTH - 20,
        max_height=REAL_TITLE_MAX_HEIGHT,
        start_size=180,
        min_size=24,
    )
    preview_spacing = max(8, int(title_font.size * 0.08))
    preview_title_bbox = _multiline_bbox(draw, fitted_title_lines, title_font, preview_spacing)
    row_x = max(0, int(round((WIDTH - preview_title_bbox.width) / 2.0)))
    available_row_width = WIDTH - row_x - MARGIN_X
    series_fit = post_production._fit_title_series_ass_text(
        series_text,
        day_text,
        layout=layout,
        language=language,
        side_margin_ratio=0.10,
        max_width=available_row_width,
    )
    series_size = series_fit.series_font_size if series_fit else SERIES_TITLE_FONT_SIZE
    day_size = series_fit.counter_font_size if series_fit else DAY_COUNTER_FONT_SIZE
    series_scale_x = series_fit.scale_x if series_fit else 100
    series_font = _load_font(FONT_SPECS["ru_series" if language == "ru" else "en_series"], series_size)
    day_font = _load_font(FONT_SPECS["day_counter"], day_size)
    row_y = TITLE_TOP_PADDING
    row_box = _draw_unscaled_series_row(
        image,
        draw,
        row_y=row_y,
        row_x=row_x,
        series_text=series_text,
        series_font=series_font,
        day_text=day_text,
        day_font=day_font,
        gap=max(10, int(series_size * 0.16)),
        scale_x=series_scale_x,
    )

    title_y = row_y + row_box.height - SERIES_MAIN_TITLE_OVERLAP
    title_box = _draw_centered_title(
        draw,
        fitted_title_lines,
        title_font,
        title_y=title_y,
        title_fill=title_fill,
        title_echo=title_echo,
    )
    if title_box.height > REAL_TITLE_MAX_HEIGHT:
        raise AssertionError(f"{name}: title taller than 5% frame height: {title_box}")
    _ = (before, row_box)
    _assert_real_safe_bounds(name, [title_box])
    image.save(REAL_OUTPUT_DIR / name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", required=True, type=Path)
    parser.add_argument("--language", choices=("ru", "en"), default="ru")
    parser.add_argument("--title", default="КЛУБНИЧНЫЙ ЛИМОНАД")
    parser.add_argument("--series-title", default="Летние напитки")
    parser.add_argument("--day-text", default="день 3 из 10")
    args = parser.parse_args()

    REAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    background = _prepare_background(args.background)
    background.save(REAL_OUTPUT_DIR / "generated_background.png")

    semantic_fill, semantic_echo = title_colors_for_dish(args.title)
    language = str(args.language or "ru").strip().lower()
    variants = [
        (f"real_{language}_one_line_8pct.png", semantic_fill, semantic_echo, 0.24, [" ".join(args.title.split())]),
    ]
    names: list[str] = []
    for name, fill, echo, alpha, title_lines in variants:
        render_real_variant(
            background,
            name=name,
            title=args.title,
            language=language,
            series_title=args.series_title,
            day_text=args.day_text,
            title_fill=fill,
            title_echo=echo,
            scrim_alpha=alpha,
            title_lines=title_lines,
        )
        names.append(name)
    _write_trial_contact_sheet(REAL_OUTPUT_DIR, names)
    print(f"saved: {REAL_OUTPUT_DIR}")
    print(f"semantic_title_fill={semantic_fill}")
    print(f"semantic_title_echo={semantic_echo}")


if __name__ == "__main__":
    main()
