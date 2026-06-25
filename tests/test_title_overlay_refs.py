from __future__ import annotations

from pathlib import Path

from PIL import Image

from scripts import render_title_overlay_refs as refs


EXPECTED_REFERENCE_FILES = {
    "ru_single_short.png",
    "ru_single_long.png",
    "en_single_short.png",
    "en_single_long.png",
    "ru_series_short.png",
    "ru_series_long.png",
    "en_series_short.png",
    "en_series_long.png",
    "ru_single_borsch.png",
    "en_single_borscht.png",
    "ru_series_borsch.png",
    "en_series_borscht.png",
    "contact_sheet.png",
}


def test_title_overlay_reference_helper_renders_expected_files(monkeypatch, tmp_path):
    monkeypatch.setattr(refs, "OUTPUT_DIR", tmp_path)

    refs.main()

    names = {path.name for path in tmp_path.iterdir() if path.suffix == ".png"}
    assert names == EXPECTED_REFERENCE_FILES
    for name in EXPECTED_REFERENCE_FILES:
        with Image.open(tmp_path / name) as image:
            if name == "contact_sheet.png":
                assert image.size == (refs.WIDTH, 1548)
            else:
                assert image.size == (refs.WIDTH, refs.HEIGHT)
    timing = (tmp_path / "title_timing.txt").read_text(encoding="utf-8")
    assert "title_overlay_start_sec=0.00" in timing
    assert "title_overlay_end_sec=6.00" in timing
    assert "scope=full_first_video_segment" in timing


def test_title_overlay_fonts_are_present_and_named():
    expected = {
        "ru_title": "TD Afisha",
        "en_title": "Taller",
        "ru_series": "Berezka",
        "en_series": "Ravsten Demo",
        "day_counter": "Marianna",
    }

    for key, family in expected.items():
        spec = refs.FONT_SPECS[key]
        assert Path(spec.path).exists()
        font = refs._load_font(spec, 42)
        assert font.getname()[0] == family


def test_title_overlay_timing_covers_full_first_video():
    assert refs.title_overlay_timing(8.0) == {"start_sec": 0.0, "end_sec": 8.0}
