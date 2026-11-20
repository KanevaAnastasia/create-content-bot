import base64
import io
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app import openai_images, vertex_images
from app.media_costs import estimate_image_generation_cost_usd


@pytest.mark.parametrize("ratio,size,with_refs", [
    ("9:16", (864, 1536), True),
    ("16:9", (1536, 864), False),
    ("1:1", (1024, 1024), True),
])
def test_image_provider_preserves_scene_references_and_format(monkeypatch, ratio, size, with_refs):
    monkeypatch.setenv("IMAGE_GENERATION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-project-key")
    output = io.BytesIO()
    Image.new("RGB", size, "blue").save(output, format="PNG")
    usage = {"input_tokens_details": {"text_tokens": 200, "image_tokens": 300}, "output_tokens": 1500}
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}], "usage": usage}
    post = MagicMock()
    post.return_value.__enter__.return_value = response
    monkeypatch.setattr(openai_images.requests, "post", post)
    refs = [b"\xff\xd8\xffgrandma", b"\x89PNG\r\n\x1a\ncat"] if with_refs else []
    prompt = "Бабушка и Тимоша на кухне. Сохрани лица, палитру и рисунок с образца."

    result = vertex_images.generate_image_vertex_result(
        prompt, reference_images=refs, aspect_ratio=ratio, model="gemini-3-pro-image",
    )

    post.assert_called_once()
    url, = post.call_args.args
    params = post.call_args.kwargs
    if with_refs:
        assert url == "https://api.openai.com/v1/images/edits"
        assert params["files"] == [("image[]", ("reference-1.jpg", refs[0], "image/jpeg")),
                                   ("image[]", ("reference-2.png", refs[1], "image/png"))]
        fields = params["data"]
    else:
        assert url == "https://api.openai.com/v1/images/generations"
        fields = params["json"]
    assert fields["prompt"].startswith(prompt)
    assert fields["model"] == result.model == vertex_images.primary_image_generation_model() == "gpt-image-2"
    assert fields["size"] == f"{size[0]}x{size[1]}"
    assert result.data == output.getvalue() and result.usage == usage
    assert estimate_image_generation_cost_usd(result.model, result.usage)["cost_usd"] == 0.0484


def test_credit_error_does_not_resubmit_or_use_vertex(monkeypatch):
    monkeypatch.setenv("IMAGE_GENERATION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-project-key")
    response = MagicMock(status_code=429)
    response.json.return_value = {"error": {"code": "insufficient_quota", "message": "test-project-key"}}
    post = MagicMock()
    post.return_value.__enter__.return_value = response
    monkeypatch.setattr(openai_images.requests, "post", post)
    with pytest.raises(RuntimeError, match="insufficient_quota") as failure:
        vertex_images.generate_image_vertex_result("test")
    assert "test-project-key" not in str(failure.value)
    post.assert_called_once()
