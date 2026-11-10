"""Style references with matching captions and choice labels."""
import logging
from pathlib import Path
from .story_generation import STORY_VISUAL_STYLES

logger = logging.getLogger(__name__)
PREVIEW_BUTTON = "Показать примеры"
NEXT_BUTTON = "Дальше →"
PREVIOUS_BUTTON = "← Назад"
PAGE_KEY = "story_style_preview_page"
ROOT = Path(__file__).resolve().parent.parent
DESCRIPTIONS = {'Сухая акварель': 'мягкая рисованная фактура и лёгкая сепия.',
 'Ретро-мультфильм': 'угловатая графика, плоские формы, оливково-горчично-коралловая палитра.',
 'Книжная гуашь': 'тёплая фактурная иллюстрация из детской книги.',
 'Пластилиновый мультфильм': 'объёмные пластилиновые герои и миниатюрные декорации.',
 'Кукольный стоп-моушен': 'мягкие войлочные куклы, ткань и ручная фактура.',
 'Рисованная ретро-сказка': 'гуашевая мультипликация в духе старых рисованных сказок.',
 'Сумеречная акварель': 'глубокие синие тени, бумажная фактура и тёплый свет лампы.',
 'Светлая анимационная пастель': 'чистая мягкая 2D-анимация в солнечной пастельной гамме.',
 'Классический рисованный мультфильм': 'выразительный контур, плоский цвет и живая мультипликация.',
 'Фетровая аппликация': 'весь кадр собран из фетра, ткани и декоративных швов.',
 'Народная рисованная сказка': 'гуашь, деревянный быт и славянский растительный орнамент.',
 'Пастельные мелки': 'яркие живые штрихи масляной пастели на белой бумаге.',
 'Вязаная иллюстрация': 'весь кадр связан из пряжи с видимыми петлями и узелками.',
 'Минималистичный ретро-мультфильм': 'тонкий контур, светлый фон и лаконичная оранжево-бирюзовая '
                                     'палитра.'}


def catalog():
    return [(key, style) for key, style in STORY_VISUAL_STYLES.items() if style.get("reference_path")]


def page_items(session=None):
    return [(i, key, style) for i, (key, style) in enumerate(catalog(), 1)]


def choice_label(number, style):
    return f"{number}. {style['label']}"


def resolve_choice(text):
    # Global numbering also accepts a button from an earlier page.
    value = str(text or "").strip()
    for i, (_, style) in enumerate(catalog(), 1):
        if value in {str(i), choice_label(i, style)}:
            return style['label']
    return value


def page_rows(session=None):
    rows = [[choice_label(number, style)] for number, _, style in page_items(session)]
    return rows + [["Основной"], [PREVIEW_BUTTON]]


def page_description(session=None):
    lines = ["Выберите стиль кадров ролика.",
             "«Показать примеры» — все 14 картинок с номером, названием и описанием. Выберите одноимённую кнопку.", ""]
    for number, _, style in page_items(session):
        lines.append(f"{choice_label(number, style)} — {DESCRIPTIONS[style['label']]}")
    lines.extend(["", "Основной — сохранить внешний вид персонажей проекта.",
                  "Выбранная стилистика будет единой для всего ролика."])
    return "\n".join(lines)


async def send_story_style_previews(message, session=None) -> bool:
    sender = getattr(message, "reply_photo", None)
    if not callable(sender):
        return False
    complete = True
    for number, _, style in page_items(session):
        path = ROOT / style['reference_path']
        try:
            await sender(photo=path.read_bytes(),
                         caption=f"{choice_label(number, style)}\n{DESCRIPTIONS[style['label']]}",
                         read_timeout=60, write_timeout=60)
        except Exception:
            complete = False
            logger.exception("story_style_preview_delivery_failed style=%s", style['label'])
    return complete
