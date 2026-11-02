import io

from PIL import Image, ImageDraw

from app.story_image_postprocess import crop_story_reference_border, finish_story_historical_frame


def _bordered_watercolor() -> bytes:
    image = Image.new("RGB", (360, 640), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 24, 341, 615), fill=(226, 217, 193), outline=(115, 96, 70), width=2)
    draw.rectangle((90, 180, 270, 520), fill=(98, 111, 87))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_historical_story_frame_removes_white_sheet_margin_and_keeps_vertical_ratio():
    result = finish_story_historical_frame(
        _bordered_watercolor(),
        {"generation_mode": "story", "time_period": "past", "era_style": "dry watercolor sepia"},
    )

    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (360, 640)
        edge = image.convert("RGB").crop((0, 0, 3, image.height))
        assert min(channel for pixel in edge.getdata() for channel in pixel) < 245


def test_non_story_image_is_not_modified():
    source = _bordered_watercolor()
    assert finish_story_historical_frame(source, {"generation_mode": "normal"}) == source


def test_story_reference_border_is_cropped_without_forcing_output_ratio():
    source = _bordered_watercolor()
    result = crop_story_reference_border(source)

    with Image.open(io.BytesIO(result)) as image:
        assert image.width < 360
        assert image.height < 640
        assert image.width / image.height != 9 / 16


def test_full_bleed_reference_is_returned_unchanged():
    image = Image.new("RGB", (100, 160), (90, 110, 80))
    out = io.BytesIO()
    image.save(out, format="PNG")
    source = out.getvalue()
    assert crop_story_reference_border(source) == source
