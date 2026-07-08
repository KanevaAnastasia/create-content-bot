"""Optional Yandex Disk upload helpers for rendered artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger("bot.yandex_disk")

YANDEX_DISK_API_BASE_URL = "https://cloud-api.yandex.net/v1/disk"
YANDEX_DISK_TOKEN_ENV_NAMES = ("YANDEX_DISK_OAUTH_TOKEN", "YANDEX_DISK_TOKEN")
YANDEX_DISK_UPLOAD_ENABLED_ENV_NAME = "YANDEX_DISK_UPLOAD_ENABLED"
YANDEX_DISK_BASE_PATH_ENV_NAME = "YANDEX_DISK_BASE_PATH"
YANDEX_DISK_OVERWRITE_ENV_NAME = "YANDEX_DISK_OVERWRITE"
YANDEX_DISK_PUBLISH_ENV_NAME = "YANDEX_DISK_PUBLISH"
YANDEX_DISK_REQUIRED_ENV_NAME = "YANDEX_DISK_REQUIRED"
YANDEX_DISK_TIMEOUT_ENV_NAME = "YANDEX_DISK_TIMEOUT_SEC"
DEFAULT_YANDEX_DISK_BASE_PATH = "/Content Fabric/final builds"
DEFAULT_YANDEX_DISK_TIMEOUT_SEC = 120

_TRUTHY = {"1", "true", "yes", "y", "on", "да"}
_FALSY = {"0", "false", "no", "n", "off", "нет"}


@dataclass(frozen=True, slots=True)
class YandexDiskUploadResult:
    path: str
    resource_href: str
    web_url: str
    public_url: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "status": "uploaded",
            "path": self.path,
            "resource_href": self.resource_href,
            "web_url": self.web_url,
        }
        if self.public_url:
            payload["public_url"] = self.public_url
        return payload


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def yandex_disk_token() -> str:
    for name in YANDEX_DISK_TOKEN_ENV_NAMES:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def yandex_disk_upload_enabled() -> bool:
    if not yandex_disk_token():
        return False
    return _env_bool(YANDEX_DISK_UPLOAD_ENABLED_ENV_NAME, default=True)


def yandex_disk_upload_required() -> bool:
    return _env_bool(YANDEX_DISK_REQUIRED_ENV_NAME, default=False)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"OAuth {token}"}


def _normalize_remote_path(path: str) -> str:
    parts = [part.strip() for part in str(path or "").replace("\\", "/").split("/") if part.strip()]
    return "/" + "/".join(parts)


def _remote_file_path(*, bundle_key: str | None, filename: str) -> str:
    base = _normalize_remote_path(os.getenv(YANDEX_DISK_BASE_PATH_ENV_NAME) or DEFAULT_YANDEX_DISK_BASE_PATH)
    segments = [base.strip("/")]
    clean_bundle = str(bundle_key or "").replace("\\", "/").strip("/")
    if clean_bundle:
        segments.extend(part.strip() for part in clean_bundle.split("/") if part.strip())
    segments.append(Path(filename).name)
    return _normalize_remote_path("/".join(segments))


def _web_url(path: str) -> str:
    quoted = quote(path.strip("/"), safe="/")
    return f"https://disk.yandex.ru/client/disk/{quoted}"


def _resource_href(path: str) -> str:
    return f"{YANDEX_DISK_API_BASE_URL}/resources?path={quote(path, safe='')}"


def _mkdir_parents(*, token: str, remote_path: str, timeout_sec: int) -> None:
    parts = [part for part in remote_path.strip("/").split("/")[:-1] if part]
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else f"/{part}"
        resp = requests.put(
            f"{YANDEX_DISK_API_BASE_URL}/resources",
            params={"path": current},
            headers=_headers(token),
            timeout=timeout_sec,
        )
        if resp.status_code == 409:
            continue
        resp.raise_for_status()


def upload_file_to_yandex_disk(
    local_path: str | Path,
    *,
    bundle_key: str | None = None,
    filename: str | None = None,
) -> YandexDiskUploadResult | None:
    """Upload ``local_path`` when Yandex Disk is configured.

    Returns ``None`` when uploads are disabled or no token is configured.
    Raises request/file exceptions when upload is enabled and fails.
    """
    if not yandex_disk_upload_enabled():
        return None
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(f"Yandex Disk upload source is missing: {path}")

    token = yandex_disk_token()
    timeout_sec = _env_int(YANDEX_DISK_TIMEOUT_ENV_NAME, DEFAULT_YANDEX_DISK_TIMEOUT_SEC, minimum=10, maximum=3600)
    remote_path = _remote_file_path(bundle_key=bundle_key, filename=filename or path.name)
    overwrite = _env_bool(YANDEX_DISK_OVERWRITE_ENV_NAME, default=True)
    publish = _env_bool(YANDEX_DISK_PUBLISH_ENV_NAME, default=False)

    _mkdir_parents(token=token, remote_path=remote_path, timeout_sec=timeout_sec)
    link_resp = requests.get(
        f"{YANDEX_DISK_API_BASE_URL}/resources/upload",
        params={"path": remote_path, "overwrite": str(overwrite).lower()},
        headers=_headers(token),
        timeout=timeout_sec,
    )
    link_resp.raise_for_status()
    upload_href = str((link_resp.json() or {}).get("href") or "").strip()
    if not upload_href:
        raise RuntimeError("Yandex Disk upload link response did not include href")

    with path.open("rb") as fh:
        upload_resp = requests.put(upload_href, data=fh, timeout=timeout_sec)
    upload_resp.raise_for_status()

    public_url: str | None = None
    if publish:
        publish_resp = requests.put(
            f"{YANDEX_DISK_API_BASE_URL}/resources/publish",
            params={"path": remote_path},
            headers=_headers(token),
            timeout=timeout_sec,
        )
        if publish_resp.status_code != 409:
            publish_resp.raise_for_status()
        resource_resp = requests.get(
            f"{YANDEX_DISK_API_BASE_URL}/resources",
            params={"path": remote_path, "fields": "public_url"},
            headers=_headers(token),
            timeout=timeout_sec,
        )
        resource_resp.raise_for_status()
        public_url = str((resource_resp.json() or {}).get("public_url") or "").strip() or None

    logger.info("yandex_disk.uploaded local=%s remote=%s", path, remote_path)
    return YandexDiskUploadResult(
        path=remote_path,
        resource_href=_resource_href(remote_path),
        web_url=_web_url(remote_path),
        public_url=public_url,
    )
