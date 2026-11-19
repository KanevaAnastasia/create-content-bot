from app.story_alignment import history_alignment, load_story_audio_metadata, save_story_audio_metadata, valid_alignment
from app.story_generation import StoryAudioResult, store_story_audio


def alignment():
    return {"characters": ["Д", "а"], "character_start_times_seconds": [0.0, 0.3], "character_end_times_seconds": [0.3, 0.6]}


def test_audio_metadata_survives_session_and_rejects_replaced_audio(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = StoryAudioResult(audio_bytes=b"approved", content_type="audio/mpeg", duration_sec=0.6,
                             alignment=alignment(), request_id="request", history_item_id="history", model_id="model")
    from pathlib import Path
    path = Path(store_story_audio(1, result))
    saved = load_story_audio_metadata(path)
    assert saved["alignment"] == alignment()
    assert saved["history_item_id"] == "history"
    path.write_bytes(b"different narration")
    assert load_story_audio_metadata(path) == {}


def test_history_durations_converted_only_when_entire_sequence_matches():
    source = alignment()
    source["character_end_times_seconds"] = [0.3, 0.2]
    restored = history_alignment(source)
    assert restored["character_end_times_seconds"] == [0.3, 0.5]
    source["character_end_times_seconds"] = [0.1, 0.2]
    assert history_alignment(source) == {}


def test_partial_or_nonfinite_alignment_is_rejected():
    source = alignment()
    source["character_start_times_seconds"] = [0.0]
    assert not valid_alignment(source)
    source = alignment()
    source["character_end_times_seconds"][-1] = float("nan")
    assert not valid_alignment(source)


def test_response_alignment_uses_valid_raw_when_normalized_is_invalid():
    from app.story_alignment import response_alignment
    assert response_alignment({"normalized_alignment": {"characters": ["bad"]}, "alignment": alignment()}) == alignment()
    assert response_alignment({"alignments": {"alignment": alignment()}}) == alignment()


def test_story_generation_does_not_hide_provider_failure(monkeypatch):
    import pytest
    from app import story_generation
    def failed(*args, **kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(story_generation, "_timed_endpoint_result", failed)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        story_generation.generate_story_audio("Approved text", language="en")


def test_missing_timestamps_preserves_audio_without_second_generation(tmp_path, monkeypatch):
    import base64
    import pytest
    from app import story_generation
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice")
    calls = []
    class Response:
        ok = True
        headers = {"history-item-id": "recover-me"}
        def json(self):
            return {"audio_base64": base64.b64encode(b"paid-audio").decode()}
    def post(*args, **kwargs):
        calls.append(args)
        return Response()
    monkeypatch.setattr("requests.post", post)
    with pytest.raises(RuntimeError, match="Saved for recovery"):
        story_generation.generate_story_audio("Hello", language="en")
    files = list(tmp_path.glob("artifacts/story_audio/0/*.mp3"))
    assert len(calls) == len(files) == 1
    assert files[0].read_bytes() == b"paid-audio"
    assert load_story_audio_metadata(files[0])["history_item_id"] == "recover-me"
