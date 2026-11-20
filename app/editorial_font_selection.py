"""Approved November title layouts. Individual words reveal in full-phrase coordinates."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageFilter

from .editorial_approved import H, W, rectangles

REVISION = '2026-11-20'
FONT_DIR = Path(__file__).resolve().parents[1] / 'fonts'
ASSETS = FONT_DIR / 'editorial_selected'


@lru_cache(maxsize=1)
def selection():
    return json.loads((ASSETS / 'selection.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=16)
def font_payload(key):
    spec = selection()['fonts'][key]
    path = FONT_DIR / spec['file']
    if not path.is_file():
        raise FileNotFoundError(f"Для заголовка нужен {spec['name']}. Установите файл {path}.")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != spec['sha256']:
        raise ValueError(f"Версия {spec['name']} отличается от утверждённой: {path}")
    return [base64.b64encode(content).decode('ascii'), spec.get('weight')]


# Canvas shapes each whole string with its actual font, including contextual Cyrillic forms.
# All prefixes use the origin and crop of the complete phrase, even when it has a swash.
_SHAPE = """async ({fonts, parts, timed}) => {
    for (const [key, [encoded, weight]] of Object.entries(fonts)) {
        const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
        const face = new FontFace(key, bytes, {weight: weight ? '1 1000' : '400'});
        document.fonts.add(await face.load());
    }
    return parts.map(([key, text]) => {
        const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d');
        const weight = fonts[key][1] || 400;
        const setSize = size => { ctx.font = weight + ' ' + size + 'px ' + key; };
        setSize(210);
        const width = ctx.measureText(text).width;
        const size = Math.min(210, 210 * 4000 / Math.max(width, 1));
        setSize(size);
        const m = ctx.measureText(text);
        const x = Math.ceil(m.actualBoundingBoxLeft) + 6;
        const y = Math.ceil(m.actualBoundingBoxAscent) + 6;
        canvas.width = Math.max(1, x + Math.ceil(m.actualBoundingBoxRight) + 6);
        canvas.height = Math.max(1, y + Math.ceil(m.actualBoundingBoxDescent) + 6);
        setSize(size);
        ctx.fillStyle = 'white';
        let prefix = '';
        const words = timed ? text.split(' ') : [text];
        return words.map(word => {
            prefix += (prefix ? ' ' : '') + word;
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.fillText(prefix, x, y);
            return canvas.toDataURL().split(',')[1];
        });
    });
}"""


def _shape_in_browser(parts, timed):
    from playwright.sync_api import sync_playwright

    payload = {key: font_payload(key) for key, _ in parts}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=['--disable-background-networking'])
        try:
            groups = browser.new_page().evaluate(_SHAPE, {'fonts': payload, 'parts': parts, 'timed': timed})
        finally:
            browser.close()
    return {part: tuple(base64.b64decode(item) for item in group) for part, group in zip(parts, groups)}


@lru_cache(maxsize=24)
def _shaped_parts(parts, timed):
    # The picker can run from an asyncio handler. Playwright's sync API needs its own thread.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(_shape_in_browser, parts, timed).result()


def _alpha(png):
    with Image.open(io.BytesIO(png)) as image:
        return image.getchannel('A')


def _case(text, reference):
    if reference.isupper():
        return text.upper()
    if reference[0].islower() and text.casefold() == reference.casefold():
        return reference
    return text


def _box(sid, record, role, text, reference_title, foreground):
    if sid == '1' and not reference_title:
        return ((40, 600, 680, 790), (50, 800, 230, 860), (40, 875, 680, 1110))[role]
    if sid == '3' and not reference_title and role == 2:
        return (40, 815, 680, 1020)
    left, top, right, bottom = record['box']
    if (sid in ('3', '9') and role == 0 and not reference_title and not foreground
            and sum(ch.isalpha() for ch in text) <= 3):
        # In the depth styles a short centered pronoun can disappear behind the face.
        # Give it a readable column in the left margin of the composition.
        return 40, top, 208, bottom
    if text != record['text'] and len(text) > len(record['text']):
        if role == 1:
            center = (left + right) // 2
            left, right = max(24, center - 110), min(W - 24, center + 110)
        else:
            left, right = 40, W - 40
    return left, top, right, bottom


def _size_and_position(mask, full, box, tilt, fit):
    if tilt:
        rotated = full.rotate(tilt, Image.Resampling.BICUBIC, expand=True)
        crop = rotated.getbbox() or (0, 0, 1, 1)
        mask = mask.rotate(tilt, Image.Resampling.BICUBIC, expand=True).crop(crop)
        full = rotated.crop(crop)
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    ratio = min(width / full.width, height / full.height)
    size = (max(1, round(full.width * ratio)), max(1, round(full.height * ratio)))
    if fit == 'reference':
        # The selected script and display faces have different optical proportions.
        # The reference word has an art-directed width and cap height in each language.
        size = width, height
    elif fit == 'tall':
        # Keep shorter Russian headlines visible above the subject's hair. Limit
        # stretching for long semantic parts whose width already fills the frame.
        size = size[0], min(height, round(size[1] * 1.65))
    position = (left + (width - size[0]) // 2, top + (height - size[1]) // 2)
    return mask.resize(size, Image.Resampling.LANCZOS), position


@lru_cache(maxsize=16)
def layers(sid, lang, parts, role, visible=None, foreground=False):
    style = selection()['styles'][sid]
    records = style[lang]
    prepared = [(r, _case(' '.join(t.split()), records[r]['text'])) for r, t in parts]
    text = dict(prepared)[role]
    record = records[role]
    if not text or visible == 0:
        return ()
    needed = tuple((style['fonts'][r], t) for r, t in prepared if t)
    frames = _shaped_parts(needed, visible is not None)[(style['fonts'][role], text)]
    full = _alpha(frames[-1])
    crop = full.getbbox() or (0, 0, 1, 1)
    full = full.crop(crop)
    mask = full if visible is None or visible >= len(frames) else _alpha(frames[visible - 1]).crop(crop)
    reference_title = all(t == records[r]['text'] for r, t in prepared) and len(prepared) == 3
    box = _box(sid, record, role, text, reference_title, foreground)
    fit = 'contain'
    if reference_title and (sid == '3' or (role == 0 and sid in ('1', '9'))):
        fit = 'reference'
    elif role == 0 and sid == '9' and lang == 'ru':
        fit = 'tall'
    mask, (x, y) = _size_and_position(mask, full, box, 9 if sid == '1' and role == 0 else 0, fit)
    face = Image.new('L', (W, H))
    face.paste(mask, (x, y))
    result = []
    if sid == '1' and role == 0:
        result.append(('#36251F', face.filter(ImageFilter.MaxFilter(3))))
    result.append((record['color'], face))
    return tuple(result)


@lru_cache(maxsize=192)
def drawings(sid, lang, parts, role, visible, foreground=False):
    return tuple((color, rectangles(mask)) for color, mask in layers(sid, lang, parts, role, visible, foreground))
