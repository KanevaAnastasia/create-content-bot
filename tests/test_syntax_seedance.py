from __future__ import annotations

from app import syntax_seedance


class _FakePage:
    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.selectors: list[str] = []
        self.waits: list[int] = []
        self.evaluations: list[str] = []

    def evaluate(self, expression: str) -> None:
        self.evaluations.append(expression)

    def set_input_files(self, selector: str, path: str) -> None:
        self.selectors.append(selector)
        self.uploads.append(path)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)


class _FakeGotoPage:
    def __init__(self, runtime_failures: int) -> None:
        self.runtime_failures = runtime_failures
        self.urls: list[str] = []
        self.waits: list[int] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.urls.append(url)

    def wait_for_function(self, _expression: str, *, timeout: int) -> None:
        assert timeout == 15_000
        if self.runtime_failures:
            self.runtime_failures -= 1
            raise RuntimeError("Telegram WebApp runtime missing")

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)


def test_upload_seedance_reference_retries_until_count_advances(monkeypatch):
    page = _FakePage()
    attempts = {"count": 0}

    def _wait(_page, minimum, *, timeout_sec):
        assert minimum == 3
        assert timeout_sec == syntax_seedance.SEEDANCE_REFERENCE_UPLOAD_TIMEOUT_SEC
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("still at 2/11")

    monkeypatch.setattr(syntax_seedance, "_wait_for_uploaded_count", _wait)

    assert syntax_seedance._upload_seedance_reference(page, "style.jpg", minimum=3, index=3) is True
    assert page.uploads == ["style.jpg", "style.jpg", "style.jpg"]
    assert page.waits == [500, 1500, 500, 3000, 500]


def test_upload_seedance_reference_returns_false_after_retries(monkeypatch):
    page = _FakePage()

    def _wait(*_args, **_kwargs):
        raise RuntimeError("still at 2/11")

    monkeypatch.setattr(syntax_seedance, "_wait_for_uploaded_count", _wait)

    assert syntax_seedance._upload_seedance_reference(page, "style.jpg", minimum=3, index=3) is False
    assert len(page.uploads) == syntax_seedance.SEEDANCE_REFERENCE_UPLOAD_ATTEMPTS


def test_upload_seedance_reference_targets_primary_dropzone(monkeypatch):
    page = _FakePage()
    monkeypatch.setattr(syntax_seedance, "_wait_for_uploaded_count", lambda *_args, **_kwargs: None)

    assert syntax_seedance._upload_seedance_reference(page, "style.jpg", minimum=1, index=1) is True
    assert page.uploads == ["style.jpg"]
    assert page.selectors == ['input[x-ref="mediaFileInput"]']
    assert 'input[x-ref="fileInput"]' in page.evaluations[0]


def test_uploaded_count_accepts_current_fifty_file_limit():
    assert syntax_seedance._uploaded_count_from_body("Загруженно (1/50)") == 1
    assert syntax_seedance._uploaded_count_from_body("Загруженна (3 / 11)") == 3


def test_goto_seedance_webview_retries_when_telegram_runtime_is_missing():
    page = _FakeGotoPage(runtime_failures=1)

    syntax_seedance._goto_seedance_webview(page, "https://example.test/seedance")

    assert page.urls == ["https://example.test/seedance", "about:blank", "https://example.test/seedance"]
    assert page.waits == [2500]
