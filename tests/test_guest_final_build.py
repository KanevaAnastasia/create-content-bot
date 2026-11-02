from app.guest_final_build import approved_guest_cartoon_scenes


def test_guest_final_build_uses_only_scenes_with_approved_video():
    scenes = [
        {
            "scene": 1,
            "video_candidates_by_lang": {
                "ru": [{"candidate_id": "approved-1", "approved_for_production": True}]
            },
        },
        {
            "scene": 2,
            "video_candidates_by_lang": {
                "ru": [{"candidate_id": "rejected", "approved_for_production": False}]
            },
        },
        {"scene": 3, "video_candidates_by_lang": {}},
        {
            "scene": 4,
            "video_candidates_by_lang": {
                "ru": [{"candidate_id": "approved-4", "approved_for_production": True}]
            },
        },
    ]

    selected = approved_guest_cartoon_scenes(scenes)

    assert [scene["scene"] for scene in selected] == [1, 4]


def test_guest_final_build_ignores_invalid_scene_payloads():
    assert approved_guest_cartoon_scenes(None) == []
    assert approved_guest_cartoon_scenes([None, {}, {"video_candidates_by_lang": {"ru": "bad"}}]) == []
