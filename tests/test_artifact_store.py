from app import artifact_store


class _UploadResult:
    def as_payload(self):
        return {
            "status": "uploaded",
            "path": "/Content Fabric/final builds/bundle-1/final.mp4",
            "web_url": "https://disk.yandex.ru/client/disk/Content%20Fabric/final%20builds/bundle-1/final.mp4",
        }


def test_persist_post_production_artifacts_labels_titled_variant(monkeypatch):
    calls: list[dict] = []

    def _fake_persist_generation_artifact(session, **kwargs):
        calls.append(kwargs)
        return "artifact-id"

    monkeypatch.setattr(artifact_store, "persist_generation_artifact", _fake_persist_generation_artifact)

    artifact_store.persist_post_production_artifacts(
        {},
        {
            "artifact_bundle_key": "bundle-1",
            "renders_by_language": {
                "ru": {
                    "titled_subtitled_music": {
                        "video_path": "/tmp/final_titled_subtitled_music_master_ru.mp4",
                        "width": 1080,
                        "height": 1920,
                    }
                }
            },
        },
    )

    assert calls == [
        {
            "artifact_type": "final_titled_subtitled_music",
            "label": "Final titled/subtitled/music master (ru)",
            "uri": "file:///tmp/final_titled_subtitled_music_master_ru.mp4",
            "language": "ru",
            "mime_type": "video/mp4",
            "payload": {
                "video_path": "/tmp/final_titled_subtitled_music_master_ru.mp4",
                "width": 1080,
                "height": 1920,
                "artifact_bundle_key": "bundle-1",
            },
        }
    ]


def test_persist_post_production_artifacts_labels_upscaled_variant(monkeypatch):
    calls: list[dict] = []

    def _fake_persist_generation_artifact(session, **kwargs):
        calls.append(kwargs)
        return "artifact-id"

    monkeypatch.setattr(artifact_store, "persist_generation_artifact", _fake_persist_generation_artifact)

    artifact_store.persist_post_production_artifacts(
        {},
        {
            "artifact_bundle_key": "bundle-4k",
            "renders_by_language": {
                "en": {
                    "upscaled_4k": {
                        "video_path": "/tmp/final_upscaled_4k_master_en.mp4",
                        "width": 2160,
                        "height": 3840,
                    }
                }
            },
        },
    )

    assert calls[0]["artifact_type"] == "final_upscaled_4k"
    assert calls[0]["label"] == "Final 4K upscaled master (en)"
    assert calls[0]["payload"]["width"] == 2160
    assert calls[0]["payload"]["height"] == 3840


def test_persist_post_production_artifacts_adds_yandex_disk_payload(monkeypatch):
    calls: list[dict] = []
    uploads: list[dict] = []

    def _fake_persist_generation_artifact(session, **kwargs):
        calls.append(kwargs)
        return "artifact-id"

    def _fake_upload(video_path, *, bundle_key=None, filename=None):
        uploads.append({"video_path": video_path, "bundle_key": bundle_key, "filename": filename})
        return _UploadResult()

    monkeypatch.setattr(artifact_store, "persist_generation_artifact", _fake_persist_generation_artifact)
    monkeypatch.setattr(artifact_store, "upload_file_to_yandex_disk", _fake_upload)

    artifact_store.persist_post_production_artifacts(
        {},
        {
            "artifact_bundle_key": "bundle-1",
            "renders_by_language": {
                "ru": {
                    "clean": {
                        "video_path": "/tmp/final.mp4",
                        "width": 1080,
                        "height": 1920,
                    }
                }
            },
        },
    )

    assert uploads == [{"video_path": "/tmp/final.mp4", "bundle_key": "bundle-1", "filename": "final.mp4"}]
    assert calls[0]["payload"]["yandex_disk"]["status"] == "uploaded"
    assert calls[0]["payload"]["yandex_disk"]["path"].endswith("/final.mp4")


def test_persist_post_production_artifacts_records_yandex_disk_failure(monkeypatch):
    calls: list[dict] = []

    def _fake_persist_generation_artifact(session, **kwargs):
        calls.append(kwargs)
        return "artifact-id"

    def _fake_upload(*_args, **_kwargs):
        raise RuntimeError("temporary upload failure")

    monkeypatch.setattr(artifact_store, "persist_generation_artifact", _fake_persist_generation_artifact)
    monkeypatch.setattr(artifact_store, "upload_file_to_yandex_disk", _fake_upload)
    monkeypatch.setattr(artifact_store, "yandex_disk_upload_required", lambda: False)

    artifact_store.persist_post_production_artifacts(
        {},
        {
            "artifact_bundle_key": "bundle-err",
            "renders_by_language": {"ru": {"clean": {"video_path": "/tmp/final.mp4"}}},
        },
    )

    assert calls[0]["payload"]["yandex_disk"]["status"] == "failed"
    assert "temporary upload failure" in calls[0]["payload"]["yandex_disk"]["error"]
