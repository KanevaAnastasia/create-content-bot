"""Compact scene context without changing approved speech or creative fields."""

# Transport/accounting state cannot help write or edit a scene. Unknown creative
# fields are deliberately retained, including references and language variants.
RUNTIME_SCENE_FIELDS = frozenset({
    "media_costs", "estimated_cost_breakdown", "estimated_cost_usd",
    "video_results_by_lang", "video_candidates_by_lang", "image_candidates",
    "start_frame_image_b64", "end_frame_image_b64", "start_frame_image_path",
    "end_frame_image_path", "start_frame_file_id", "end_frame_file_id",
    "start_frame_message_id", "end_frame_message_id", "video_message_ids",
    "video_file_id", "video_path", "video_bytes", "image_bytes",
})


def scene_context(value):
    if isinstance(value, dict):
        return {key: scene_context(item) for key, item in value.items() if key not in RUNTIME_SCENE_FIELDS}
    if isinstance(value, list):
        return [scene_context(item) for item in value]
    return value
