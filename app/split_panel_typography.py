"""Placement and exact spoken-opening timing for stacked-panel stories."""
import math
import re


def speaker_panel(style):
    value = (style or {}).get('speaker_panel')
    return value if value in ('top', 'bottom') else None


def caption_anchor(style):
    return {'top': .30, 'bottom': .80}.get(speaker_panel(style))


def opening_timing(title, text, duration, alignment, *, omitted_words=()):
    """Match only the opening sentence; skip only explicitly allowed spoken words."""
    from .story_generation import _word_timing_rows
    from .discipline_modes import opening_sentence
    from .story_alignment import valid_alignment
    clean = lambda word: re.sub(r'[^\w]+', '', word).casefold()
    if not valid_alignment(alignment):
        return [], None
    try:
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0 or any(
            float(end) > duration + .05 for end in alignment['character_end_times_seconds']
        ):
            return [], None
    except (TypeError, ValueError, OverflowError):
        return [], None
    if re.findall(r'\w+', ''.join(alignment['characters']).casefold()) != re.findall(r'\w+', text.casefold()):
        return [], None
    opening = opening_sentence(text).split()
    all_rows = _word_timing_rows(text, duration, alignment)
    rows = all_rows[:len(opening)]
    next_start = all_rows[len(opening)][1] if len(all_rows) > len(opening) else duration
    allowed = {clean(word) for word in omitted_words}
    target = title.split()
    timing = []
    cursor = 0
    for word, start, end in rows:
        if cursor < len(target) and clean(word) == clean(target[cursor]):
            timing.append({'text': target[cursor], 'start': start, 'end': min(end, next_start)})
            cursor += 1
        elif clean(word) not in allowed:
            return [], None
    if not rows or cursor != len(target):
        return [], None
    return timing, [rows[0][1], min(rows[-1][2], next_start)]


def configure_opening_titles(session, titles, style):
    """Use the spoken opening, with only the owner's explicit short-form exceptions."""
    from .discipline_modes import opening_sentence
    approved_short_forms = {
        'Каждый мой день такой же, как вчера.': ('Каждый день такой же, как вчера.', ['мой']),
        'Every day of mine is just like yesterday.': ('Every day is just like yesterday.', ['of', 'mine']),
    }
    for language, suffix in (('ru', ''), ('en', '_en')):
        if language not in titles:
            continue
        text = str(session.get('story_voiceover_text' + suffix) or '')
        opening = opening_sentence(text)
        title, omitted = approved_short_forms.get(opening, (opening, []))
        titles[language] = title
        timing, interval = opening_timing(title, text,
            session.get('story_audio_duration_sec' + suffix, 0),
            session.get('story_audio_alignment' + suffix), omitted_words=omitted)
        style.setdefault('word_timing_by_lang', {})[language] = timing
        style.setdefault('subtitle_suppression_ranges_by_lang', {})[language] = [interval] if interval else []
        style.setdefault('part_colors_by_lang', {})[language] = {'0': '#ffd456'}
        if opening in approved_short_forms:
            style.setdefault('semantic_parts_by_lang', {})[language] = (
                ['Каждый день', 'такой же,', 'как вчера.'] if language == 'ru'
                else ['Every day', 'is just like', 'yesterday.'])
