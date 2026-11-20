"""Генерация изображений через Vertex AI SDK (vertexai)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .constants import VERTEX_IMAGE_GENERATE_TIMEOUT_SEC_ENV_NAME
from .llm_queue import _run_llm_call_with_deadline
from .llm_logging import get_llm_io_logger
from .media_file_store import image_mime_type
from .openai_images import OPENAI_IMAGE_MODEL, generate_openai_image

logger = logging.getLogger("bot.image")
_llm_io = get_llm_io_logger()
_IMAGE_RATE_LIMIT_MAX_ATTEMPTS = 3
_IMAGE_RATE_LIMIT_INITIAL_BACKOFF_SEC = 4.0
_IMAGE_RATE_LIMIT_MAX_BACKOFF_SEC = 20.0
IMAGE_GENERATION_PROFILE_ENV_NAME = "IMAGE_GENERATION_PROFILE"
IMAGE_GENERATION_PROFILE_CHEAP = "cheap"
IMAGE_GENERATION_PROFILE_BALANCED = "balanced"
IMAGE_GENERATION_PROFILE_BEST = "best"
IMAGE_GENERATION_PROFILE_LEGACY = "legacy"
LATEST_IMAGE_GENERATION_MODEL = "gemini-3-pro-image"
_PROFILE_MODELS: dict[str, list[str]] = {
    IMAGE_GENERATION_PROFILE_CHEAP: [LATEST_IMAGE_GENERATION_MODEL],
    IMAGE_GENERATION_PROFILE_BALANCED: [LATEST_IMAGE_GENERATION_MODEL],
    IMAGE_GENERATION_PROFILE_BEST: [LATEST_IMAGE_GENERATION_MODEL],
}
_DEFAULT_IMAGE_SIZE = "2K"
_DEFAULT_IMAGE_GENERATE_TIMEOUT_SEC = 480.0
_MIN_IMAGE_GENERATE_TIMEOUT_SEC = 60.0
_MAX_IMAGE_GENERATE_TIMEOUT_SEC = 900.0


@dataclass(frozen=True)
class ImageGenerationResult:
    data: bytes
    model: str
    request_id: str
    usage: dict | None = None


def resolve_image_generate_timeout_sec() -> float:
    """Return the image-specific Vertex deadline without inheriting the shorter text deadline."""
    raw = (os.getenv(VERTEX_IMAGE_GENERATE_TIMEOUT_SEC_ENV_NAME) or "").strip()
    if not raw:
        return _DEFAULT_IMAGE_GENERATE_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_IMAGE_GENERATE_TIMEOUT_SEC
    return max(_MIN_IMAGE_GENERATE_TIMEOUT_SEC, min(value, _MAX_IMAGE_GENERATE_TIMEOUT_SEC))


def _required_env(name: str) -> str:
    """
    Читает обязательную переменную окружения.

    Args:
        name: Имя env-переменной.
    Returns:
        Непустое строковое значение без внешних пробелов.
    Raises:
        RuntimeError: Если переменная отсутствует или пуста.
    """
    v = (os.getenv(name) or "").strip()
    if not v:
        raise RuntimeError(f"Missing environment variable: {name}")
    return v


def _enforce_vertical_9_16_prompt(prompt: str) -> str:
    """
    Нормализует image-prompt под требования пайплайна.

    Args:
        prompt: Исходный текстовый промпт.
    Returns:
        Промпт, где гарантированно:
        - есть требование вертикального кадра 9:16;
        - в конце присутствует фраза `все надписи на русском`.

    Логика:
        Если 9:16 уже указан, не дублируем; если фраза про русский уже есть, не добавляем второй раз.
    """
    p = (prompt or "").strip()
    suffix = " Vertical composition, portrait frame, aspect ratio 9:16."
    upright_guard = (
        " UPRIGHT PORTRAIT ORIENTATION IS MANDATORY: the top of the scene is at the top edge, "
        "the floor and characters' feet are toward the bottom edge, heads are above bodies, and the horizon is level. "
        "Compose all people and objects inside a tall portrait layout, using vertical stacking or depth when needed. "
        "Never rotate or tilt the artwork 90 degrees, never place a sideways landscape composition inside the portrait canvas."
    )
    russian_labels = "все надписи на русском"
    low = p.lower()
    base = p if ("9:16" in low or "aspect ratio" in low) else (f"{p}{suffix}" if p else "Vertical composition, portrait frame, aspect ratio 9:16.")
    if "upright portrait orientation is mandatory" not in base.lower():
        base = f"{base}{upright_guard}"
    if russian_labels in base.lower():
        return base
    return f"{base} {russian_labels}".strip()


def _enforce_aspect_ratio_prompt(prompt: str, aspect_ratio: str = "9:16") -> str:
    ratio = str(aspect_ratio or "9:16").strip() or "9:16"
    if ratio == "9:16":
        return _enforce_vertical_9_16_prompt(prompt)
    p = (prompt or "").strip()
    suffix = (
        " Horizontal composition, landscape frame, aspect ratio 16:9."
        if ratio == "16:9"
        else f" Composition aspect ratio {ratio}."
    )
    russian_labels = "все надписи на русском"
    low = p.lower()
    base = p if ratio in low else (f"{p}{suffix}" if p else suffix.strip())
    if russian_labels in base.lower():
        return base
    return f"{base} {russian_labels}".strip()


def _coerce_reference_images(
    reference_image: bytes | None,
    reference_images: list[bytes] | None,
) -> list[bytes]:
    """
    Приводит legacy/new входы референсов к единому списку bytes.

    Args:
        reference_image: Старый одиночный референс.
        reference_images: Новый список референсов.
    Returns:
        Список непустых бинарных изображений в порядке приоритета:
        `reference_images` (если задан), иначе `[reference_image]`.
    """
    if reference_images is not None:
        return [b for b in reference_images if b]
    if reference_image:
        return [reference_image]
    return []


def _response_excerpt(value: object, *, max_len: int = 2000) -> str:
    """Compact JSON/text excerpt for logs."""
    if isinstance(value, str):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = str(value)
    text = re.sub(r'data:\s*"(?:\\.|[^"\\])*"', 'data: "<binary omitted>"', text)
    text = re.sub(r'"data":"(?:\\.|[^"\\])*"', '"data":"<binary omitted>"', text)
    if len(text) > max_len:
        return text[:max_len] + "...[truncated]"
    return text


def _ref_images_summary(reference_images: list[bytes] | None) -> list[dict[str, object]]:
    """Build lightweight reference-images summary without dumping binary data."""
    out: list[dict[str, object]] = []
    for i, ref in enumerate(reference_images or [], start=1):
        out.append(
            {
                "index": i,
                "mimeType": image_mime_type(ref),
                "bytes": len(ref),
            }
        )
    return out


def _is_transient_image_rate_limit_error(e: BaseException) -> bool:
    """429/RESOURCE_EXHAUSTED для image-generation — есть смысл подождать и повторить."""
    msg = str(e).lower()
    return "resource_exhausted" in msg or "resource exhausted" in msg or "429" in msg


def _image_backoff_delay_before_retry(attempt_index: int) -> float:
    """Простой экспоненциальный backoff для image 429 без бесконечных задержек."""
    delay = _IMAGE_RATE_LIMIT_INITIAL_BACKOFF_SEC * (2 ** attempt_index)
    return min(_IMAGE_RATE_LIMIT_MAX_BACKOFF_SEC, delay)


def _image_size() -> str:
    """
    Requested output size for image generation.

    We cap the request at 2K: if someone configures 4K, the request is coerced
    back to 2K rather than resizing the returned image.
    """
    raw = (os.getenv("VERTEX_IMAGE_SIZE") or _DEFAULT_IMAGE_SIZE).strip().upper()
    if raw == "512":
        return "512"
    if raw in {"1K", "2K"}:
        return raw
    return _DEFAULT_IMAGE_SIZE


def _legacy_image_model_candidates() -> list[str]:
    primary = (os.getenv("VERTEX_IMAGE_MODEL") or LATEST_IMAGE_GENERATION_MODEL).strip()
    fallbacks_raw = os.getenv("VERTEX_IMAGE_MODEL_FALLBACKS") or ""
    out: list[str] = []
    for item in [primary or LATEST_IMAGE_GENERATION_MODEL, *fallbacks_raw.split(",")]:
        model = str(item or "").strip()
        if model and model not in out:
            out.append(model)
    return out or [LATEST_IMAGE_GENERATION_MODEL]


def primary_image_generation_model(profile: str | None = None) -> str:
    models = image_generation_profile_models(profile)
    return models[0] if models else LATEST_IMAGE_GENERATION_MODEL


def _image_provider() -> str:
    provider = (os.getenv("IMAGE_GENERATION_PROVIDER") or "vertex").strip().lower()
    if provider not in {"vertex", "openai"}:
        raise ValueError("IMAGE_GENERATION_PROVIDER must be vertex or openai")
    return provider


def image_generation_profile_models(profile: str | None = None) -> list[str]:
    """
    Возвращает порядок моделей для генерации изображений.

    The first model is the default strongest attempt. Remaining models are
    available only for explicit user-approved downgrade, not for silent
    automatic fallback.
    """
    if _image_provider() == "openai":
        return [OPENAI_IMAGE_MODEL]
    raw = (profile or os.getenv(IMAGE_GENERATION_PROFILE_ENV_NAME) or IMAGE_GENERATION_PROFILE_LEGACY).strip().lower()
    if raw in {"", IMAGE_GENERATION_PROFILE_LEGACY, "env", "custom"}:
        return _legacy_image_model_candidates()
    return list(_PROFILE_MODELS.get(raw) or [LATEST_IMAGE_GENERATION_MODEL])


def next_image_generation_model(current_model: str | None, profile: str | None = None) -> str | None:
    """Return the next lower configured image model after ``current_model``."""
    models = image_generation_profile_models(profile)
    if not models:
        return None
    current = str(current_model or "").strip()
    if not current:
        return models[1] if len(models) > 1 else None
    try:
        idx = models.index(current)
    except ValueError:
        return models[1] if len(models) > 1 else None
    next_idx = idx + 1
    if next_idx < len(models):
        return models[next_idx]
    return None


def _generate_image_vertex_sdk(
    prompt: str,
    reference_images: list[bytes] | None = None,
    *,
    model: str | None = None,
    request_id: str = "-",
    aspect_ratio: str = "9:16",
) -> bytes:
    """Генерирует изображение через Vertex AI SDK `ImageGenerationModel.from_pretrained`."""
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (
        os.getenv("VERTEX_IMAGE_LOCATION")
        or os.getenv("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    ).strip() or "us-central1"
    eff_model = (model or "").strip() or (os.getenv("VERTEX_IMAGE_MODEL") or LATEST_IMAGE_GENERATION_MODEL).strip()
    eff_aspect_ratio = str(aspect_ratio or "9:16").strip() or "9:16"
    if not project:
        raise RuntimeError("Missing environment variable: GOOGLE_CLOUD_PROJECT")
    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import (  # type: ignore
            GenerativeModel,
            GenerationConfig,
            HarmBlockThreshold,
            HarmCategory,
            Part,
            SafetySetting,
        )
        from vertexai.preview.vision_models import ImageGenerationModel, RawReferenceImage  # type: ignore
    except Exception as e:
        raise RuntimeError("Missing dependency. Install with: pip install google-cloud-aiplatform") from e

    # Используем только Vertex transport (Google Cloud billing), без прямого API key.
    vertexai.init(project=project, location=location)
    is_imagen_model = eff_model.lower().startswith("imagen")
    logger.info(
        "image.sdk.request request_id=%s project=%s location=%s model=%s prompt=%s refs=%s",
        request_id,
        project,
        location,
        eff_model,
        prompt,
        _response_excerpt(_ref_images_summary(reference_images), max_len=1200),
    )
    _llm_io.info(
        "[image.sdk] request_id=%s model=%s prompt=%s refs=%s",
        request_id,
        eff_model,
        prompt,
        _response_excerpt(_ref_images_summary(reference_images), max_len=8000),
    )
    if is_imagen_model:
        model_client = ImageGenerationModel.from_pretrained(eff_model)
        refs = [RawReferenceImage(reference_id=i, image=ref) for i, ref in enumerate(reference_images or [], start=1)]
        base_kwargs = {
            "prompt": _enforce_aspect_ratio_prompt(prompt, eff_aspect_ratio),
            "number_of_images": 1,
            "aspect_ratio": eff_aspect_ratio,
            "sample_image_size": _image_size(),
            "person_generation": "allow_all",
        }
        if refs:
            try:
                response = model_client.generate_images(
                    **base_kwargs,
                    reference_images=refs,
                )
            except TypeError as e:
                if "unexpected keyword argument 'reference_images'" not in str(e):
                    raise
                logger.warning(
                    "image.sdk.model_rejects_refs request_id=%s model=%s err=%s",
                    request_id,
                    eff_model,
                    str(e),
                )
                raise RuntimeError(
                    f"Image model {eff_model} does not support reference_images in this SDK; "
                    "refusing to generate without character references."
                ) from e
        else:
            response = model_client.generate_images(**base_kwargs)
    else:
        model_client = GenerativeModel(eff_model)
        parts = [
            Part.from_text(
                "Generate exactly one image from the following prompt. "
                "Return an image, not a text-only description.\n\n"
                f"{_enforce_aspect_ratio_prompt(prompt, eff_aspect_ratio)}"
            )
        ]
        for ref in reference_images or []:
            parts.append(Part.from_data(data=ref, mime_type=image_mime_type(ref)))
        # The SDK constructor omits image_config, but its wire schema supports it.
        generation_config = GenerationConfig.from_dict({
            "response_modalities": ["IMAGE"],
            "image_config": {"aspect_ratio": eff_aspect_ratio},
        })
        safety_settings = [
            SafetySetting(
                category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
            ),
            SafetySetting(
                category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
            ),
            SafetySetting(
                category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
            ),
            SafetySetting(
                category=HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
            ),
        ]
        response = model_client.generate_content(
            parts,
            generation_config=generation_config,
            safety_settings=safety_settings,
        )
    logger.info(
        "image.sdk.response request_id=%s model=%s body=%s",
        request_id,
        eff_model,
        _response_excerpt(response),
    )
    _llm_io.info(
        "[image.sdk] request_id=%s model=%s\n---------- RESPONSE ----------\n%s\n---------- END RESPONSE ----------",
        request_id,
        eff_model,
        _response_excerpt(response, max_len=8000),
    )
    if is_imagen_model:
        images = getattr(response, "images", None) or []
        if not images:
            raise RuntimeError("Vertex image generation failed: ImageGenerationModel returned no images.")
        first = images[0]
        data = getattr(first, "_image_bytes", None)
        if isinstance(data, (bytes, bytearray)) and data:
            logger.info("image.sdk.result request_id=%s model=%s bytes=%s", request_id, eff_model, len(data))
            return bytes(data)
    else:
        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            cparts = getattr(content, "parts", None) or []
            for part in cparts:
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None)
                if isinstance(data, (bytes, bytearray)) and data:
                    logger.info("image.sdk.result request_id=%s model=%s bytes=%s", request_id, eff_model, len(data))
                    return bytes(data)
    raise RuntimeError("Vertex image generation failed: generated image has no binary payload.")


def generate_image_vertex_result(
    prompt: str,
    reference_image: bytes | None = None,
    *,
    reference_images: list[bytes] | None = None,
    request_id: str | None = None,
    model: str | None = None,
    model_candidates: list[str] | None = None,
    aspect_ratio: str = "9:16",
) -> ImageGenerationResult:
    """
    Главная точка генерации изображения через Vertex AI SDK.

    Args:
        prompt: Текстовый промпт.
        reference_image: Legacy одиночный референс.
        reference_images: Список референсов (предпочтительный формат).
    Returns:
        Байты, имя модели и request_id для первой успешно сработавшей модели.

    Логика:
        1) Uses exactly one selected image model. By default this is
           `VERTEX_IMAGE_MODEL` (default: `gemini-3-pro-image`).
        2) Делает вызов выбранной модели через Vertex SDK.
        3) На 429/RESOURCE_EXHAUSTED делает retry с backoff на той же модели.
        4) Любая финальная ошибка возвращается наверх; на старые модели
           автоматически не откатываемся.
    """
    refs_list = _coerce_reference_images(reference_image, reference_images)
    rid = request_id or uuid.uuid4().hex[:12]
    eff_aspect_ratio = str(aspect_ratio or "9:16").strip() or "9:16"
    if _image_provider() == "openai":
        # Older saved scenes can carry a Vertex model selection.
        data, usage = generate_openai_image(
            _enforce_aspect_ratio_prompt(prompt, eff_aspect_ratio), refs_list,
            model=OPENAI_IMAGE_MODEL, aspect_ratio=eff_aspect_ratio, request_id=rid,
            timeout=resolve_image_generate_timeout_sec(),
        )
        return ImageGenerationResult(data, OPENAI_IMAGE_MODEL, rid, usage)
    explicit_model = str(model or "").strip()
    if explicit_model:
        selected_model = explicit_model
    elif model_candidates:
        selected_model = next((str(m or "").strip() for m in model_candidates if str(m or "").strip()), "")
    else:
        selected_model = primary_image_generation_model()
    models = [selected_model] if selected_model else []

    last_error: Exception | None = None
    for model in models:
        for attempt in range(_IMAGE_RATE_LIMIT_MAX_ATTEMPTS):
            logger.info(
                "image.generate.attempt request_id=%s model=%s try=%s/%s prompt=%s refs=%s",
                rid,
                model,
                attempt + 1,
                _IMAGE_RATE_LIMIT_MAX_ATTEMPTS,
                prompt,
                _response_excerpt(_ref_images_summary(refs_list), max_len=1200),
            )
            try:
                timeout_sec = resolve_image_generate_timeout_sec()
                def _sdk_call() -> bytes:
                    kwargs = {
                        "model": model,
                        "request_id": rid,
                    }
                    if eff_aspect_ratio != "9:16":
                        kwargs["aspect_ratio"] = eff_aspect_ratio
                    return _generate_image_vertex_sdk(
                        prompt,
                        refs_list or None,
                        **kwargs,
                    )

                data = _run_llm_call_with_deadline(
                    _sdk_call,
                    timeout_sec,
                )
                return ImageGenerationResult(data=data, model=model, request_id=rid)
            except Exception as e:
                last_error = e
                logger.warning("image.generate.failed request_id=%s model=%s error=%s", rid, model, str(e))
                if _is_transient_image_rate_limit_error(e) and attempt < _IMAGE_RATE_LIMIT_MAX_ATTEMPTS - 1:
                    wait_sec = _image_backoff_delay_before_retry(attempt)
                    logger.warning(
                        "image.generate.rate_limit request_id=%s model=%s wait=%.1fs before retry",
                        rid,
                        model,
                        wait_sec,
                    )
                    time.sleep(wait_sec)
                    continue
                break

    if last_error is not None:
        raise RuntimeError(f"Vertex image generation failed on model {models[0]}: {last_error}") from last_error
    raise RuntimeError("Vertex image generation failed: no image model configured.")


def generate_image_vertex(
    prompt: str,
    reference_image: bytes | None = None,
    *,
    reference_images: list[bytes] | None = None,
    request_id: str | None = None,
    model: str | None = None,
    model_candidates: list[str] | None = None,
    aspect_ratio: str = "9:16",
) -> bytes:
    """
    Главная точка генерации изображения через Vertex AI SDK.

    Возвращает только байты для совместимости. Если нужно знать фактически
    использованную модель, вызывайте `generate_image_vertex_result`.
    """
    return generate_image_vertex_result(
        prompt,
        reference_image,
        reference_images=reference_images,
        request_id=request_id,
        model=model,
        model_candidates=model_candidates,
        aspect_ratio=aspect_ratio,
    ).data


def generate_image_vertex_to_file(
    prompt: str,
    output_path: str,
    reference_image: bytes | None = None,
    *,
    reference_images: list[bytes] | None = None,
    request_id: str | None = None,
) -> str:
    """Генерирует изображение и сохраняет в файл."""
    data = generate_image_vertex(
        prompt,
        reference_image,
        reference_images=reference_images,
        request_id=request_id,
    )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    logger.info("image.save path=%s bytes=%s", str(out), len(data))
    return str(out)
