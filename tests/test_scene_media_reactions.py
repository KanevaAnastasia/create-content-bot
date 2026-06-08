import asyncio
from types import SimpleNamespace

from app.handlers.handlers_state_scene import on_message_reaction
from app.state import SESSIONS
from app.scene_media_candidates import register_image_candidate, register_media_message_candidate


def test_on_message_reaction_marks_candidate_liked():
    SESSIONS.clear()
    scene = {}
    candidate = register_image_candidate(scene, kind="start", image_bytes=b"img")
    session = {"production": {"json_plan": [scene]}}
    register_media_message_candidate(
        session,
        message_id=77,
        scene_idx=0,
        media_type="image",
        candidate_id=candidate["candidate_id"],
        kind="start",
    )
    SESSIONS[555] = session
    update = SimpleNamespace(
        message_reaction=SimpleNamespace(
            chat=SimpleNamespace(id=555),
            message_id=77,
            new_reaction=[SimpleNamespace(type="emoji")],
        )
    )

    asyncio.run(on_message_reaction(update, SimpleNamespace()))

    assert candidate["liked"] is True
