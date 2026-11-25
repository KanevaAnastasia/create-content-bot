from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from app import media_generation_gate as gate


def test_media_generation_slot_allows_image_and_video_to_run_together(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_GENERATION_GATE_DIR", str(tmp_path / "gate"))
    active = 0
    maximum = 0
    order: list[str] = []
    mutex = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()

    def run(kind: str, hold: bool) -> None:
        nonlocal active, maximum
        with gate.media_generation_slot(kind, request_id=kind):
            with mutex:
                active += 1
                maximum = max(maximum, active)
                order.append(f"start:{kind}")
            if hold:
                first_started.set()
                assert release_first.wait(timeout=3)
            with mutex:
                order.append(f"end:{kind}")
                active -= 1

    image = threading.Thread(target=run, args=("image", True))
    video = threading.Thread(target=run, args=("video", False))
    image.start()
    assert first_started.wait(timeout=3)
    video.start()
    deadline = time.monotonic() + 3
    while "start:video" not in order and time.monotonic() < deadline:
        time.sleep(0.01)
    assert order == ["start:image", "start:video", "end:video"]
    release_first.set()
    image.join(timeout=3)
    video.join(timeout=3)

    assert maximum == 2
    assert order == ["start:image", "start:video", "end:video", "end:image"]
    assert gate.media_generation_status()["active"] is False
    assert gate.media_generation_status()["waiters"] == 0


def test_media_generation_slot_serializes_requests_in_the_same_lane(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_GENERATION_GATE_DIR", str(tmp_path / "gate"))
    release_first = threading.Event()
    release_second = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    idle_returned = threading.Event()

    def first() -> None:
        with gate.media_generation_slot("video", request_id="first"):
            first_started.set()
            assert release_first.wait(timeout=3)

    def second() -> None:
        with gate.media_generation_slot("video", request_id="second"):
            second_started.set()
            assert release_second.wait(timeout=3)

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert first_started.wait(timeout=3)
    second_thread.start()
    deadline = time.monotonic() + 3
    while gate.media_generation_status()["waiters"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gate.media_generation_status()["waiters"] == 1

    waiter = threading.Thread(
        target=lambda: (gate.wait_until_media_idle(quiet_sec=0, poll_sec=0.01), idle_returned.set())
    )
    waiter.start()
    release_first.set()
    assert second_started.wait(timeout=3)
    assert not idle_returned.is_set()
    release_second.set()

    first_thread.join(timeout=3)
    second_thread.join(timeout=3)
    waiter.join(timeout=3)
    assert idle_returned.is_set()


def test_image_requests_run_concurrently(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_GENERATION_GATE_DIR", str(tmp_path / "gate"))
    release_first = threading.Event()
    release_second = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    idle_returned = threading.Event()

    def run(started: threading.Event, release: threading.Event) -> None:
        with gate.media_generation_slot("image"):
            started.set()
            assert release.wait(timeout=3)

    first = threading.Thread(target=run, args=(first_started, release_first))
    second = threading.Thread(target=run, args=(second_started, release_second))
    first.start()
    assert first_started.wait(timeout=3)
    second.start()
    assert second_started.wait(timeout=3)
    status = gate.media_generation_status()
    assert len(status["active_states"]) == 2
    assert status["waiters"] == 0
    waiter = threading.Thread(
        target=lambda: (gate.wait_until_media_idle(quiet_sec=0, poll_sec=0.01), idle_returned.set())
    )
    waiter.start()
    release_first.set()
    first.join(timeout=3)
    assert not idle_returned.is_set()
    release_second.set()
    second.join(timeout=3)
    waiter.join(timeout=3)
    assert idle_returned.is_set()
    assert gate.media_generation_status()["active"] is False


def test_wait_until_idle_waits_for_concurrent_image_and_video(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_GENERATION_GATE_DIR", str(tmp_path / "gate"))
    release_image = threading.Event()
    release_video = threading.Event()
    image_started = threading.Event()
    video_started = threading.Event()
    idle_returned = threading.Event()

    def run(kind: str, started: threading.Event, release: threading.Event) -> None:
        with gate.media_generation_slot(kind):
            started.set()
            assert release.wait(timeout=3)

    image = threading.Thread(target=run, args=("image", image_started, release_image))
    video = threading.Thread(target=run, args=("video", video_started, release_video))
    image.start()
    video.start()
    assert image_started.wait(timeout=3)
    assert video_started.wait(timeout=3)
    status = gate.media_generation_status()
    assert status["active"] is True
    assert {item["lane"] for item in status["active_states"]} == {"image", "video"}

    waiter = threading.Thread(
        target=lambda: (gate.wait_until_media_idle(quiet_sec=0, poll_sec=0.01), idle_returned.set())
    )
    waiter.start()
    release_image.set()
    image.join(timeout=3)
    assert not idle_returned.is_set()
    release_video.set()
    video.join(timeout=3)
    waiter.join(timeout=3)
    assert idle_returned.is_set()


def test_syntax_entrypoint_holds_cross_chat_video_slot(monkeypatch):
    from app import syntax_video_fallback as syntax

    events: list[str] = []

    @contextmanager
    def fake_slot(kind: str, **_kwargs):
        events.append(f"enter:{kind}")
        yield
        events.append(f"exit:{kind}")

    monkeypatch.setattr(syntax, "media_generation_slot", fake_slot)
    monkeypatch.setattr(syntax, "run_syntax_telethon_fallback", lambda **_kwargs: events.append("provider") or "ok")

    result = syntax.try_generate_via_syntax(
        prompt="prompt",
        model="veo3_fast",
        duration_sec=6,
        start_image=b"image",
    )

    assert result == "ok"
    assert events == ["enter:video", "provider", "exit:video"]
