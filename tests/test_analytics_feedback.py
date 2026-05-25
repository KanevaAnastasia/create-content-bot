from app.analytics_feedback import build_analytics_feedback, normalize_analytics_payload


class Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_normalize_analytics_payload_casts_numbers_and_requires_platform() -> None:
    data = normalize_analytics_payload(
        {
            "platform": "Instagram",
            "views": "1200",
            "average_percentage_viewed": "83.5",
            "retention_3s": "91",
            "posted_at": "2026-05-25T12:00:00+03:00",
        }
    )

    assert data["platform"] == "Instagram"
    assert data["views"] == 1200
    assert data["average_percentage_viewed"] == 83.5
    assert data["posted_at"].year == 2026


def test_build_analytics_feedback_ranks_hooks_and_first_frames() -> None:
    rows = [
        Row(hook="hook A", first_frame_type="cut", recipe="торт", format="secret", average_percentage_viewed=80, retention_3s=90, saves=20),
        Row(hook="hook B", first_frame_type="prep", recipe="суп", format="plain", average_percentage_viewed=50, retention_3s=60, saves=3),
    ]

    feedback = build_analytics_feedback(rows)

    assert feedback["best_hooks_by_average_percentage_viewed"][0]["value"] == "hook A"
    assert feedback["best_first_frame_types_by_retention_3s"][0]["value"] == "cut"
    assert feedback["formats_to_repeat"][0]["value"] == "secret"
