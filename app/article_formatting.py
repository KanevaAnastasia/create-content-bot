"""The same explicit bold spans for Bot API previews and Telegram channel posts."""
from __future__ import annotations

import re


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def render_article(text):
    """Remove paired inline ** markers; return Telegram UTF-16 bold ranges."""
    parts, spans = [], []
    cursor = offset = 0
    for match in re.finditer(r"\*\*([^*\n]+)\*\*", text):
        before, heading = text[cursor:match.start()], match.group(1)
        parts.extend((before, heading))
        offset += utf16_length(before)
        length = utf16_length(heading)
        spans.append((offset, length))
        offset += length
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts), spans
