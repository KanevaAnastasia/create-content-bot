from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from app import media_generation_gate as gate


def test_media_generation_slot_serializes_image_and_video_across_threads(monkeypatch, tmp_path):
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
    time.sleep(0.1)
    assert order == ["start:image"]
    release_first.set()
    image.join(timeout=3)
    video.join(timeout=3)

    assert maximum == 1
    assert order == ["start:image", "end:image", "start:video", "end:video"]
    assert gate.media_generation_status()["active"] is False
    assert gate.media_generation_status()["waiters"] == 0


def test_wait_until_idle_includes_requests_already_waiting(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_GENERATION_GATE_DIR", str(tmp_path / "gate"))
    release_first = threading.Event()
    release_second = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    idle_returned = threading.Event()

    def first() -> None:
        with gate.media_generation_slot("image"):
            first_started.set()
            assert release_first.wait(timeout=3)

    def second() -> None:
        with gate.media_generation_slot("video"):
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


def test_syntax_entrypoint_holds_global_video_slot(monkeypatch):
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
