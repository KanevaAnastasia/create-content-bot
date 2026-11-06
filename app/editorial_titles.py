"""Reusable bilingual, three-part title layouts shared by previews and ASS output."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import re
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path(__file__).resolve().parents[1] / 'fonts'
STYLE_IDS = ('1', '2', '3', '5', '6', '7', '9', '10')
STYLE_KEY = 'editorial_style_id'
PARTS_KEY = 'semantic_parts_by_lang'
DEFAULT_DIM = 0.23
# libass measures font size using Windows ascender/descender, Pillow uses em units.
ASS_FONT_SCALE = {'Manrope': 1.366, 'Oranienbaum': 1.155, 'Oswald': 1.702,
                  'Caveat': 1.289, 'MarckScript': 1.249, 'CormorantItalic': 1.38,
                  'Montserrat': 1.562, 'Cormorant': 1.38}


@dataclass(frozen=True)
class PartStyle:
    font: str
    color: str
    size: float
    uppercase: bool = False
    scale_x: int = 100


@dataclass(frozen=True)
class TitleStyle:
    name: str
    parts: tuple[PartStyle, PartStyle, PartStyle]


def p(font, color, size, uppercase=False, scale_x=100):
    return PartStyle(font, color, size, uppercase, scale_x)


CREAM = '#fff0ce'
WHITE = '#fffaf1'
STYLES = {
    '1': TitleStyle('Кисть и плакат', (p('MarckScript','#fa6335',.12), p('Manrope',WHITE,.055), p('Oswald',WHITE,.20,True))),
    '2': TitleStyle('Золотая каллиграфия', (p('MarckScript','#ffd04c',.14), p('CormorantItalic',WHITE,.07), p('Oswald',WHITE,.20,True))),
    '3': TitleStyle('Яркий рукописный акцент', (p('Oswald',WHITE,.15,True), p('CormorantItalic',WHITE,.07), p('Caveat','#ff3971',.16))),
    '5': TitleStyle('Тёплый журнал', (p('CormorantItalic',CREAM,.13,True), p('MarckScript','#e7753c',.09), p('Cormorant',CREAM,.17,True))),
    '6': TitleStyle('Мятная кисть', (p('Caveat','#c8f6ce',.15), p('Manrope',WHITE,.055), p('Oswald',WHITE,.19,True))),
    '7': TitleStyle('Архитектурный', (p('Oranienbaum',CREAM,.24,True,65), p('Caveat',CREAM,.12), p('Montserrat','#ffd456',.18,True,70))),
    '9': TitleStyle('Голубая подпись', (p('Oswald',WHITE,.15,True), p('CormorantItalic',WHITE,.07), p('MarckScript','#58c8fa',.15))),
    '10': TitleStyle('Коралловая кисть', (p('Caveat','#ff7045',.16), p('Manrope',CREAM,.055), p('Oranienbaum',CREAM,.20,True,75))),
}


def normalized(text: str) -> str:
    return ' '.join(str(text).split())


def valid_parts(title: str, parts: object) -> bool:
    return (isinstance(parts, list) and len(parts) == min(3, len(normalized(title).split())) and bool(parts)
            and all(isinstance(x, str) and x.strip() for x in parts)
            and normalized(' '.join(parts)).casefold() == normalized(title).casefold())


def fallback_parts(title: str) -> list[str]:
    """Conservative offline fallback; real proposals use semantic LLM segmentation."""
    words = normalized(title).split()
    if len(words) <= 3:
        return words
    # Keep meaningful negations together instead of cycling fonts word by word.
    for index, word in enumerate(words[1:-1], 1):
        if word.casefold() in {'нет', 'не', 'no', 'not', 'never', 'никогда'}:
            return [' '.join(words[:index]), word, ' '.join(words[index + 1:])]
    a = max(1, len(words) // 3)
    b = max(a + 1, 2 * len(words) // 3)
    return [' '.join(words[:a]), ' '.join(words[a:b]), ' '.join(words[b:])]


async def propose_parts(titles: dict[str, str]) -> dict[str, list[str]]:
    from .pipeline_llm_steps import gemini_text
    from .llm_utils import extract_json_object
    prompt = (
        'Раздели каждый заголовок на три СМЫСЛОВЫХ фрагмента для типографического плаката. '
        'Не дели по равному числу слов и не чередуй шрифты по словам. Сохрани порядок, '
        'все слова и пунктуацию дословно; ничего не добавляй. Для одного/двух слов допустимо '
        'один/два фрагмента. Пример: У меня нет энергии -> ["У меня", "нет", "энергии"]. '
        'I have no energy -> ["I have", "no", "energy"]. Верни JSON {"ru":[...],"en":[...]} '
        'только для переданных языков. Заголовки: ' + json.dumps(titles, ensure_ascii=False)
    )
    try:
        result = extract_json_object(await gemini_text(prompt))
    except Exception:
        result = {}
    return {lang: result[lang] if valid_parts(title, result.get(lang)) else fallback_parts(title)
            for lang, title in titles.items() if str(title).strip()}


@lru_cache(maxsize=256)
def _font(name: str, size: int):
    return ImageFont.truetype(str(FONT_DIR / f'CF-{name}.ttf'), size)


def layout_parts(title: str, language: str, style: dict, width: int, height: int) -> list[dict]:
    preset = STYLES[str(style[STYLE_KEY])]
    by_lang = style.get(PARTS_KEY) or {}
    parts = by_lang.get(language) if isinstance(by_lang, dict) else None
    if not valid_parts(title, parts):
        parts = fallback_parts(title)
    specs = preset.parts if len(parts) == 3 else ((preset.parts[0], preset.parts[2]) if len(parts) == 2 else (preset.parts[2],))
    rows = []
    for text, spec in zip(parts, specs):
        text = normalized(text).upper() if spec.uppercase else normalized(text)
        size = max(14, round(height * spec.size))
        # Fit each semantic unit as a whole, with a bounded font size and no clipping.
        while size > 14:
            bounds = _font(spec.font, size).getbbox(text)
            if (bounds[2] - bounds[0]) * spec.scale_x / 100 <= width * .84:
                break
            size -= 1
        bounds = _font(spec.font, size).getbbox(text)
        rows.append({'text': text, 'font': 'CF ' + spec.font, 'size': size, 'scale_x': spec.scale_x,
                     'color': spec.color, 'ass_size': round(size * ASS_FONT_SCALE[spec.font]),
                     'height': size * 1.12})
    gap = height * .005
    total = sum(row['height'] for row in rows) + gap * max(0, len(rows) - 1)
    cursor = (height - total) / 2
    for row in rows:
        row['y'] = round(cursor + row['height'] / 2)
        cursor += row['height'] + gap
    return rows


def _timestamp(value: float) -> str:
    cs = max(0, round(float(value) * 100))
    return f'{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}'


def _escape(text: str) -> str:
    return text.replace('\\', '／').replace('{', '(').replace('}', ')').replace('\n', ' ')


def ass_events(cue: dict, style: dict, width: int, height: int, base_style: str) -> list[str]:
    """Emit a full-frame scrim and three text units for the cue's exact lifetime."""
    start, end = float(cue.get('start_sec') or 0), float(cue.get('end_sec') or 0)
    if end <= start:
        return []
    def event(layer, text):
        return f'Dialogue: {layer},{_timestamp(start)},{_timestamp(end)},{base_style},,0,0,0,,{text}'
    try:
        opacity = min(.6, max(0, float(style.get('dim_opacity', DEFAULT_DIM))))
    except (ValueError, TypeError):
        opacity = DEFAULT_DIM
    alpha = round(255 * (1 - opacity))
    rows = [event(0, f'{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H000000&\\1a&H{alpha:02X}&\\p1}}m 0 0 l {width} 0 l {width} {height} l 0 {height}{{\\p0}}')]
    for part in layout_parts(str(cue.get('text') or ''), str(cue.get('language') or 'ru'), style, width, height):
        color = part['color'].lstrip('#')
        bgr = color[4:6] + color[2:4] + color[:2]
        tags = (f"{{\\an5\\pos({width//2},{part['y']})\\q2\\fn{part['font']}"
                f"\\fs{part['ass_size']}\\fscx{part['scale_x']}\\fscy100\\b0\\i0\\bord0\\shad0"
                f"\\1a&H00&\\1c&H{bgr}&}}")
        rows.append(event(2, tags + _escape(part['text'])))
    return rows
