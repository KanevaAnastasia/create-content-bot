"""Generate a scene or edit its ordered reference images with the OpenAI Images API."""

from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
from fractions import Fraction

import requests
from PIL import Image

from .media_file_store import image_file_extension, image_mime_type

logger = logging.getLogger("bot.image")
OPENAI_IMAGE_MODEL = "gpt-image-2"


def _output_size(aspect_ratio: str) -> str:
    ratio = Fraction(aspect_ratio.replace(":", "/"))
    if not Fraction(1, 3) <= ratio <= 3:
        raise ValueError(f"OpenAI Images does not support aspect ratio {aspect_ratio}")
    if ratio == 1:
        return "1024x1024"
    width = round(min(1, ratio) * 96) * 16
    height = round(min(1, 1 / ratio) * 96) * 16
    return f"{width}x{height}"


def _reference_files(references: list[bytes]) -> list:
    if len(references) > 16:
        raise ValueError("OpenAI Images accepts up to 16 reference images per scene")
    files = []
    for index, data in enumerate(references, start=1):
        if image_mime_type(data) == "image/gif":
            buffer = io.BytesIO()
            with Image.open(io.BytesIO(data)) as source:
                source.convert("RGBA").save(buffer, format="PNG")
            data = buffer.getvalue()
        name = f"reference-{index}.{image_file_extension(data)}"
        files.append(("image[]", (name, data, image_mime_type(data))))
    return files


def generate_openai_image(
    prompt: str, references: list[bytes], *, model: str, aspect_ratio: str, request_id: str, timeout: float,
) -> tuple[bytes, dict]:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("Missing environment variable: OPENAI_API_KEY")
    size = _output_size(aspect_ratio)
    fields = {"model": model, "prompt": prompt, "size": size, "quality": "medium", "output_format": "png", "n": 1}
    endpoint = "edits" if references else "generations"
    payload = {"data": fields, "files": _reference_files(references)} if references else {"json": fields}
    logger.info("image.openai.request request_id=%s model=%s endpoint=%s size=%s refs=%d",
                request_id, model, endpoint, size, len(references))
    with requests.post(
        f"https://api.openai.com/v1/images/{endpoint}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=(15, timeout),
        allow_redirects=False,
        **payload,
    ) as response:
        if response.status_code != 200:
            try:
                code = response.json()["error"]["code"]
            except (KeyError, ValueError, TypeError):
                code = None
            reason = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,70}", code) else "request_failed"
            raise RuntimeError(f"OpenAI Images HTTP {response.status_code} ({reason})")
        result = response.json()
    try:
        data = base64.b64decode(result["data"][0]["b64_json"], validate=True)
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or f"{image.width}x{image.height}" != size:
                raise ValueError("unexpected format or dimensions")
            image.verify()
    except (KeyError, IndexError, TypeError, ValueError, OSError, binascii.Error) as exc:
        raise RuntimeError("OpenAI Images returned an invalid or incomplete image") from exc
    usage = result.get("usage") or {}
    logger.info("image.openai.result request_id=%s model=%s size=%s bytes=%d tokens=%s",
                request_id, model, size, len(data), usage.get("total_tokens"))
    return data, usage
