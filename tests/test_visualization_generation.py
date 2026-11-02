from __future__ import annotations

import json
import io
import wave

import pytest

from app.visualization_generation import (
    format_visualization_transcript,
    parse_visualization_transcript,
    prepare_visualization_media,
    resolve_visualization_trim_end,
)
from app.ai_approval_service import _resolve_ffmpeg


def test_parse_visualization_transcript_keeps_text_and_phrase_timings() -> None:
    raw = json.dumps(
        {
            "title": "Сила привычки",
            "full_text": "Начните с малого. Повторяйте каждый день.",
            "segments": [
                {"start_sec": 0.4, "end_sec": 2.2, "text": "Начните с малого."},
                {"start_sec": 2.6, "end_sec": 5.1, "text": "Повторяйте каждый день."},
            ],
        },
        ensure_ascii=False,
    )

    result = parse_visualization_transcript(raw, duration_sec=5.5)

    assert result.title == "Сила привычки"
    assert result.text == "Начните с малого. Повторяйте каждый день."
    assert result.segments[0] == {"start_sec": 0.4, "end_sec": 2.2, "text": "Начните с малого."}
    assert result.alignment["characters"] == list(result.text)
    assert len(result.alignment["character_start_times_seconds"]) == len(result.text)
    assert max(result.alignment["character_end_times_seconds"]) <= 5.5
    rendered = format_visualization_transcript(result)
    assert "0.40–2.20 сек" in rendered
    assert "Повторяйте каждый день" in rendered


def test_parse_visualization_transcript_falls_back_to_whole_track() -> None:
    raw = json.dumps({"title": "Тема", "full_text": "Одна короткая фраза.", "segments": []}, ensure_ascii=False)

    result = parse_visualization_transcript(raw, duration_sec=3.0)

    assert result.segments == [{"start_sec": 0.0, "end_sec": 3.0, "text": "Одна короткая фраза."}]
    assert result.alignment["character_end_times_seconds"][-1] == 3.0


def test_parse_visualization_transcript_rejects_silent_media() -> None:
    raw = json.dumps({"title": "", "full_text": "", "segments": []})

    try:
        parse_visualization_transcript(raw, duration_sec=4.0)
    except ValueError as exc:
        assert "распознать речь" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("silent media must be rejected")


def test_prepare_visualization_media_normalizes_audio_and_builds_mp4_proxy(tmp_path, monkeypatch) -> None:
    if not _resolve_ffmpeg():
        pytest.skip("ffmpeg is unavailable")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 8000)
    monkeypatch.chdir(tmp_path)

    result = prepare_visualization_media(stream.getvalue(), chat_id=12, file_name="source.wav")

    assert result.duration_sec == pytest.approx(1.0, abs=0.1)
    assert result.audio_bytes.startswith(b"ID3") or result.audio_bytes.startswith(b"\xff")
    assert result.analysis_video_bytes[4:8] == b"ftyp"


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (15.4, 15.0),
        (15.5, 15.0),
        (16.2, 16.2),
        (18.0, 18.0),
    ],
)
def test_visualization_trim_only_discards_half_second_tail(duration: float, expected: float) -> None:
    assert resolve_visualization_trim_end(
        duration,
        output_policy={"trim_to_previous_multiple_sec": 3, "max_discard_tail_sec": 0.5},
    ) == expected


def test_visualization_trim_removes_social_outro_before_rounding() -> None:
    assert resolve_visualization_trim_end(
        19.4,
        output_policy={
            "remove_social_outro": True,
            "trim_to_previous_multiple_sec": 3,
            "max_discard_tail_sec": 0.5,
        },
        source_media={"author_outro_start_sec": 15.4},
    ) == 15.0
