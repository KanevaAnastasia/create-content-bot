from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import app.speech_alignment as alignment


def test_parse_speech_response_preserves_transcript_punctuation() -> None:
    result = alignment._parse_speech_response(
        {
            "results": [
                {
                    "alternatives": [
                        {
                            "transcript": "Привет, мир!",
                            "confidence": 0.94,
                            "words": [
                                {"word": "Привет", "startTime": "0.3s", "endTime": "0.8s"},
                                {"word": "мир", "startTime": "0.9s", "endTime": "1.2s"},
                            ],
                        }
                    ]
                }
            ],
            "totalBilledTime": "2s",
        }
    )

    assert result.recognized_text == "Привет, мир!"
    assert result.word_timings == [
        {"word": "Привет,", "start_sec": 0.3, "end_sec": 0.8},
        {"word": "мир!", "start_sec": 0.9, "end_sec": 1.2},
    ]
    assert result.confidence == 0.94
    assert result.billed_duration_sec == 2.0


def test_align_video_speech_sends_flac_and_requests_word_offsets(monkeypatch, tmp_path: Path) -> None:
    video_path = tmp_path / "scene.mp4"
    video_path.write_bytes(b"video")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"flac-audio")
        return SimpleNamespace(returncode=0)

    class FakeResponse:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {
                "results": [
                    {
                        "alternatives": [
                            {
                                "transcript": "Точное слово.",
                                "words": [
                                    {"word": "Точное", "startTime": "0.2s", "endTime": "0.6s"},
                                    {"word": "слово", "startTime": "0.7s", "endTime": "1.0s"},
                                ],
                            }
                        ]
                    }
                ]
            }

    class FakeSession:
        def __init__(self, credentials):
            captured["credentials"] = credentials

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["request"] = kwargs
            return FakeResponse()

    monkeypatch.setattr(alignment, "_resolve_ffmpeg", lambda: "/test/ffmpeg")
    monkeypatch.setattr(alignment.subprocess, "run", fake_run)
    monkeypatch.setattr(alignment.google.auth, "default", lambda **_kwargs: ("credentials", "project"))
    monkeypatch.setattr(alignment, "AuthorizedSession", FakeSession)

    result = alignment.align_video_speech(video_path=video_path, language="ru")

    assert captured["command"][:5] == ["/test/ffmpeg", "-y", "-i", str(video_path), "-vn"]
    assert captured["url"].endswith("/v1/speech:recognize")
    request = captured["request"]
    assert request["json"]["config"]["languageCode"] == "ru-RU"
    assert request["json"]["config"]["enableWordTimeOffsets"] is True
    assert request["json"]["audio"]["content"]
    assert result.word_timings[-1] == {"word": "слово.", "start_sec": 0.7, "end_sec": 1.0}
