"""Visual selection of the approved Discipline title presets."""
from __future__ import annotations

import asyncio
import io
import re

from PIL import Image, ImageDraw, ImageFont
from telegram import ReplyKeyboardMarkup

from ..editorial_titles import (
    DEFAULT_DIM,
    FONT_DIR,
    HIERARCHY_KEY,
    PARTS_KEY,
    STYLE_IDS,
    STYLES,
    STYLE_KEY,
    propose_parts,
    valid_parts,
)
from ..editorial_font_selection import REVISION
from ..post_production import render_title_overlay_preview
from ..series_store import SERIES_COUNTER_BY_LANG_SESSION_KEY, SERIES_TITLE_BY_LANG_SESSION_KEY
from .handlers_generation_utils import OVERLAY_TITLE_BY_LANG_SESSION_KEY, OVERLAY_TITLE_STYLE_SESSION_KEY


def picker_markup(speaker=False):
    return ReplyKeyboardMarkup([
        ['Стиль 1', 'Стиль 2', 'Стиль 3'], ['Стиль 5', 'Стиль 9'], ['Все стили', 'ОК'], ['В начало'],
    ] + ([['Говорит верхний', 'Говорит нижний']] if speaker else []), resize_keyboard=True)


def render_card(titles: dict, style: dict, background_bytes: bytes) -> bytes:
    """Use the production ASS renderer, not AI images, for every selectable card."""
    languages = [lang for lang in ('ru', 'en') if titles.get(lang)]
    width, height = 540, 960
    card = Image.new('RGB', (width * len(languages), height + 82), '#eee8dc')
    draw = ImageDraw.Draw(card)
    font = ImageFont.truetype(str(FONT_DIR / 'CF-Manrope.ttf'), 20)
    style_id = str(style[STYLE_KEY])
    draw.text((20, 14), f'{style_id}. {STYLES[style_id].name}', font=font, fill='#24282a')
    for index, language in enumerate(languages):
        background = Image.open(io.BytesIO(background_bytes)).convert('RGB').resize((width, height))
        data = render_title_overlay_preview(background, language=language, title=titles[language], title_style={**style, "word_timing_by_lang": {}})
        card.paste(Image.open(io.BytesIO(data)), (index * width, 82))
        draw.text((index * width + 20, 48), language.upper(), font=font, fill='#42484a')
    stream = io.BytesIO()
    card.save(stream, format='PNG')
    return stream.getvalue()


async def _send_card(update, titles, style, session):
    from .handlers_state_scene import safe_reply_photo
    from .handlers_state_scene import _overlay_first_frame
    data = await asyncio.to_thread(render_card, titles, style, _overlay_first_frame(session))
    stream = io.BytesIO(data)
    stream.name = f'editorial_style_{style[STYLE_KEY]}.png'
    await safe_reply_photo(update.message, photo=stream, caption=f'Стиль {style[STYLE_KEY]} · {STYLES[str(style[STYLE_KEY])].name}')


async def ask_picker(update, session, *, all_styles=True):
    titles = {lang: str(value) for lang, value in (session.get(OVERLAY_TITLE_BY_LANG_SESSION_KEY) or {}).items()
              if lang in {'ru', 'en'} and str(value).strip()}
    if not titles:
        return False
    style = dict(session.get(OVERLAY_TITLE_STYLE_SESSION_KEY) or {})
    from ..discipline_modes import ARCHIVE, active_mode, TWO_LIVES, FORK
    if active_mode(session) in (TWO_LIVES, FORK):
        style.setdefault('speaker_panel', 'top')
    hierarchy = {}
    if active_mode(session) == ARCHIVE:
        series_titles = session.get(SERIES_TITLE_BY_LANG_SESSION_KEY) or {}
        series_counters = session.get(SERIES_COUNTER_BY_LANG_SESSION_KEY) or {}
        for lang, title in titles.items():
            series_title = str(series_titles.get(lang) or series_titles.get('ru') or '').strip()
            series_counter = str(series_counters.get(lang) or series_counters.get('ru') or '').strip()
            if series_title and series_counter:
                hierarchy[lang] = [title, series_title, series_counter]
    if hierarchy:
        style[HIERARCHY_KEY] = hierarchy
        parts = style.get(PARTS_KEY) or {}
    else:
        style.pop(HIERARCHY_KEY, None)
        parts = style.get(PARTS_KEY) or {}
        if not all(valid_parts(title, parts.get(lang)) for lang, title in titles.items()):
            parts = await propose_parts(titles)
    style.update({'editorial_font_revision': REVISION, PARTS_KEY: parts, 'dim_opacity': style.get('dim_opacity', DEFAULT_DIM)})
    session[OVERLAY_TITLE_STYLE_SESSION_KEY] = style
    await update.message.reply_text('Выберите оформление заголовка. На карточках ваш текст; фон — первый сгенерированный кадр.')
    for style_id in STYLE_IDS if all_styles else (str(style.get(STYLE_KEY) or '1'),):
        await _send_card(update, titles, {**style, STYLE_KEY: style_id}, session)
    if hierarchy:
        explanation = 'Три уровня заголовка:\n' + '\n'.join(
            f'{lang.upper()}: ' + ' / '.join(value) for lang, value in hierarchy.items()
        )
    else:
        explanation = (
            'Смысловые части:\n' + '\n'.join(f'{lang.upper()}: ' + ' | '.join(value) for lang, value in parts.items())
            + '\nМожно исправить: RU: У меня | нет | энергии (или EN: I have | no | energy).'
        )
    await update.message.reply_text(
        'Нажмите номер стиля. ' + explanation
        + '\nЗатемнение: «затемнение 32%». После выбора нажмите «ОК».',
        reply_markup=picker_markup(style.get('speaker_panel') in ('top', 'bottom')),
    )
    return True


async def handle_picker_input(update, session, text):
    """Return True if consumed; False means continue the existing approval path."""
    low = text.strip().lower()
    style = dict(session.get(OVERLAY_TITLE_STYLE_SESSION_KEY) or {})
    titles = session.get(OVERLAY_TITLE_BY_LANG_SESSION_KEY) or {}
    if low in {'ок', 'ok', 'okay', 'approve'}:
        if str(style.get(STYLE_KEY)) not in STYLE_IDS:
            await update.message.reply_text('Сначала выберите стиль по номеру.', reply_markup=picker_markup(style.get('speaker_panel') in ('top', 'bottom')))
            return True
        return False
    if low == 'все стили':
        return await ask_picker(update, session)
    choice = re.fullmatch(r'(?:стиль\s*)?(1|2|3|5|9)', low)
    if choice:
        style[STYLE_KEY] = choice.group(1)
        style['editorial_font_revision'] = REVISION
    elif '|' in text:
        match = re.match(r'\s*(RU|EN)\s*:\s*(.*)', text, re.I | re.S)
        language, body = (match.group(1).lower(), match.group(2)) if match else ('ru', text)
        parts = [part.strip() for part in body.split('|')]
        if not valid_parts(str(titles.get(language) or ''), parts):
            await update.message.reply_text('Разделите исходный заголовок на 3 части, сохранив все слова и их порядок. Для короткого заголовка можно 1–2 части.')
            return True
        style[PARTS_KEY] = {**(style.get(PARTS_KEY) or {}), language: parts}
    else:
        dim = re.fullmatch(r'затемнение\s+(\d{1,2})\s*%', low)
        if not dim or not 0 <= int(dim.group(1)) <= 60:
            await update.message.reply_text('Выберите стиль, измените смысловые части через | или напишите «затемнение 32%».', reply_markup=picker_markup(style.get('speaker_panel') in ('top', 'bottom')))
            return True
        style['dim_opacity'] = int(dim.group(1)) / 100
    session[OVERLAY_TITLE_STYLE_SESSION_KEY] = style
    if style.get(STYLE_KEY):
        await _send_card(update, titles, style, session)
        await update.message.reply_text('Если оформление подходит — «ОК». Можно выбрать другой стиль.', reply_markup=picker_markup(style.get('speaker_panel') in ('top', 'bottom')))
    else:
        await ask_picker(update, session)
    return True
