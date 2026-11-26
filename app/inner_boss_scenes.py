"""Scene scripts and local picture/sound assembly for the Inner Boss workflow."""
from __future__ import annotations

import re
from pathlib import Path

from . import inner_boss_mix

SCRIPT_HEADER = re.compile(r"(?im)^\s{0,3}(?:#{1,6}\s*)?(?:\*\*)?сцена\s+(\d+)\b[^\n]*")
SPEAKER = re.compile(r'(?i)(?<!\w)(босс|вася|василий|герой)\s*(?::\s*|(?=[«"“]))')
QUOTED = re.compile(r'«([^»]+)»|"([^"]+)"|“([^”]+)”')
DIRECTIVE = re.compile(r"(?im)^\s*(звук|титр|титр en|в кадре)\s*:\s*([^\n]+)")
SOUNDS = (
    ("alarm", r"\bбудильник\w*"),
    ("click", r"\bщелч\w*|выключает будильник|будильник замолкает"),
    ("thunder", r"\bгром\b|гремит|гроза"),
    ("rain", r"\bдожд\w*|\bлуж\w*"),
    ("footsteps", r"\bшаг\w*|\bбежит\b|\bпробегает\b"),
    ("breathing", r"\bдыш\w*|\bдыхан\w*|\bвыдох\w*"),
    ("wind", r"\bветер\b|\bветра\b"),
    ("paper", r"\bшорох\w*"),
)


def parse_script(text: str) -> list[dict]:
    """Extract only explicitly labelled lines; retain their position in the script."""
    text = text.replace("**", "")
    headers = list(SCRIPT_HEADER.finditer(text))
    if not headers or len(headers) > 30 or [int(h[1]) for h in headers] != list(range(1, len(headers) + 1)):
        raise ValueError("Пронумеруй сцены подряд, начиная со строки «Сцена 1».")
    scenes = []
    for index, header in enumerate(headers):
        body = text[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)].strip()
        title = header[0].strip().lstrip("# ").strip()
        duration = re.search(r"(?<!\d)(\d+)\s*с(?:ек\w*)?\b", title, re.I)
        seconds = int(duration[1]) if duration else 7
        if not body or not 3 <= seconds <= 15:
            raise ValueError(f"Сцена {index + 1} должна содержать действие и длиться от 3 до 15 секунд.")
        turns, actions, last_end = [], [], 0
        directive_ranges = [match.span() for match in DIRECTIVE.finditer(body)]
        for speaker in SPEAKER.finditer(body):
            if speaker.start() < last_end or any(start <= speaker.start() < end for start, end in directive_ranges):
                continue
            quoted = QUOTED.match(body, speaker.end())
            if quoted is None:
                raise ValueError(f"Сцена {index + 1}. Заключи реплику после имени говорящего в кавычки.")
            actions.append(body[last_end:speaker.start()])
            turns.append({
                "speaker": "boss" if speaker[1].lower() == "босс" else "hero",
                "text": next(group for group in quoted.groups() if group is not None).strip(),
            })
            last_end = quoted.end()
        actions.append(body[last_end:])
        action = " ".join(actions)
        labels: dict[str, list[str]] = {}
        for match in DIRECTIVE.finditer(action):
            labels.setdefault(match[1].casefold(), []).append(match[2].strip().strip("«»\""))
        visual = DIRECTIVE.sub(lambda match: match[2] if match[1].casefold() == "в кадре" else "", action)
        visual = re.sub(r"(?m)^\s*[.,;]+\s*$", "", visual).strip()
        fx_text = " ".join(labels.get("звук", [])) if "звук" in labels else visual
        if not visual:
            raise ValueError(f"Добавь действие в сцену {index + 1}.")
        fx_times = {}
        for fragment in labels.get("звук", []):
            for cue, pattern in SOUNDS:
                for part in fragment.split(","):
                    match = re.search(r"@\s*(\d+(?:\.\d+)?)$", part.strip())
                    if match and re.search(pattern, part, re.I):
                        when = float(match[1])
                        if when >= seconds:
                            raise ValueError(f"Звук в сцене {index + 1} выходит за её длительность.")
                        fx_times.setdefault(cue, []).append(when)
        scenes.append({
            "title": title, "body": body, "visual": visual, "turns": turns, "duration": seconds,
            "fx": [key for key, pattern in SOUNDS if re.search(pattern, fx_text, re.I)], "fx_times": fx_times,
            "titles": {"ru": labels.get("титр", []), "en": labels.get("титр en", [])},
        })
    return scenes


PILOT_SCRIPT = """Сцена 1. День 1 · 8 секунд
Общий боковой план спальни в ночной 2D-рисовке. Герой спит с закрытыми глазами на деревянной кровати внизу кадра. Под головой подушка, на синем одеяле видны складки. Слева тумбочка с будильником, справа наверху окно со шторами. На будильнике 06:00. Звонит будильник. Герой приоткрывает глаза, и на стене над кроватью появляется Босс.
Босс: «Давай ещё пять минуточек».
Герой: «Ладно, пять минуточек ничего не изменят».
Герой тянется к кнопке. Щелчок, будильник замолкает. Он снова укрывается и закрывает глаза. Склейка в том же ракурсе. На часах уже 09:00.
Звук: будильник, щелчок, шорох одеяла.

Сцена 2. День 2 · 7 секунд
На будильнике снова 06:00. Герой открывает глаза. За окном идёт дождь. Гремит гром. На стене появляется Босс.
Босс: «В такую погоду? Давай завтра».
Герой выключает будильник и поворачивается к стене. Кроссовки остаются у двери.
Звук: будильник, дождь, гром, щелчок.

Сцена 3. Следующие месяцы · 7 секунд
Тот же ракурс. Звонит будильник, рука нажимает кнопку. За окном меняются сезоны. С каждым новым утром слышны тот же сигнал и тот же щелчок. В последний раз герой выключает будильник с закрытыми глазами.
Звук: будильник@0, щелчок@0.75, будильник@2.3, щелчок@3.05, будильник@4.6, щелчок@5.35.

Сцена 4. Год спустя · 8 секунд
Герой достаёт из шкафа коробку с кроссовками. На подошвах ещё магазинные наклейки.
Герой: «Я их в прошлом ноябре купил».
В зеркале у двери появляется Босс.
Босс: «Ну и куда ты в них сходил?»
Герой разворачивает план пробежек. Все клетки пустые.
Звук: шорох бумаги.

Сцена 5. У двери · 9 секунд
Герой садится у двери и надевает кроссовки. Босс смотрит на него из зеркала.
Босс: «Ты просто ленивый!»
Герой поднимает глаза.
Герой: «Я смогу измениться! Лень — это не то, кто ты. Это то, что ты повторяешь!»
Он затягивает шнурки, открывает дверь и выходит.
Звук: щелчок замка.

Сцена 6. Первая пробежка · 8 секунд
Герой добегает до первого фонаря и останавливается. Упирается руками в колени, тяжело дышит. Впереди виден второй фонарь. Рядом появляется Босс.
Босс: «Ну что, обратно?»
Герой выпрямляется, смотрит на второй фонарь и идёт к нему пешком.
Звук: шаги, дыхание.

Сцена 7. День 12 · 7 секунд
Герой снова пробегает первый фонарь. На подъёме сбавляет темп и переходит на шаг. На этот раз он проходит мимо второго фонаря и идёт дальше. Слышно, как постепенно выравнивается дыхание.
Звук: шаги, дыхание.

Сцена 8. День 48 · 6 секунд
На будильнике 06:00. За окном дождь. Герой открывает глаза. Гремит гром. Появляется Босс.
Босс: «Слышишь? Опять дождь».
Герой: «Слышу».
Он выключает будильник и ставит ноги на пол. Склейка по шагу: тапочек касается пола, кроссовок наступает в лужу.
Звук: будильник, гром, дождь, щелчок, шаг.

Сцена 9. День 170 · 7 секунд
Снова первый фонарь. Герой пробегает мимо, держит темп на подъёме и проходит второй фонарь бегом. Камера остаётся на месте, пока он не скрывается за поворотом.
Звук: ровные шаги.

Сцена 10. День 365 · 8 секунд
Длинный подъём перед рассветом. Герой сбавляет темп и останавливается. Поперёк тропы появляется Босс.
Босс: «Ты и так далеко забрался. Никто не увидит, если ты сейчас повернёшь».
Герой оглядывается на тропу внизу. Он снова смотрит вверх.
Звук: дыхание, ветер.

Сцена 11. Последние метры · 8 секунд
Крупно: герой делает шаг. Потом ещё один. Босс остаётся позади. Два титра появляются последовательно, второй коротко дрожит на шаге и замирает.
Титр: Мотивация помогает начать.
Титр: Дисциплина помогает продолжать.
Титр EN: Motivation helps you start.
Титр EN: Discipline helps you keep going.
Звук: шаги.

Сцена 12. Вершина · 7 секунд
Герой выходит на гребень. Камера раскрывает общий план: внизу виден весь подъём, впереди рассвет. Он останавливается и выдыхает. Держим кадр несколько секунд.
Звук: ветер, дыхание."""


CLOCK = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
APPEARANCE = re.compile(
    r"(?i)\b(?:появляется\s+босс|босс\s+появляется|босс\s+возникает|возникает\s+босс)"
)


def frame_actions(scene: dict) -> tuple[str, str]:
    """Return a text-free opening beat and, when written, the Boss appearance beat."""
    visual = re.sub(
        r"(?i)\bна (?:будильнике|часах)\s+(?:(?:снова|уже)\s+)?\d{1,2}:\d{2}\.?",
        " ", scene["visual"],
    )
    clean = " ".join(QUOTED.sub("", CLOCK.sub("", visual)).split())
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    for index, sentence in enumerate(sentences):
        if index and APPEARANCE.search(sentence):
            return " ".join(sentences[:index]), " ".join(sentences[index:])
    return clean, ""


def clock_overlays(scene: dict, duration: float, *, speech_end: float = 0.0) -> list[dict]:
    times = [match[0] for match in CLOCK.finditer(scene["visual"])]
    if not times:
        return []
    position = scene.get("clock_position")
    style = (
        {"anchor_x_ratio": float(position[0]), "anchor_ratio": float(position[1]), "clock_display": True}
        if isinstance(position, (list, tuple)) and len(position) == 2 else {"anchor_ratio": 0.15}
    )
    change = max(1.7, duration - 1.15, speech_end + 0.18 if speech_end else 0.0)
    has_change = times[-1] != times[0]
    first_end = (change if has_change else duration) if style.get("clock_display") else min(1.7, duration)
    cues = [{"text": times[0], "start_sec": 0.0, "end_sec": first_end, "shake": False, **style}]
    if has_change:
        cues.append({"text": times[-1], "start_sec": change, "end_sec": duration, "shake": False, **style})
    return cues


def effect_events(scene: dict, duration: float, speech_end: float) -> list[tuple[str, float, float]]:
    events = []
    for cue in scene["fx"]:
        start, length = {
            "alarm": (0.0, inner_boss_mix.ALARM_DURATION),
            "click": (min(duration - 0.15, max(0.58, speech_end + 0.15)), 0.07),
            "thunder": (0.56 if "alarm" in scene["fx"] else 0.2, 1.6),
            "rain": (0.0, duration),
            "wind": (0.0, duration),
            "breathing": (duration * 0.3, duration * 0.7),
            "footsteps": (0.15, max(0.16, duration - 0.5)),
            "paper": (duration * 0.76, 0.35),
        }[cue]
        times = scene.get("fx_times", {}).get(cue, [start])
        events.extend((cue, at, min(length, duration - at)) for at in times)
    return events


def speech_lead(scene: dict) -> float:
    arrival = bool(frame_actions(scene)[1])
    if "alarm" in scene["fx"] or (arrival and scene["turns"] and scene["turns"][0]["speaker"] == "boss"):
        return 0.82
    return 0.16


def render_scene(scene: dict, path: Path, *, language: str, duration: int) -> None:
    """Render a dialogue/SFX preview with no background score."""
    from .post_production import (
        _alignment_word_rows, _media_duration, _resolve_ffmpeg, _run_ffmpeg,
        _subtitle_filter, _video_dimensions, _write_burned_subtitle_ass,
        build_word_synced_subtitle_cues,
    )

    raw = Path(scene["video"])
    measured = _media_duration(raw)
    if not measured or measured + 0.08 < duration or measured - duration > 1.05:
        raise ValueError("Длина клипа не совпадает с монтажом. Утверди другую длительность или клип.")
    audio = scene.get("audio", {}).get(language)
    lead = speech_lead(scene)
    speech_end = lead + audio["duration"] if audio else 0.0
    speech_cue = scene.get("speech_not_before_sec")
    hold = 0.0
    if audio and speech_cue is not None:
        # End both dubs at the same action. The shorter language enters later.
        speech_end = float(speech_cue) + max(a["duration"] for a in scene["audio"].values())
        lead = speech_end - audio["duration"]
        duration_with_tail = max(duration, round(speech_end + 0.65, 3))
        hold = duration_with_tail - duration
        duration = duration_with_tail
    if speech_end > duration:
        raise ValueError("В клип не помещается утверждённая реплика.")
    stem, ass = path.with_suffix(".wav"), path.with_suffix(".ass")
    inner_boss_mix.score(
        stem, [], duration=duration, intro_duration=0, silence=(0, 0), include_music=False,
        events=effect_events(scene, duration, speech_end),
    )
    captions = []
    if audio:
        words = [(word, start + lead, end + lead) for word, start, end in _alignment_word_rows(audio["alignment"])]
        captions = build_word_synced_subtitle_cues(words, window_end_sec=min(duration, speech_end + 0.16),
                                                   timing_source="elevenlabs_alignment")
    titles = scene["titles"][language]
    duration_per_title = (duration - 0.85) / len(titles) if titles else 0
    accents = [
        {"text": title, "start_sec": 0.65 + i * duration_per_title,
         "end_sec": 0.65 + (i + 1) * duration_per_title, "shake": i == len(titles) - 1}
        for i, title in enumerate(titles)
    ]
    accents.extend(clock_overlays(scene, duration, speech_end=speech_end if speech_cue is not None else 0.0))
    width, height = _video_dimensions(raw)
    _write_burned_subtitle_ass(ass, captions, accent_cues=accents, video_width=width, video_height=height)
    ffmpeg = _resolve_ffmpeg()
    cmd = [ffmpeg, "-y", "-i", str(raw), "-i", str(stem)]
    if audio:
        cmd += [
            "-i", audio["path"], "-filter_complex",
            f"[2:a]adelay={round(lead * 1000)}:all=1[voice];"
            "[1:a][voice]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0[a]",
            "-map", "[a]",
        ]
    else:
        cmd += ["-map", "1:a:0"]
    hold_filter = f"tpad=stop_mode=clone:stop_duration={hold:.3f}," if hold else ""
    _run_ffmpeg(cmd + [
        "-map", "0:v:0", "-vf", f"{hold_filter}{_subtitle_filter(ass)},scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
        "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
        "-c:a", "aac", "-ar", "48000", "-ac", "1", "-movflags", "+faststart", str(path),
    ], stage="inner_boss_scene")


def render_master(clips: list[Path], music: Path | None, output: Path) -> None:
    """Concatenate approved scene renders; put the optional user's track on top once."""
    from .post_production import _concat_segments, _resolve_ffmpeg, _run_ffmpeg

    ffmpeg = _resolve_ffmpeg()
    playlist = output.with_suffix(".txt")
    playlist.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    dry = output.with_name(output.stem + "-dry.mp4") if music else output
    _concat_segments(ffmpeg=ffmpeg, list_path=playlist, dest_path=dry)
    if music is None:
        return
    _run_ffmpeg([
        ffmpeg, "-y", "-i", str(dry), "-i", str(music), "-filter_complex",
        "[1:a:0]volume=0.20,apad[music];"
        "[0:a:0][music]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0[a]",
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
        "-movflags", "+faststart", str(output),
    ], stage="inner_boss_master_music")
