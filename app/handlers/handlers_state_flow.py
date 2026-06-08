"""Facade for state/text handlers split across focused modules."""

from .handlers_state_commands import (
    ai_approve_command,
    start,
    cancel,
    check_env,
    generate_image_command,
    generate_video_command,
    fix_stress_command,
)
from .handlers_state_start import generate, quick_generate
from .handlers_state_scene import on_message_reaction, on_photo, on_text
