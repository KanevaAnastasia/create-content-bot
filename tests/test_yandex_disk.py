from __future__ import annotations

from pathlib import Path

from app import yandex_disk


class _Response:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def test_upload_file_to_yandex_disk_skips_without_token(tmp_path, monkeypatch):
    source = tmp_path / "final.mp4"
    source.write_bytes(b"video")
    monkeypatch.delenv("YANDEX_DISK_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("YANDEX_DISK_TOKEN", raising=False)
    monkeypatch.setattr(yandex_disk.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(yandex_disk.requests, "put", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))

    assert yandex_disk.upload_file_to_yandex_disk(source, bundle_key="bundle") is None


def test_upload_file_to_yandex_disk_uploads_and_publishes(tmp_path, monkeypatch):
    source = tmp_path / "final.mp4"
    source.write_bytes(b"video")
    monkeypatch.setenv("YANDEX_DISK_OAUTH_TOKEN", "token-1")
    monkeypatch.setenv("YANDEX_DISK_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("YANDEX_DISK_BASE_PATH", "/Root/finals")
    monkeypatch.setenv("YANDEX_DISK_OVERWRITE", "1")
    monkeypatch.setenv("YANDEX_DISK_PUBLISH", "1")

    get_calls: list[dict] = []
    put_calls: list[dict] = []

    def fake_get(url, **kwargs):
        get_calls.append({"url": url, **kwargs})
        if url.endswith("/resources/upload"):
            return _Response(payload={"href": "https://upload.example/final"})
        if url.endswith("/resources"):
            return _Response(payload={"public_url": "https://disk.yandex.ru/i/public"})
        raise AssertionError(f"unexpected GET {url}")

    def fake_put(url, **kwargs):
        put_calls.append({"url": url, **kwargs})
        if url == "https://upload.example/final":
            assert kwargs["data"].read() == b"video"
            return _Response(status_code=201)
        return _Response(status_code=201)

    monkeypatch.setattr(yandex_disk.requests, "get", fake_get)
    monkeypatch.setattr(yandex_disk.requests, "put", fake_put)

    result = yandex_disk.upload_file_to_yandex_disk(source, bundle_key="bundle-1")

    assert result is not None
    assert result.path == "/Root/finals/bundle-1/final.mp4"
    assert result.public_url == "https://disk.yandex.ru/i/public"
    assert get_calls[0]["params"] == {"path": "/Root/finals/bundle-1/final.mp4", "overwrite": "true"}
    mkdir_paths = [call["params"]["path"] for call in put_calls if call["url"].endswith("/resources")]
    assert mkdir_paths == ["/Root", "/Root/finals", "/Root/finals/bundle-1"]


def test_upload_file_to_yandex_disk_uses_custom_filename(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    monkeypatch.setenv("YANDEX_DISK_OAUTH_TOKEN", "token-1")
    monkeypatch.setenv("YANDEX_DISK_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("YANDEX_DISK_BASE_PATH", "/Root/finals")
    monkeypatch.setenv("YANDEX_DISK_PUBLISH", "0")

    monkeypatch.setattr(yandex_disk.requests, "put", lambda *_args, **_kwargs: _Response(status_code=201))
    monkeypatch.setattr(
        yandex_disk.requests,
        "get",
        lambda url, **_kwargs: _Response(payload={"href": "https://upload.example/custom"})
        if url.endswith("/resources/upload")
        else _Response(),
    )

    result = yandex_disk.upload_file_to_yandex_disk(Path(source), bundle_key="bundle", filename="target.mp4")

    assert result is not None
    assert result.path == "/Root/finals/bundle/target.mp4"
