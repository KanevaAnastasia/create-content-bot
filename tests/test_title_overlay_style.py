import io

from PIL import Image, ImageChops
import pytest

from app import post_production as production


@pytest.mark.parametrize("language,title", [("ru", "БОРЩ"), ("en", "BUTTERBEER")])
@pytest.mark.parametrize("width,height,shadow", [
    (1080, 1920, "12"), (2160, 3840, "24"), (3840, 2160, "24"),
])
def test_title_uses_language_specific_typography_and_keeps_admin_color(
    tmp_path, language, title, width, height, shadow,
):
    path = tmp_path / "title.ass"
    production._write_burned_subtitle_ass(
        path, [],
        title_cues=[production._build_title_overlay_cue({language: title}, language=language, duration_sec=6)],
        title_style={"title_color": "#FF4F7B"}, video_width=width, video_height=height,
    )
    style = next(line for line in path.read_text().splitlines() if line.startswith("Style: TitleOverlay"))
    fields = style.removeprefix("Style: ").split(",")
    assert fields[3] == "&H007B4FFF"  # Admin RGB converted to ASS BGR.
    if language == "en":
        assert fields[1] == "Taller"
        assert fields[7] == "0"  # Preserve the original English face without synthetic bold.
        assert fields[15:18] == ["1", "5", "12"]
    else:
        assert fields[1] == "TD Afisha"
        assert fields[7] == "-1"
        assert fields[15:18] == ["1", "0", shadow]
    assert fields[6] == "&H00000000"  # Opaque hard black shadow.


def test_english_color_change_preserves_all_typography_and_event_layout(tmp_path):
    payloads = []
    for color in ("#FFF7E8", "#FFC766"):
        path = tmp_path / f"{color[1:]}.ass"
        production._write_burned_subtitle_ass(
            path, [], title_cues=[production._build_title_overlay_cue(
                {"en": "Why Not You?"}, language="en", duration_sec=3,
            )], title_style={"title_color": color}, video_width=2160, video_height=3840,
        )
        payloads.append(path.read_text())
    assert payloads[0].replace("&H00E8F7FF", "&H0066C7FF") == payloads[1]


def test_preview_color_changes_fill_without_changing_geometry():
    background = Image.new("RGB", (1080, 1920), "#927453")
    images = []
    for color in ("#FF4400", "#FFF7E8"):
        payload = production.render_title_overlay_preview(
            background, language="ru", title="СЛИВОЧНОЕ ПИВО", title_style={"title_color": color},
        )
        image = Image.open(io.BytesIO(payload)).convert("RGB")
        assert image.size == background.size
        assert image.getpixel((540, 960)) == background.getpixel((540, 960))
        colors = image.getcolors(image.width * image.height)
        expected_fill = tuple(bytes.fromhex(color[1:]))
        assert any(count > 100 and pixel == expected_fill for count, pixel in colors)
        assert any(count > 100 and pixel == (0, 0, 0) for count, pixel in colors)
        images.append(image)
    boxes = [ImageChops.difference(image, background).getbbox() for image in images]
    assert boxes[0] == boxes[1]
    left, top, right, bottom = boxes[0]
    assert 100 <= left < right <= 972
    assert 96 <= top < bottom < 320
    assert ImageChops.difference(*images).getbbox() is not None
