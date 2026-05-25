import base64

from PIL import Image

from app.syntax_image_prepare import prepare_syntax_frame_bytes


def test_prepare_syntax_frame_bytes_upscales_tiny_png(monkeypatch):
    monkeypatch.delenv("SYNTAX_MIN_IMAGE_EDGE_PX", raising=False)
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5+9XkAAAAASUVORK5CYII="
    )
    out = prepare_syntax_frame_bytes(tiny_png)
    with Image.open(__import__("io").BytesIO(out)) as im:
        assert im.size[0] >= 300
        assert im.size[1] >= 300
