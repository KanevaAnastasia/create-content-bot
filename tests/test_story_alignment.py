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
