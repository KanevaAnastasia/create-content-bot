"""Local score and foley for the Inner Boss format, built from the rendered timeline."""

import math
import re
import wave
from pathlib import Path

import numpy as np

from .discipline_modes import BOSS, MODE_KEY

SAMPLE_RATE = 24000
ALARM_SOUND = Path(__file__).resolve().parents[1] / "assets/sounds/inner_boss/alarm.wav"
ALARM_DURATION = 1.0
SOUND_TYPES = ("quiet", "alarm_click", "footsteps", "breathing", "wind", "rain", "paper", "tap")
SOUND_PREFIX = "inner_boss:"
MIX_VOLUME = 0.65

_PHRASES = {
    "ru": [
        ("лень не определяет кто ты", "Лень не определяет,\nкто ты"),
        ("лень это не то кто ты есть", "Лень не определяет,\nкто ты"),
        ("мотивация помогает начать", "Мотивация помогает\nначать"),
        ("дисциплина помогает продолжать", "Дисциплина помогает\nпродолжать"),
        ("дисциплина помогает двигаться дальше", "Дисциплина помогает\nпродолжать"),
    ],
    "en": [
        ("laziness does not define who you are", "Laziness does not define\nwho you are"),
        ("laziness doesn t define who you are", "Laziness doesn't define\nwho you are"),
        ("laziness isn t who you are", "Laziness isn't\nwho you are"),
        ("motivation helps you get started", "Motivation helps you\nget started"),
        ("motivation gets you started", "Motivation gets you\nstarted"),
        ("motivation helps you start", "Motivation helps you\nstart"),
        ("discipline helps you keep going", "Discipline helps you\nkeep going"),
        ("discipline keeps you going", "Discipline keeps you\ngoing"),
        ("discipline helps you carry on", "Discipline helps you\ncarry on"),
        ("discipline helps you continue", "Discipline helps you\ncontinue"),
    ],
}
_CHALLENGE = {
    "ru": ("никто не увидит", "если ты сейчас повернёшь"),
    "en": ("no one will see", "nobody will see", "no one will know", "nobody will know", "if you turn back now"),
}


def enabled(scenes, project_slug):
    return project_slug == "discipline" and any(s.get(MODE_KEY) == BOSS for s in scenes)


def _tokens(text):
    return re.findall(r"\w+", str(text).casefold().replace("ё", "е"))


def scene_sound(candidate):
    """Save one validated cue on the scene using its existing sound_design field."""
    declared = str(candidate.get("sound_cue") or "").strip().casefold()
    if declared in SOUND_TYPES:
        return SOUND_PREFIX + declared
    action = " ".join(str(candidate.get(key) or "") for key in ("action", "visual", "display_action")).casefold()
    if re.search(r"\b(?:нажима|выключа|глуши|press|taps?|switch|hits?)", action) and re.search(r"будильник|alarm", action):
        return SOUND_PREFIX + "alarm_click"
    for sound, pattern in (
        ("wind", r"выходит на (?:гребень|вершину)|(?:reaches|emerges on) the (?:summit|ridge)"),
        ("footsteps", r"\bбежит\b|поднимается по|ид[её]т по|\bruns?\b|\bclimbs?\b|\bwalks?\b"),
        ("breathing", r"запыхал|тяжело дыш|(?:heavy|labored) breath"),
    ):
        if re.search(pattern, action):
            return SOUND_PREFIX + sound
    return SOUND_PREFIX + "quiet"


def _scene_track(scene):
    track = str(scene.get("sound_design") or "")
    return track if track.startswith(SOUND_PREFIX) else scene_sound(scene)


def cue_name(scene):
    return _scene_track(scene).split(":")[1]


def _timed_caption_words(entries):
    """Recover the spoken word from progressive caption rows and discard clipped duplicates."""
    out = []
    previous = None
    for cue in sorted(entries, key=lambda item: float(item["start_sec"])):
        if cue.get("story_timing_source") != "elevenlabs_alignment":
            continue
        block = cue.get("fixed_block_words") or []
        count = int(cue.get("visible_word_count") or 0)
        if not 0 < count <= len(block):
            continue
        start, end = float(cue["start_sec"]), float(cue["end_sec"])
        key = (tuple(block), count)
        if previous and key == previous[0] and start <= previous[1] + 0.01:
            out[-1] = (out[-1][0], out[-1][1], max(out[-1][2], end))
            previous = (key, end)
            continue
        previous = (key, end)
        out.append((str(block[count - 1]), start, end))
    return out


def _find_phrase(words, text):
    needle = _tokens(text)
    size = len(needle)
    for i in range(len(words) - size + 1):
        if [w[0] for w in words[i:i + size]] == needle:
            return words[i][1], words[i + size - 1][2]
    return None


def anchors(entries, language, duration, accent_scenes=()):
    words = [
        (token, start, end)
        for word, start, end in _timed_caption_words(entries)
        for token in _tokens(word)
    ]
    peaks = []
    used = set()
    for phrase, title in _PHRASES.get(language, []):
        timing = _find_phrase(words, phrase)
        if timing and title not in used and timing[0] < duration:
            start, end = timing
            peaks.append({"text": title, "start_sec": start, "end_sec": min(duration, max(start + 1.15, end))})
            used.add(title)
    for scene_number in accent_scenes:
        if len(peaks) >= 2:
            break
        rows = [row for row in entries if row.get("scene") == scene_number]
        if not rows:
            continue
        # Use the actual localized words and their audio timing. Only a complete,
        # quick-to-read sentence is eligible as a standalone overlay.
        words_in_scene = []
        for word, start, end in _timed_caption_words(rows):
            words_in_scene.append((word, start, end))
            if word.endswith((".", "!", "?")):
                break
        if not words_in_scene:
            continue
        start, end = words_in_scene[0][1], words_in_scene[-1][2]
        line = " ".join(word for word, _, _ in words_in_scene)
        if start >= duration or len(line) > 46 or not line.endswith((".", "!", "?")):
            continue
        if any(start < peak["end_sec"] and end > peak["start_sec"] for peak in peaks):
            continue
        parts = line.split()
        if len(line) > 24 and len(parts) > 1:
            cut = min(range(1, len(parts)), key=lambda i: abs(len(" ".join(parts[:i])) - len(line) / 2))
            line = " ".join(parts[:cut]) + "\n" + " ".join(parts[cut:])
        peaks.append({"text": line, "start_sec": start, "end_sec": min(duration, max(start + 1.15, end))})
    challenge = [result[0] for text in _CHALLENGE.get(language, ()) if (result := _find_phrase(words, text))]
    finish_accent = next((peak["start_sec"] for peak in peaks if peak["start_sec"] >= 0.6 * duration), None)
    silence_start = min(challenge, default=duration * 0.79)
    silence_end = (
        finish_accent if finish_accent is not None and finish_accent > silence_start
        else min(duration, max(silence_start + 0.5, duration * 0.9))
    )
    peaks.sort(key=lambda peak: peak["start_sec"])
    for peak, following in zip(peaks, peaks[1:]):
        peak["end_sec"] = min(peak["end_sec"], following["start_sec"])
    return peaks, (silence_start, silence_end)


def accent_scene_numbers(scenes):
    return [index for index, scene in enumerate(scenes, 1) if "accent" in _scene_track(scene).split(":")[2:]]


def score(path, scenes, *, duration, intro_duration, silence, include_music=True, events=()):
    """Write an exact-length, reusable music-and-foley stem without an external provider."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Inner Boss soundtrack requires a positive timeline.")
    size = round(duration * SAMPLE_RATE)
    t = np.arange(size, dtype=np.float32) / SAMPLE_RATE
    mix = np.zeros(size, dtype=np.float32)
    rng = np.random.default_rng(144)
    if include_music:
        before, after = silence
        pulse = np.maximum(0, np.sin(2 * np.pi * 1.25 * t)) ** 8
        envelope = np.minimum(1, t / 1.2) * np.minimum(1, (duration - t) / 0.48)
        envelope *= np.maximum(np.clip((before - t) / 0.32, 0, 1), np.clip((t - after) / 0.65, 0, 1))
        envelope *= (t >= intro_duration)
        motif = np.sin(2 * np.pi * 110 * t) + 0.27 * np.sin(2 * np.pi * 164.81 * t)
        lift = np.clip((t - after) / 2.0, 0, 1)
        mix += envelope * (0.036 * motif + 0.035 * pulse * np.sin(2 * np.pi * 55 * t)
                           + 0.038 * lift * np.sin(2 * np.pi * 220 * t))

    def add(at, seconds, make):
        left = min(size, max(0, round(at * SAMPLE_RATE)))
        count = min(size - left, round(seconds * SAMPLE_RATE))
        if count > 0:
            ts = np.arange(count, dtype=np.float32) / SAMPLE_RATE
            mix[left:left + count] += make(ts)

    def noise(ts):
        return rng.uniform(-1, 1, len(ts)).astype(np.float32)

    def click(ts):
        return 0.24 * noise(ts) * np.exp(-65 * ts)

    def step(ts):
        return (0.16 * np.sin(2 * np.pi * 76 * ts) + 0.08 * noise(ts)) * np.exp(-22 * ts)

    def ring(at, length):
        with wave.open(str(ALARM_SOUND), "rb") as source:
            samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        add(at, min(length, len(samples) / SAMPLE_RATE), lambda ts: samples[:len(ts)] * (0.65 / 32768))

    cursor = intro_duration
    for index, (scene, length) in enumerate(scenes):
        _, cue, *markers = _scene_track(scene).split(":")
        if cue == "alarm_click":
            click_at = min(0.55, length * 0.7)
            if index or "opening" not in markers:
                ring(cursor, click_at)
            add(cursor + click_at, min(0.07, length - click_at), click)
        elif cue == "footsteps":
            for offset in np.arange(0.19, max(0, length - 0.06), 0.43):
                add(cursor + offset, min(0.13, length - offset), step)
        elif cue == "tap":
            add(cursor + min(0.55, length * 0.7), min(0.09, length * 0.25), click)
        elif cue == "paper":
            add(cursor + min(0.42, length * 0.6), min(0.27, length * 0.35),
                lambda ts: 0.12 * noise(ts) * np.sin(2 * np.pi * 11 * ts) ** 2 * np.exp(-8 * ts))
        elif cue in {"breathing", "wind", "rain"}:
            gain = {"breathing": 0.095, "wind": 0.065, "rain": 0.05}[cue]

            def texture(ts):
                raw = noise(ts)
                # A short low-pass softens the procedural noise.
                soft = np.convolve(raw, np.ones(min(17, len(raw))) / min(17, len(raw)), mode="same")
                breath = (0.25 + 0.75 * np.sin(np.pi * 0.95 * ts) ** 2) if cue == "breathing" else 1
                return gain * soft * breath * np.clip(ts / 0.2, 0, 1) * np.clip((length - ts) / 0.2, 0, 1)

            add(cursor, length, texture)
        cursor += length
    for cue, at, length in events:
        if cue == "alarm":
            ring(at, length)
        elif cue == "click":
            add(at, 0.07, click)
        elif cue == "thunder":
            def thunder(ts):
                roll = np.sin(2 * np.pi * (45 * ts - 4 * ts ** 2))
                return (0.15 * roll + 0.10 * noise(ts)) * np.clip(ts / 0.06, 0, 1) * np.exp(-2.1 * ts)
            add(at, length, thunder)
        elif cue == "footsteps":
            for offset in np.arange(0, length, 0.48):
                add(at + offset, 0.13, step)
        elif cue == "paper":
            add(at, length, lambda ts: 0.09 * noise(ts) * np.sin(2 * np.pi * 12 * ts) ** 2 * np.exp(-6 * ts))
        elif cue in {"rain", "wind", "breathing"}:
            gain = {"rain": 0.055, "wind": 0.10, "breathing": 0.13}[cue]

            def atmosphere(ts):
                width = min(17, len(ts))
                soft = np.convolve(noise(ts), np.ones(width) / width, mode="same")
                pulse = 0.25 + 0.75 * np.sin(np.pi * ts) ** 2 if cue == "breathing" else 1
                return gain * soft * pulse * np.clip(ts / 0.13, 0, 1) * np.clip((length - ts) / 0.2, 0, 1)

            add(at, length, atmosphere)

    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        out.writeframes((np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes())
