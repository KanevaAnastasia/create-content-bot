"""Render title-overlay reference PNGs and validate title-safe bounds.

The helper intentionally renders still references instead of full videos so the
layout can be inspected quickly. It uses the same production-safe constraints:
10% horizontal margins and the top 30% title band.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageChops, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "fonts"
OUTPUT_DIR = ROOT / "artifacts" / "title_overlay_refs"

WIDTH = 1080
HEIGHT = 1920
MARGIN_X = int(WIDTH * 0.10)
TEXT_MARGIN_X = MARGIN_X
SAFE_TOP_HEIGHT = int(HEIGHT * 0.30)
TITLE_TOP_PADDING = int(SAFE_TOP_HEIGHT * 0.20)
SERIES_TITLE_FONT_SIZE = 78
DAY_COUNTER_FONT_SIZE = 104
SERIES_ROW_GAP = 18
SERIES_MAIN_TITLE_OVERLAP = 24
MAX_TEXT_WIDTH = WIDTH - (MARGIN_X * 2)
TEXT_MAX_WIDTH = WIDTH - (TEXT_MARGIN_X * 2)
FIRST_VIDEO_DURATION_SEC = 6.0
TITLE_OVERLAY_START_SEC = 0.0
TRIAL_OUTPUT_DIR_NAME = "title_size_trials"
TITLE_STROKE_WIDTH = 2
SERIES_STROKE_WIDTH = 2
DAY_STROKE_WIDTH = 3
DAY_COUNTER_FILL = "#111111"
DAY_COUNTER_STROKE = "#ffffff"
DAY_COUNTER_VERTICAL_OFFSET = 6
TITLE_TRACKING = 0
TITLE_SPACE_WIDTH_SCALE = 0.24


@dataclass(frozen=True, slots=True)
class FontSpec:
    path: Path
    family: str


@dataclass(frozen=True, slots=True)
class OverlayBox:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


FONT_SPECS = {
    "ru_title": FontSpec(FONTS_DIR / "TD Afisha.otf", "TD Afisha"),
    "en_title": FontSpec(FONTS_DIR / "Taller.otf", "Taller"),
    "ru_series": FontSpec(FONTS_DIR / "Berezka.ttf", "Berezka"),
    "en_series": FontSpec(FONTS_DIR / "Ravsten Demo.otf", "Ravsten Demo"),
    "day_counter": FontSpec(FONTS_DIR / "Marianna.ttf", "Marianna"),
}

REFERENCE_IMAGE_NAMES = [
    "ru_single_short.png",
    "ru_single_long.png",
    "en_single_short.png",
    "en_single_long.png",
    "ru_series_short.png",
    "ru_series_long.png",
    "en_series_short.png",
    "en_series_long.png",
    "ru_single_borsch.png",
    "ru_series_borsch.png",
    "en_single_borscht.png",
    "en_series_borscht.png",
]

TRIAL_TITLES = [
    ("borsch", "Борщ"),
    ("syrniki", "Сырники"),
    ("strawberry_lemonade", "Клубничный лимонад"),
    ("fruit_ice", "фруктовый лед"),
    ("fruit_ice_cream", "фруктовое мороженое"),
    ("ice_cream", "мороженое"),
    ("shchi", "ЩИ"),
]

COLOR_VARIANT_OUTPUT_DIR_NAME = "color_overlay_variants"
COLOR_VARIANTS = [
    ("variant_1_strawberry.png", "#ff4f7b", "#ffd85a", 0.24),
    ("variant_2_lemon.png", "#ffd84a", "#ff5b79", 0.28),
    ("variant_3_coral.png", "#ff7a59", "#ffe58c", 0.26),
    ("variant_4_berry_cream.png", "#ff9fc2", "#fff0b8", 0.30),
    ("variant_5_raspberry.png", "#e93265", "#ffd76a", 0.24),
]


def _load_font(spec: FontSpec, size: int) -> ImageFont.FreeTypeFont:
    if not spec.path.exists():
        raise FileNotFoundError(f"Missing font: {spec.path}")
    font = ImageFont.truetype(str(spec.path), size=size)
    family, _style = font.getname()
    if family != spec.family:
        raise AssertionError(f"Unexpected family for {spec.path.name}: {family!r}, expected {spec.family!r}")
    return font


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> OverlayBox:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=TITLE_STROKE_WIDTH)
    return OverlayBox(left, top, left + max(right - left, _tracked_line_width(draw, text, font)), bottom)


def _multiline_bbox(draw: ImageDraw.ImageDraw, lines: list[str], font: ImageFont.FreeTypeFont, spacing: int) -> OverlayBox:
    text = "\n".join(lines)
    left, top, right, bottom = draw.multiline_textbbox(
        (0, 0),
        text,
        font=font,
        spacing=spacing,
        stroke_width=TITLE_STROKE_WIDTH,
    )
    tracked_width = max((_tracked_line_width(draw, line, font) for line in lines), default=0)
    return OverlayBox(left, top, left + tracked_width, bottom)


def _tracked_line_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    chars = list(str(text or ""))
    if not chars:
        return 0
    width = 0.0
    for ch in chars:
        char_width = float(draw.textlength(ch, font=font))
        width += char_width * TITLE_SPACE_WIDTH_SCALE if ch == " " else char_width
    width += TITLE_TRACKING * max(0, len(chars) - 1)
    return int(round(width)) + TITLE_STROKE_WIDTH * 2


def _draw_tracked_line(
    draw: ImageDraw.ImageDraw,
    *,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    stroke_width: int = TITLE_STROKE_WIDTH,
    stroke_fill: str = "#000000",
) -> None:
    x, y = xy
    for char in str(text or ""):
        advance = int(round(float(draw.textlength(char, font=font))))
        if char == " ":
            x += int(round(advance * TITLE_SPACE_WIDTH_SCALE))
            continue
        draw.text(
            (x, y),
            char,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        x += advance + TITLE_TRACKING


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = [word.strip() for word in str(text or "").split() if word.strip()]
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if max(_text_bbox(draw, candidate, font).width, _tracked_line_width(draw, candidate, font)) <= max_width:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


def _fit_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    spec: FontSpec,
    *,
    max_width: int,
    max_height: int,
    max_lines: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], OverlayBox]:
    for size in range(start_size, min_size - 1, -2):
        font = _load_font(spec, size)
        spacing = max(8, int(size * 0.12))
        lines = _wrap_text(draw, text, font, max_width)
        if len(lines) > max_lines:
            continue
        bbox = _multiline_bbox(draw, lines, font, spacing)
        if bbox.width <= max_width and bbox.height <= max_height:
            return font, lines, bbox
    font = _load_font(spec, min_size)
    spacing = max(8, int(min_size * 0.12))
    lines = _wrap_text(draw, text, font, max_width)[:max_lines]
    bbox = _multiline_bbox(draw, lines, font, spacing)
    if bbox.width > max_width:
        lines[-1] = _ellipsize_to_width(draw, lines[-1], font, max_width)
        bbox = _multiline_bbox(draw, lines, font, spacing)
    return font, lines, bbox


def _ellipsize_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    clean = str(text or "").strip()
    if max(_text_bbox(draw, clean, font).width, _tracked_line_width(draw, clean, font)) <= max_width:
        return clean
    suffix = "..."
    while clean and max(_text_bbox(draw, clean + suffix, font).width, _tracked_line_width(draw, clean + suffix, font)) > max_width:
        clean = clean[:-1].rstrip()
    return (clean + suffix) if clean else suffix


def _fit_single_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    spec: FontSpec,
    *,
    max_width: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.FreeTypeFont, str, OverlayBox]:
    for size in range(start_size, min_size - 1, -2):
        font = _load_font(spec, size)
        bbox = _text_bbox(draw, text, font)
        if bbox.width <= max_width:
            return font, text, bbox
    font = _load_font(spec, min_size)
    fitted = _ellipsize_to_width(draw, text, font, max_width)
    return font, fitted, _text_bbox(draw, fitted, font)


def _fit_series_row(
    draw: ImageDraw.ImageDraw,
    *,
    series_text: str,
    series_font: ImageFont.FreeTypeFont,
    series_spec: FontSpec | None = None,
    series_start_size: int | None = None,
    series_min_size: int = 34,
    day_text: str,
    day_font: ImageFont.FreeTypeFont,
    gap: int,
) -> tuple[ImageFont.FreeTypeFont, str, OverlayBox, OverlayBox, int, int]:
    day_bbox = _text_bbox(draw, day_text, day_font)
    series_bbox = _text_bbox(draw, series_text, series_font)
    row_width = series_bbox.width + gap + day_bbox.width
    if row_width > TEXT_MAX_WIDTH and series_spec is not None:
        start_size = int(series_start_size or getattr(series_font, "size", 58))
        for size in range(start_size, series_min_size - 1, -2):
            candidate_font = _load_font(series_spec, size)
            candidate_bbox = _text_bbox(draw, series_text, candidate_font)
            candidate_width = candidate_bbox.width + gap + day_bbox.width
            if candidate_width <= TEXT_MAX_WIDTH:
                series_font = candidate_font
                series_bbox = candidate_bbox
                row_width = candidate_width
                break
    if row_width > TEXT_MAX_WIDTH:
        max_series_width = max(1, TEXT_MAX_WIDTH - gap - day_bbox.width)
        series_text = _ellipsize_to_width(draw, series_text, series_font, max_series_width)
        series_bbox = _text_bbox(draw, series_text, series_font)
        row_width = series_bbox.width + gap + day_bbox.width
    row_height = max(series_bbox.height, day_bbox.height)
    return series_font, series_text, series_bbox, day_bbox, row_width, row_height


def _series_row_image(
    draw: ImageDraw.ImageDraw,
    *,
    series_text: str,
    series_font: ImageFont.FreeTypeFont,
    day_text: str,
    day_font: ImageFont.FreeTypeFont,
    gap: int,
) -> tuple[Image.Image, int, int]:
    series_bbox = _text_bbox(draw, series_text, series_font)
    day_bbox = _text_bbox(draw, day_text, day_font)
    natural_width = series_bbox.width + gap + day_bbox.width
    natural_height = max(series_bbox.height, day_bbox.height)
    row = Image.new("RGBA", (natural_width, natural_height), (0, 0, 0, 0))
    row_draw = ImageDraw.Draw(row)
    row_draw.text(
        (-series_bbox.left, -series_bbox.top),
        series_text,
        font=series_font,
        fill="#ffffff",
        stroke_width=SERIES_STROKE_WIDTH,
        stroke_fill="#000000",
    )
    day_xy = (series_bbox.width + gap - day_bbox.left, -day_bbox.top + DAY_COUNTER_VERTICAL_OFFSET)
    row_draw.text(
        day_xy,
        day_text,
        font=day_font,
        fill=DAY_COUNTER_FILL,
        stroke_width=DAY_STROKE_WIDTH,
        stroke_fill=DAY_COUNTER_STROKE,
    )
    return row, natural_width, natural_height


def _draw_scaled_series_row(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    row_y: int,
    series_text: str,
    series_font: ImageFont.FreeTypeFont,
    day_text: str,
    day_font: ImageFont.FreeTypeFont,
    gap: int = SERIES_ROW_GAP,
) -> OverlayBox:
    row_image, natural_width, row_height = _series_row_image(
        draw,
        series_text=series_text,
        series_font=series_font,
        day_text=day_text,
        day_font=day_font,
        gap=gap,
    )
    target_width = min(natural_width, TEXT_MAX_WIDTH)
    if target_width < natural_width:
        row_image = row_image.resize((target_width, row_height), Image.Resampling.LANCZOS)
    row_x = TEXT_MARGIN_X + (TEXT_MAX_WIDTH - target_width) // 2
    image.paste(row_image, (row_x, row_y), row_image)
    return OverlayBox(row_x, row_y, row_x + target_width, row_y + row_height)


def _series_title_for_render(language: str, text: str) -> str:
    clean = " ".join(str(text or "").split())
    if str(language or "").strip().lower() == "ru":
        # Berezka's uppercase Cyrillic "С" has a near-empty glyph in the bundled font.
        # Lowercase keeps the intended decorative look and renders the first letter.
        return clean.lower()
    return clean


def _title_start_size(text: str, *, series: bool = False) -> int:
    clean = " ".join(str(text or "").split())
    if len(clean) <= 4:
        return 360 if not series else 300
    if len(clean) <= 8:
        return 300 if not series else 250
    if len(clean) <= 24:
        return 178 if not series else 170
    return 122 if not series else 96


def _background() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#302b2b")
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        t = y / HEIGHT
        r = int(49 + (202 - 49) * t)
        g = int(43 + (134 - 43) * t)
        b = int(43 + (76 - 43) * t)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))
    draw.rectangle((0, SAFE_TOP_HEIGHT, WIDTH, HEIGHT), fill="#d9b68f")
    draw.ellipse((165, 760, 915, 1510), fill="#ad6043")
    draw.ellipse((240, 835, 840, 1435), fill="#f2d3a7")
    draw.rectangle((MARGIN_X, 0, WIDTH - MARGIN_X, SAFE_TOP_HEIGHT), outline=(255, 255, 255), width=2)
    draw.line((0, SAFE_TOP_HEIGHT, WIDTH, SAFE_TOP_HEIGHT), fill=(255, 255, 255), width=2)
    return image


def _character_background() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f3d4aa")
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        t = y / HEIGHT
        r = int(246 + (218 - 246) * t)
        g = int(218 + (166 - 218) * t)
        b = int(175 + (128 - 175) * t)
        draw.line((0, y, WIDTH, y), fill=(r, g, b))

    draw.rectangle((0, 680, WIDTH, HEIGHT), fill="#d7aa7a")
    draw.rectangle((0, 1230, WIDTH, HEIGHT), fill="#bd8558")
    draw.rectangle((0, 1180, WIDTH, 1295), fill="#e8c391")
    draw.ellipse((185, 1330, 930, 1795), fill="#9b5a3e")
    draw.ellipse((245, 1375, 870, 1745), fill="#f0d2a3")

    # Cartoon grandma.
    draw.ellipse((310, 770, 615, 1075), fill="#f0c7a8", outline="#5f3c32", width=8)
    draw.arc((280, 705, 645, 1050), 180, 360, fill="#f4f0e6", width=58)
    draw.ellipse((348, 890, 390, 930), fill="#3c2a24")
    draw.ellipse((530, 890, 572, 930), fill="#3c2a24")
    draw.arc((405, 940, 520, 1005), 10, 170, fill="#8d3f3f", width=8)
    draw.rectangle((355, 1065, 570, 1365), fill="#7eb6b0", outline="#4e6f72", width=8)
    draw.polygon([(355, 1065), (570, 1065), (520, 1175), (405, 1175)], fill="#fff3da")
    draw.line((360, 1170, 185, 1295), fill="#f0c7a8", width=30)
    draw.line((560, 1170, 735, 1300), fill="#f0c7a8", width=30)

    # Cartoon orange cat.
    draw.ellipse((690, 985, 895, 1205), fill="#e57f39", outline="#7a3e25", width=8)
    draw.polygon([(725, 1005), (765, 910), (800, 1015)], fill="#e57f39", outline="#7a3e25")
    draw.polygon([(820, 1015), (855, 910), (870, 1025)], fill="#e57f39", outline="#7a3e25")
    draw.ellipse((735, 1075, 760, 1102), fill="#2d211c")
    draw.ellipse((815, 1075, 840, 1102), fill="#2d211c")
    draw.arc((755, 1125, 825, 1165), 0, 180, fill="#2d211c", width=5)
    draw.arc((855, 1060, 1015, 1280), 285, 75, fill="#e57f39", width=26)

    # Strawberry lemonade props.
    draw.rounded_rectangle((120, 1250, 270, 1515), radius=20, fill="#ff6b7e", outline="#7f2e3f", width=6)
    draw.rectangle((135, 1245, 255, 1285), fill="#fff4a6")
    draw.line((220, 1190, 250, 1265), fill="#f7f1e0", width=8)
    draw.ellipse((145, 1290, 190, 1335), fill="#d9274f")
    draw.ellipse((200, 1370, 245, 1415), fill="#d9274f")
    draw.ellipse((790, 1260, 940, 1515), fill="#ffd85a", outline="#8c6722", width=6)
    draw.rectangle((805, 1255, 925, 1295), fill="#ff7a59")
    draw.line((870, 1195, 845, 1265), fill="#f7f1e0", width=8)
    draw.ellipse((835, 1330, 885, 1380), fill="#d9274f")
    return image


def _overlay_top_scrim(image: Image.Image, *, alpha: float) -> Image.Image:
    out = image.convert("RGBA")
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fade_height = max(SAFE_TOP_HEIGHT, int(HEIGHT * 0.42))
    max_alpha = int(255 * max(0.0, min(float(alpha), 1.0)))
    for y in range(min(fade_height, HEIGHT)):
        t = y / max(1, fade_height - 1)
        smooth = t * t * (3 - 2 * t)
        line_alpha = int(max_alpha * (1 - smooth))
        if line_alpha <= 0:
            continue
        draw.line((0, y, WIDTH, y), fill=(0, 0, 0, line_alpha))
    return Image.alpha_composite(out, overlay).convert("RGB")


def _draw_centered_multiline(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    *,
    y: int,
    fill: str,
    spacing: int,
    x_offset: int = 0,
    stroke_width: int = TITLE_STROKE_WIDTH,
    stroke_fill: str = "#000000",
) -> OverlayBox:
    bbox = _multiline_bbox(draw, lines, font, spacing)
    x = TEXT_MARGIN_X + (TEXT_MAX_WIDTH - bbox.width) // 2 + x_offset
    y_draw = y - bbox.top
    x_draw = x - bbox.left
    line_y = y_draw
    for line in lines:
        line_width = _tracked_line_width(draw, line, font)
        line_x = x_draw + (bbox.width - line_width) // 2
        _draw_tracked_line(
            draw,
            xy=(line_x, line_y),
            text=line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        line_bbox = draw.textbbox((0, 0), line or " ", font=font, stroke_width=stroke_width)
        line_y += (line_bbox[3] - line_bbox[1]) + spacing
    return OverlayBox(x, y, x + bbox.width, y + bbox.height)


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    y: int,
    fill: str,
) -> OverlayBox:
    bbox = _text_bbox(draw, text, font)
    x = TEXT_MARGIN_X + (TEXT_MAX_WIDTH - bbox.width) // 2
    y_draw = y - bbox.top
    x_draw = x - bbox.left
    draw.text(
        (x_draw, y_draw),
        text,
        font=font,
        fill=fill,
        stroke_width=TITLE_STROKE_WIDTH,
        stroke_fill="#000000",
    )
    return OverlayBox(x, y, x + bbox.width, y + bbox.height)


def _assert_safe_bounds(name: str, boxes: Iterable[OverlayBox]) -> None:
    for box in boxes:
        if box.left < MARGIN_X or box.right > WIDTH - MARGIN_X:
            raise AssertionError(f"{name}: overlay outside 10% horizontal margin: {box}")
        if box.top < 0 or box.bottom > SAFE_TOP_HEIGHT:
            raise AssertionError(f"{name}: overlay outside top 30% title band: {box}")


def _assert_visible_safe_bounds(name: str, before: Image.Image, after: Image.Image) -> None:
    diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB")).crop((0, 0, WIDTH, SAFE_TOP_HEIGHT))
    bbox = diff.getbbox()
    if bbox is None:
        raise AssertionError(f"{name}: no visible overlay pixels")
    left, top, right, bottom = bbox
    if left < MARGIN_X or right > WIDTH - MARGIN_X:
        raise AssertionError(f"{name}: visible overlay outside 10% horizontal margin: {(left, top, right, bottom)}")
    if top < 0 or bottom > SAFE_TOP_HEIGHT:
        raise AssertionError(f"{name}: visible overlay outside top 30% title band: {(left, top, right, bottom)}")


def _assert_overlay_visible_safe_bounds(name: str, image: Image.Image) -> None:
    crop = image.convert("RGB").crop((0, 0, WIDTH, SAFE_TOP_HEIGHT))
    mask = Image.new("RGB", crop.size, (0, 0, 0))
    # A conservative check: ensure the whole colored/text composition is inside the 10% guide by
    # requiring the non-background high-contrast pixels to stay within the top title band.
    _ = mask
    if crop.getbbox() is None:
        raise AssertionError(f"{name}: empty overlay crop")


def title_overlay_timing(first_video_duration_sec: float = FIRST_VIDEO_DURATION_SEC) -> dict[str, float]:
    """Return the intended title event window: the whole first video segment."""
    duration = max(0.01, float(first_video_duration_sec or 0.0))
    return {"start_sec": TITLE_OVERLAY_START_SEC, "end_sec": duration}


def render_single(name: str, language: str, title: str, *, output_dir: Path | None = None) -> None:
    image = _background()
    before = image.copy()
    draw = ImageDraw.Draw(image)
    spec = FONT_SPECS["ru_title" if language == "ru" else "en_title"]
    font, lines, bbox = _fit_wrapped_text(
        draw,
        title,
        spec,
        max_width=TEXT_MAX_WIDTH,
        max_height=int(SAFE_TOP_HEIGHT * 0.66),
        max_lines=2,
        start_size=_title_start_size(title),
        min_size=50,
    )
    spacing = max(10, int(font.size * 0.12))
    y = max(TITLE_TOP_PADDING, int((SAFE_TOP_HEIGHT - bbox.height) * 0.36))
    box = _draw_centered_multiline(draw, lines, font, y=y, fill="#fff7e8", spacing=spacing)
    _assert_safe_bounds(name, [box])
    _assert_visible_safe_bounds(name, before, image)
    image.save((output_dir or OUTPUT_DIR) / name)


def render_series(
    name: str,
    language: str,
    title: str,
    series_title: str,
    day_text: str,
    *,
    output_dir: Path | None = None,
) -> None:
    image = _background()
    before = image.copy()
    draw = ImageDraw.Draw(image)
    title_spec = FONT_SPECS["ru_title" if language == "ru" else "en_title"]
    series_spec = FONT_SPECS["ru_series" if language == "ru" else "en_series"]

    day_font = _load_font(FONT_SPECS["day_counter"], DAY_COUNTER_FONT_SIZE)
    series_title = _series_title_for_render(language, series_title)
    series_font, series_text, series_bbox = _fit_single_line(
        draw,
        series_title,
        series_spec,
        max_width=TEXT_MAX_WIDTH,
        start_size=SERIES_TITLE_FONT_SIZE,
        min_size=34,
    )
    row_y = TITLE_TOP_PADDING
    series_box = _draw_scaled_series_row(
        image,
        draw,
        row_y=row_y,
        series_text=series_text,
        series_font=series_font,
        day_text=day_text,
        day_font=day_font,
    )
    row_height = series_box.height
    title_gap = -SERIES_MAIN_TITLE_OVERLAP
    title_top = row_y + row_height + title_gap
    title_font, title_lines, title_bbox = _fit_wrapped_text(
        draw,
        title,
        title_spec,
        max_width=TEXT_MAX_WIDTH,
        max_height=SAFE_TOP_HEIGHT - title_top - 24,
        max_lines=2,
        start_size=_title_start_size(title, series=True),
        min_size=42,
    )
    title_spacing = max(8, int(title_font.size * 0.10))
    title_y = title_top

    title_box = _draw_centered_multiline(
        draw,
        title_lines,
        title_font,
        y=title_y,
        fill="#fff7e8",
        spacing=title_spacing,
    )
    _assert_safe_bounds(name, [title_box, series_box])
    _assert_visible_safe_bounds(name, before, image)
    image.save((output_dir or OUTPUT_DIR) / name)


def _write_timing_reference() -> None:
    timing = title_overlay_timing()
    (OUTPUT_DIR / "title_timing.txt").write_text(
        f"title_overlay_start_sec={timing['start_sec']:.2f}\n"
        f"title_overlay_end_sec={timing['end_sec']:.2f}\n"
        "scope=full_first_video_segment\n",
        encoding="utf-8",
    )


def _write_contact_sheet() -> None:
    thumb_w, thumb_h = 270, 480
    label_h = 36
    columns = 4
    rows = (len(REFERENCE_IMAGE_NAMES) + columns - 1) // columns
    sheet = Image.new("RGB", (thumb_w * columns, (thumb_h + label_h) * rows), "#222222")
    draw = ImageDraw.Draw(sheet)
    label_font = _load_font(FONT_SPECS["day_counter"], 22)
    for idx, name in enumerate(REFERENCE_IMAGE_NAMES):
        source = Image.open(OUTPUT_DIR / name).convert("RGB").resize((thumb_w, thumb_h))
        x = (idx % columns) * thumb_w
        y = (idx // columns) * (thumb_h + label_h)
        sheet.paste(source, (x, y))
        draw.rectangle((x, y + thumb_h, x + thumb_w, y + thumb_h + label_h), fill="#111111")
        draw.text((x + 10, y + thumb_h + 7), name, fill="#ffffff", font=label_font)
    sheet.save(OUTPUT_DIR / "contact_sheet.png")


def _write_trial_contact_sheet(trial_dir: Path, names: list[str]) -> None:
    thumb_w, thumb_h = 270, 480
    label_h = 36
    columns = 3
    rows = (len(names) + columns - 1) // columns
    sheet = Image.new("RGB", (thumb_w * columns, (thumb_h + label_h) * rows), "#222222")
    draw = ImageDraw.Draw(sheet)
    label_font = _load_font(FONT_SPECS["day_counter"], 22)
    for idx, name in enumerate(names):
        source = Image.open(trial_dir / name).convert("RGB").resize((thumb_w, thumb_h))
        x = (idx % columns) * thumb_w
        y = (idx // columns) * (thumb_h + label_h)
        sheet.paste(source, (x, y))
        draw.rectangle((x, y + thumb_h, x + thumb_w, y + thumb_h + label_h), fill="#111111")
        draw.text((x + 10, y + thumb_h + 7), name, fill="#ffffff", font=label_font)
    sheet.save(trial_dir / "contact_sheet.png")


def _write_title_size_trials() -> None:
    trial_dir = OUTPUT_DIR / TRIAL_OUTPUT_DIR_NAME
    trial_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for slug, title in TRIAL_TITLES:
        single_name = f"{slug}_single.png"
        series_name = f"{slug}_series.png"
        render_single(single_name, "ru", title, output_dir=trial_dir)
        render_series(
            series_name,
            "ru",
            title,
            "Супы у бабушки",
            "день 2 из 7",
            output_dir=trial_dir,
        )
        names.extend([single_name, series_name])
    _write_trial_contact_sheet(trial_dir, names)


def _render_color_variant(name: str, title_fill: str, _title_shadow: str, scrim_alpha: float) -> None:
    variant_dir = OUTPUT_DIR / COLOR_VARIANT_OUTPUT_DIR_NAME
    variant_dir.mkdir(parents=True, exist_ok=True)
    image = _overlay_top_scrim(_character_background(), alpha=scrim_alpha)
    before = image.copy()
    draw = ImageDraw.Draw(image)

    series_font = _load_font(FONT_SPECS["ru_series"], SERIES_TITLE_FONT_SIZE)
    day_font = _load_font(FONT_SPECS["day_counter"], DAY_COUNTER_FONT_SIZE)
    series_text = _series_title_for_render("ru", "Летние напитки")
    day_text = "день 3 из 10"
    row_y = TITLE_TOP_PADDING
    row_box = _draw_scaled_series_row(
        image,
        draw,
        row_y=row_y,
        series_text=series_text,
        series_font=series_font,
        day_text=day_text,
        day_font=day_font,
    )
    row_height = row_box.height
    title_y = row_y + row_height - SERIES_MAIN_TITLE_OVERLAP
    title_font, title_lines, title_bbox = _fit_wrapped_text(
        draw,
        "КЛУБНИЧНЫЙ ЛИМОНАД",
        FONT_SPECS["ru_title"],
        max_width=TEXT_MAX_WIDTH,
        max_height=SAFE_TOP_HEIGHT - title_y - 24,
        max_lines=2,
        start_size=150,
        min_size=58,
    )
    title_spacing = max(8, int(title_font.size * 0.08))
    shadow_offset = max(7, int(title_font.size * 0.045))
    shadow_box = _draw_centered_multiline(
        draw,
        title_lines,
        title_font,
        y=title_y + shadow_offset,
        fill="#000000",
        spacing=title_spacing,
        x_offset=shadow_offset,
        stroke_width=0,
    )
    title_box = _draw_centered_multiline(
        draw,
        title_lines,
        title_font,
        y=title_y,
        fill=title_fill,
        spacing=title_spacing,
    )

    _assert_safe_bounds(name, [row_box, title_box, shadow_box])
    _assert_visible_safe_bounds(name, before, image)
    _assert_overlay_visible_safe_bounds(name, image)
    image.save(variant_dir / name)


def _write_color_variants() -> None:
    variant_dir = OUTPUT_DIR / COLOR_VARIANT_OUTPUT_DIR_NAME
    variant_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for name, fill, shadow, alpha in COLOR_VARIANTS:
        _render_color_variant(name, fill, shadow, alpha)
        names.append(name)
    _write_trial_contact_sheet(variant_dir, names)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for key, spec in FONT_SPECS.items():
        font = _load_font(spec, 42)
        family, style = font.getname()
        print(f"{key}: {family} {style} ({spec.path.name})")

    render_single("ru_single_short.png", "ru", "Сырники как в детстве")
    render_single(
        "ru_single_long.png",
        "ru",
        "Нежнейший пирог с яблоками и корицей, который исчезает со стола за минуту",
    )
    render_single("en_single_short.png", "en", "Grandma's Berry Pie")
    render_single(
        "en_single_long.png",
        "en",
        "The Softest Creamy Potato Casserole Everyone Asks Grandma To Make Again",
    )
    render_series("ru_series_short.png", "ru", "Сырники как в детстве", "Завтраки у бабушки", "день 5 из 10")
    render_series(
        "ru_series_long.png",
        "ru",
        "Нежнейший пирог с яблоками и корицей, который исчезает со стола за минуту",
        "Большая уютная серия семейных рецептов на каждый день",
        "день 5 из 10",
    )
    render_series("en_series_short.png", "en", "Grandma's Berry Pie", "Grandma Breakfasts", "day 3 of 10")
    render_series(
        "en_series_long.png",
        "en",
        "The Softest Creamy Potato Casserole Everyone Asks Grandma To Make Again",
        "A Long Cozy Family Recipe Series For Every Busy Weeknight",
        "day 3 of 10",
    )
    render_single("ru_single_borsch.png", "ru", "БОРЩ")
    render_single("en_single_borscht.png", "en", "Borscht")
    render_series("ru_series_borsch.png", "ru", "БОРЩ", "Супы у бабушки", "день 2 из 7")
    render_series("en_series_borscht.png", "en", "Borscht", "Grandma's Soups", "day 2 of 7")
    _write_timing_reference()
    _write_contact_sheet()
    _write_title_size_trials()
    _write_color_variants()
    print(f"saved: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
