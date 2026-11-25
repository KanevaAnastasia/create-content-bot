import pytest


@pytest.fixture
def title_event_ink(monkeypatch):
    """Exercise ASS timing and compositing without needing desktop fonts in CI."""
    from app import editorial_font_selection

    def drawings(sid, lang, parts, role, visible, *, foreground, hierarchy=False):
        color = editorial_font_selection.selection()['styles'][sid][lang][role]['color']
        return ((color, 'm 0 0 l 8 0 8 8 0 8'),)

    monkeypatch.setattr(editorial_font_selection, 'drawings', drawings)


@pytest.fixture(autouse=True)
def isolated_openai_usage(monkeypatch, tmp_path):
    """Never write paid usage from mocked API calls into the live ledger."""
    monkeypatch.setenv("OPENAI_USAGE_DB", str(tmp_path / "openai_usage.sqlite3"))
