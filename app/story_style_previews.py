"""Show the same style references that guide Story image generation."""
import logging
from pathlib import Path

from telegram import InputMediaPhoto

from .story_generation import STORY_VISUAL_STYLES

logger = logging.getLogger(__name__)
PREVIEW_BUTTON = "Показать примеры"
ROOT = Path(__file__).resolve().parent.parent


async def send_story_style_previews(message) -> bool:
    sender = getattr(message, "reply_media_group", None)
    if not callable(sender):
        return False
    media = []
    complete = True
    for style in STORY_VISUAL_STYLES.values():
        reference = style.get("reference_path")
        if not reference:
            continue  # Primary style comes from the project's own character references.
        path = ROOT / reference
        try:
            media.append(InputMediaPhoto(media=path.read_bytes(), filename=path.name,
                                         caption=style["label"]))
        except OSError:
            complete = False
            logger.warning("story_style_preview_missing path=%s", path)
    # Two manageable albums; Telegram accepts at most ten media per album.
    for offset in range(0, len(media), 7):
        batch = media[offset:offset + 7]
        try:
            if len(batch) == 1:
                await message.reply_photo(photo=batch[0].media, caption=batch[0].caption)
            else:
                await sender(media=batch, read_timeout=60, write_timeout=60)
        except Exception:
            complete = False
            logger.exception("story_style_preview_delivery_failed batch=%s", offset // 7)
    return complete and bool(media)
