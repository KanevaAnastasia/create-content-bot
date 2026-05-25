"""
Video generation via Vertex PredictLongRunning API (LRO).

Implementation notes:
- Veo generation is long-running; request is sent to `predictLongRunning`.
- Operation polling is done via official Vertex SDK operation client.
- Auth uses ADC/Bearer (no API key query parameters).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any

_VIDEO_RESULT_META = threading.local()
logger = logging.getLogger("bot.video")


def _read_file_bytes(path: str) -> bytes:
    """Read bytes from file and ensure non-empty content."""
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        raise RuntimeError(f"Input file is empty: {path}")
    return data


def _encode_b64(data: bytes) -> str:
    """Encode bytes to base64 string for JSON payload."""
    return base64.b64encode(data).decode("utf-8")


def _normalize_quality(quality: str) -> str:
    """Normalize quality to Vertex-compatible token."""
    q = (quality or "").strip().lower()
    if q in {"1080", "1080p"}:
        return "1080p"
    if q in {"720", "720p"}:
        return "720p"
    return "1080p"


def _bool_from_text(value: str) -> bool:
    """Parse true/false from CLI string."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _guess_image_mime_type(data: bytes) -> str:
    """Detect image MIME type by magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    return "image/jpeg"


def _required_env(name: str) -> str:
    """Read required env var and raise explicit error if missing."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env variable: {name}")
    return value


def _resolve_model(model: str) -> str:
    """Resolve user alias to concrete model id."""
    m = (model or "").strip()
    if not m:
        return "veo-3.1-fast-generate-001"
    if m == "veo3_fast":
        raw = (os.getenv("VERTEX_VIDEO_MODEL_VEO3_FAST") or "").strip()
        first = raw.split(",")[0].strip() if raw else ""
        return first or "veo-3.1-fast-generate-001"
    return m


def _resolve_video_location(location: str | None = None) -> str:
    """Resolve Vertex Video region, avoiding Gemini's global location."""
    raw = (
        location
        or os.getenv("VERTEX_VIDEO_LOCATION")
        or os.getenv("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    )
    resolved = (raw or "").strip() or "us-central1"
    if resolved.lower() == "global":
        return "us-central1"
    return resolved


def _build_predict_payload(
    *,
    prompt: str,
    start_image: bytes,
    end_image: bytes | None,
    duration_sec: int,
    quality: str,
    aspect_ratio: str,
    audio_enabled: bool,
) -> dict[str, Any]:
    """Build Vertex predictLongRunning payload for Veo."""
    instance: dict[str, Any] = {
        "prompt": prompt,
        "image": {
            "bytesBase64Encoded": _encode_b64(start_image),
            "mimeType": _guess_image_mime_type(start_image),
        },
    }
    if end_image:
        instance["lastFrame"] = {
            "bytesBase64Encoded": _encode_b64(end_image),
            "mimeType": _guess_image_mime_type(end_image),
        }
    safety_settings = _resolve_video_safety_settings()
    return {
        "instances": [instance],
        "parameters": {
            "sampleCount": 1,
            "durationSeconds": int(duration_sec),
            "aspectRatio": aspect_ratio,
            "resolution": _normalize_quality(quality),
            "generateAudio": bool(audio_enabled),
            "safety_settings": safety_settings,
        },
    }


def _default_video_safety_settings() -> list[dict[str, str]]:
    """Default relaxed safety profile for Veo predictLongRunning."""
    return [
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
    ]


def _resolve_video_safety_settings() -> list[dict[str, str]]:
    """
    Resolve Veo safety settings from env with safe defaults.

    Env override:
      VERTEX_VIDEO_SAFETY_SETTINGS_JSON='[{"category":"...","threshold":"..."}]'
    """
    raw = (os.getenv("VERTEX_VIDEO_SAFETY_SETTINGS_JSON") or "").strip()
    if not raw:
        return _default_video_safety_settings()
    try:
        parsed = json.loads(raw)
    except Exception as e:
        raise RuntimeError("Invalid JSON in VERTEX_VIDEO_SAFETY_SETTINGS_JSON") from e
    if not isinstance(parsed, list):
        raise RuntimeError("VERTEX_VIDEO_SAFETY_SETTINGS_JSON must be a JSON array.")
    out: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        threshold = str(item.get("threshold") or "").strip()
        if category and threshold:
            out.append({"category": category, "threshold": threshold})
    return out or _default_video_safety_settings()


def _build_predict_url(*, project: str, location: str, model: str) -> str:
    """Build predictLongRunning URL for publisher model."""
    path = f"projects/{project}/locations/{location}/publishers/google/models/{model}:predictLongRunning"
    return f"https://{location}-aiplatform.googleapis.com/v1/{path}"


def _build_fetch_operation_url(*, project: str, location: str, model: str) -> str:
    """Build fetchPredictOperation URL for publisher model endpoint."""
    path = f"projects/{project}/locations/{location}/publishers/google/models/{model}:fetchPredictOperation"
    return f"https://{location}-aiplatform.googleapis.com/v1/{path}"


def _to_compact_json(value: Any) -> str:
    """Best-effort compact JSON for readable diagnostics."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def _post_predict_long_running(*, url: str, payload: dict[str, Any], request_id: str = "-") -> dict[str, Any]:
    """Send predictLongRunning request with ADC credentials."""
    try:
        import google.auth  # type: ignore
        from google.auth.transport.requests import AuthorizedSession  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Missing google-auth runtime for ADC. Install with: "
            f"'{sys.executable} -m pip install google-auth requests'."
        ) from e

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)
    logger.info(
        "video.predict_long_running.request request_id=%s url=%s payload=%s",
        request_id,
        url,
        _response_excerpt(payload, max_len=2000),
    )
    resp = session.post(url, json=payload, timeout=180)
    text = resp.text
    logger.info(
        "video.predict_long_running.response request_id=%s status=%s body=%s",
        request_id,
        resp.status_code,
        text[:2000],
    )
    if resp.status_code >= 400:
        logger.error(
            "video.predict_long_running.error request_id=%s status=%s body=%s",
            request_id,
            resp.status_code,
            text[:3000],
        )
        raise RuntimeError(f"HTTP {resp.status_code}: {text}")
    try:
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError(f"Server returned invalid JSON: {text[:1000]}") from e
    if not isinstance(obj, dict):
        raise RuntimeError("Server returned invalid JSON object.")
    return obj


def _poll_operation(
    *,
    operation_name: str,
    project: str,
    location: str,
    model: str,
    timeout_sec: int = 900,
    request_id: str = "-",
) -> dict[str, Any]:
    """Poll LRO operation via fetchPredictOperation API."""
    try:
        import google.auth  # type: ignore
        from google.auth.transport.requests import AuthorizedSession  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Missing dependency: google-cloud-aiplatform. Install with: "
            f"'{sys.executable} -m pip install google-cloud-aiplatform'."
        ) from e

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)
    fetch_url = _build_fetch_operation_url(project=project, location=location, model=model)

    deadline = time.time() + timeout_sec
    while True:
        resp = session.post(fetch_url, json={"operationName": operation_name}, timeout=120)
        logger.info(
            "video.fetch_predict_operation.response request_id=%s status=%s operation=%s body=%s",
            request_id,
            resp.status_code,
            operation_name,
            resp.text[:2000],
        )
        if resp.status_code >= 400:
            logger.error(
                "video.fetch_predict_operation.error request_id=%s status=%s operation=%s body=%s",
                request_id,
                resp.status_code,
                operation_name,
                resp.text[:3000],
            )
            raise RuntimeError(f"Failed to poll Vertex operation. HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Invalid JSON while polling operation: {resp.text[:500]}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected JSON type while polling operation: {type(data).__name__}")

        if data.get("done") is True:
            logger.info("video.operation.done request_id=%s operation=%s", request_id, operation_name)
            err = data.get("error")
            if isinstance(err, dict) and err:
                logger.error(
                    "video.operation.failed request_id=%s operation=%s error=%s",
                    request_id,
                    operation_name,
                    _to_compact_json(err),
                )
                raise RuntimeError(f"Vertex operation failed: {_to_compact_json(err)}")
            response = data.get("response")
            if not isinstance(response, dict):
                raise RuntimeError("Vertex operation completed without response payload.")
            logger.info(
                "video.operation.response request_id=%s operation=%s response=%s",
                request_id,
                operation_name,
                _response_excerpt(response, max_len=3000),
            )
            return response
        if time.time() >= deadline:
            raise RuntimeError("Vertex video generation timed out while waiting for operation completion.")
        time.sleep(3)


def _iter_values(obj: Any):
    """Recursively yield all leaf values from dict/list tree."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_values(v)
    else:
        yield obj


def _decode_possible_base64(data: str) -> bytes | None:
    """Decode base64 payload robustly (standard + urlsafe + padding fix)."""
    s = (data or "").strip()
    if len(s) < 16:
        return None
    if s.startswith("data:") and ";base64," in s:
        s = s.split(";base64,", 1)[1]
    # Remove occasional whitespace/newlines in large encoded payloads.
    s = "".join(s.split())
    pad = (-len(s)) % 4
    s_padded = s + ("=" * pad)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(s_padded)
        except Exception:
            continue
        if raw and (b"ftyp" in raw[:4096] or b"moov" in raw[:4096]):
            return raw
    return None


def _extract_video_bytes_from_response(resp: Any) -> bytes:
    """Extract MP4 bytes from completed operation response."""
    # Fast-path for common shapes from GenerateVideoResponse.
    if isinstance(resp, dict):
        vids = resp.get("videos")
        if isinstance(vids, list):
            for item in vids:
                if not isinstance(item, dict):
                    continue
                for key in ("bytesBase64Encoded", "videoBytesBase64Encoded", "bytes_base64_encoded"):
                    candidate = item.get(key)
                    if isinstance(candidate, str):
                        raw = _decode_possible_base64(candidate)
                        if raw:
                            _VIDEO_RESULT_META.source = "inline"
                            logger.info("video.extract.success source=inline fast_path=true bytes=%s", len(raw))
                            return raw

    for val in _iter_values(resp):
        if not isinstance(val, str) or len(val) < 16:
            continue
        raw = _decode_possible_base64(val)
        if raw:
            _VIDEO_RESULT_META.source = "inline"
            logger.info("video.extract.success source=inline fast_path=false bytes=%s", len(raw))
            return raw
    video_uri = _extract_video_uri_from_response(resp)
    if video_uri:
        out = _download_video_from_uri(video_uri)
        _VIDEO_RESULT_META.source = "uri"
        _VIDEO_RESULT_META.uri = video_uri
        logger.info("video.extract.success source=uri uri=%s bytes=%s", video_uri, len(out))
        return out
    _raise_detailed_video_error(resp)
    excerpt = _response_excerpt(resp, max_len=1200)
    logger.error("video.extract.failed reason=no_inline_bytes excerpt=%s", excerpt)
    raise RuntimeError(f"Video response does not contain inline MP4 bytes. Response excerpt: {excerpt}")


def _extract_video_uri_from_response(resp: Any) -> str | None:
    """Find downloadable video URI (e.g. gs://...) in response tree."""
    for val in _iter_values(resp):
        if not isinstance(val, str):
            continue
        s = val.strip()
        if s.startswith("gs://") or "gs://" in s:
            idx = s.find("gs://")
            return s[idx:]
        if s.startswith("https://") and (
            ".mp4" in s.lower()
            or "videostore" in s.lower()
            or "storage.googleapis.com" in s.lower()
            or "googleusercontent.com" in s.lower()
        ):
            return s
    return None


def _download_video_from_uri(uri: str) -> bytes:
    """Download MP4 bytes from supported URI formats."""
    if uri.startswith("gs://"):
        try:
            from google.cloud import storage  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Video returned as gs:// URI, but google-cloud-storage is missing. "
                f"Install with: '{sys.executable} -m pip install google-cloud-storage'."
            ) from e
        path = uri[len("gs://") :]
        if "/" not in path:
            raise RuntimeError(f"Invalid gs:// URI returned by provider: {uri}")
        bucket_name, blob_name = path.split("/", 1)
        client = storage.Client()
        data = client.bucket(bucket_name).blob(blob_name).download_as_bytes()
        if not data:
            raise RuntimeError(f"Downloaded empty video from URI: {uri}")
        logger.info("video.download.success uri=%s bytes=%s", uri, len(data))
        return data
    raise RuntimeError(f"Unsupported video URI format in response: {uri}")


def get_last_video_result_source() -> str | None:
    """Return source marker for last generated video in current thread."""
    return getattr(_VIDEO_RESULT_META, "source", None)


def get_last_video_result_uri() -> str | None:
    """Return source URI for last generated video in current thread, if any."""
    return getattr(_VIDEO_RESULT_META, "uri", None)


def _raise_detailed_video_error(resp: Any) -> None:
    """Raise detailed moderation/provider diagnostics when no MP4 found."""
    stack = [resp]
    safety_reasons: list[str] = []
    provider_errors: list[str] = []
    finish_reasons: list[str] = []
    blocked_reasons: list[str] = []
    safety_ratings: list[str] = []

    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            finish_reason = str(cur.get("finish_reason", "")).strip().upper()
            if finish_reason:
                finish_reasons.append(finish_reason)
            if finish_reason == "SAFETY":
                safety_reasons.append("finish_reason=SAFETY")
            err = cur.get("error")
            if err:
                provider_errors.append(_to_compact_json(err))
            blocked_reason = str(cur.get("blocked_reason", "")).strip()
            if blocked_reason:
                blocked_reasons.append(blocked_reason)
            safety = cur.get("safety_ratings")
            if safety:
                safety_ratings.append(_to_compact_json(safety))
            stack.extend(cur.values())
            continue
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if hasattr(cur, "__dict__"):
            stack.append(vars(cur))

    if safety_reasons or provider_errors or blocked_reasons or safety_ratings or finish_reasons:
        parts: list[str] = ["Video generation did not return MP4 bytes."]
        if safety_reasons:
            parts.append("Safety: " + "; ".join(sorted(set(safety_reasons))))
        if finish_reasons:
            parts.append("Finish reasons: " + ", ".join(sorted(set(finish_reasons))))
        if blocked_reasons:
            parts.append("Blocked reasons: " + "; ".join(sorted(set(blocked_reasons))))
        if safety_ratings:
            parts.append("Safety ratings: " + " | ".join(safety_ratings[:2]))
        if provider_errors:
            parts.append("Provider error: " + " | ".join(provider_errors[:2]))
        raise RuntimeError(" ".join(parts))


def _response_excerpt(resp: Any, *, max_len: int = 1200) -> str:
    """Compact response excerpt for easier debugging in production logs."""
    try:
        text = json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(resp)
    if len(text) > max_len:
        return text[:max_len] + "...[truncated]"
    return text


def generate_video_via_vertex(
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    start_image: bytes,
    end_image: bytes | None = None,
    quality: str = "1080",
    aspect_ratio: str = "9:16",
    audio_enabled: bool = True,
    location: str | None = None,
) -> bytes:
    """Generate MP4 through Vertex predictLongRunning Veo endpoint."""
    request_id = uuid.uuid4().hex[:12]
    project = _required_env("GOOGLE_CLOUD_PROJECT")
    location = _resolve_video_location(location)
    model_id = _resolve_model(model)
    logger.info(
        "video.generate.start request_id=%s project=%s location=%s model=%s duration=%s quality=%s aspect=%s audio=%s",
        request_id,
        project,
        location,
        model_id,
        duration_sec,
        quality,
        aspect_ratio,
        audio_enabled,
    )

    try:
        import vertexai  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Missing dependency: google-cloud-aiplatform. "
            f"Current python: {sys.executable}. "
            f"Install into this interpreter with: '{sys.executable} -m pip install google-cloud-aiplatform'."
        ) from e

    # Keep region/project aligned with request URLs and operation polling endpoint.
    vertexai.init(project=project, location=location)

    payload = _build_predict_payload(
        prompt=prompt,
        start_image=start_image,
        end_image=end_image,
        duration_sec=int(duration_sec),
        quality=quality,
        aspect_ratio=aspect_ratio,
        audio_enabled=bool(audio_enabled),
    )

    predict_url = _build_predict_url(project=project, location=location, model=model_id)
    predict_resp = _post_predict_long_running(url=predict_url, payload=payload)
    op_name = str(predict_resp.get("name", "")).strip()
    if not op_name:
        raise RuntimeError(f"Vertex did not return operation name. Response: {_to_compact_json(predict_resp)}")

    final_response = _poll_operation(
        operation_name=op_name,
        project=project,
        location=location,
        model=model_id,
    )
    result = _extract_video_bytes_from_response(final_response)
    logger.info(
        "video.generate.done request_id=%s bytes=%s source=%s",
        request_id,
        len(result),
        get_last_video_result_source(),
    )
    return result


def main() -> int:
    """CLI entrypoint for manual checks."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    parser = argparse.ArgumentParser(description="Generate video via Vertex PredictLongRunning API.")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--start", required=True, help="Path to start frame PNG/JPG")
    parser.add_argument("--end", default="", help="Path to optional end frame PNG/JPG")
    parser.add_argument("--model", default="veo-3.1-fast-generate-001", help="Model ID")
    parser.add_argument("--duration", required=True, type=int, help="Duration in seconds")
    parser.add_argument("--quality", default="1080")
    parser.add_argument("--aspect", dest="aspect_ratio", default="9:16")
    parser.add_argument("--audio", default="true", help="true/false")
    parser.add_argument("--out", required=True, help="Path to resulting MP4")
    args = parser.parse_args()

    start_image = _read_file_bytes(args.start)
    end_image = _read_file_bytes(args.end) if args.end else None
    video_bytes = generate_video_via_vertex(
        prompt=args.prompt,
        duration_sec=int(args.duration),
        start_image=start_image,
        end_image=end_image,
        model=args.model,
        quality=args.quality,
        aspect_ratio=args.aspect_ratio,
        audio_enabled=_bool_from_text(args.audio),
    )

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(video_bytes)
    if not os.path.exists(args.out) or os.path.getsize(args.out) == 0:
        raise RuntimeError("Generated output file is empty.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
