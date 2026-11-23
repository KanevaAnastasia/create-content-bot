"""Series-scoped visual and audio settings; never mutate global font defaults."""
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

HOLIDAY_THEME = "grandma_christmas"
THEME_KEY = "series_theme"
_active_theme = ContextVar("series_render_theme", default="")
HOLIDAY_DIRECTION = (
    "SERIES ART DIRECTION (visual only, never spoken): In EVERY scene and every start/end frame, "
    "Keep the same room decorated with a Soviet-era multicoloured electric garland, a Christmas tree decorated with "
    "vintage Soviet glass ornaments, and a window with falling snow outside. "
    "Use consistent placement of the same decorations throughout Grandma's kitchen. "
    "Show only the portions of these decorations that naturally fit behind the close foreground action. Never widen the camera to fit the whole tree, window or room into every shot. "
    "Preserve the recipe actions and characters. Russian series: Новогодние рецепты; "
    "English series: Christmas Recipes. Do not generate lettering inside images."
)


def theme_from_session(session):
    series = session.get("series") or {}
    return series.get(THEME_KEY, "") if isinstance(series, dict) else ""


def themed_fonts(func):
    """Scope both font measurement and ASS output to this render, including concurrent renders."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        style = kwargs.get("title_style") or {}
        token = _active_theme.set(style.get(THEME_KEY, ""))
        try:
            return func(*args, **kwargs)
        finally:
            _active_theme.reset(token)
    return wrapped


def holiday_font(kind, language):
    if _active_theme.get() != HOLIDAY_THEME or language not in {"ru", "en"}:
        return None
    return ("SnowCapCyr", "snowcapcyr.otf") if kind == "dish" else ("Pudelinka", "pudelinka.ttf")


def holiday_music(style):
    if (style or {}).get(THEME_KEY) != HOLIDAY_THEME:
        return None
    path = Path(__file__).resolve().parents[1] / "assets/music/17-melodija.mp3"
    if not path.is_file():
        raise FileNotFoundError(f"Series music is missing: {path}")
    return str(path), 0.5


def holiday_music_filter(path):
    """Level the quiet holiday recording before applying background gain."""
    expected = Path(__file__).resolve().parents[1] / "assets/music/17-melodija.mp3"
    if path and Path(path).resolve() == expected.resolve():
        return "loudnorm=I=-16:TP=-2:LRA=7,"
    return ""


def holiday_dish_color(style):
    """Keep chosen colours, replacing white/near-white with festive red."""
    import re
    value = str((style or {}).get("title_color") or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        channels = [int(value[i:i + 2], 16) for i in (1, 3, 5)]
        if min(channels) < 220:
            return value
    return "#cd2b34"
