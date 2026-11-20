import pytest


@pytest.fixture
def title_event_ink(monkeypatch):
    """Exercise ASS timing and compositing without needing desktop fonts in CI."""
    from app import editorial_font_selection

    def drawings(sid, lang, parts, role, visible, *, foreground):
        color = editorial_font_selection.selection()['styles'][sid][lang][role]['color']
        return ((color, 'm 0 0 l 8 0 8 8 0 8'),)

    monkeypatch.setattr(editorial_font_selection, 'drawings', drawings)
