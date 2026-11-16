"""Build the white snow-cap companion to the user-supplied SnowCapCyr font.

Run with fontTools installed. Runtime rendering only needs the generated font.
Preserves advances and vertical metrics so both ASS layers align exactly.
"""
from pathlib import Path

from fontTools.pens.areaPen import AreaPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]


def build():
    font = TTFont(ROOT / 'fonts/snowcapcyr.otf', recalcBBoxes=False)
    glyphs = font.getGlyphSet()
    replacements = {}
    original_bounds = {}
    for name in font.getGlyphOrder():
        glyph = font["glyf"][name]
        glyph.recalcBounds(font["glyf"])
        original_bounds[name] = tuple(getattr(glyph, key, 0) for key in ("xMin", "yMin", "xMax", "yMax"))
    for name in font.getGlyphOrder():
        source = DecomposingRecordingPen(glyphs)
        glyphs[name].draw(source)
        output = TTGlyphPen(None)
        contour = RecordingPen()
        for operation in source.value:
            contour.value.append(operation)
            if operation[0] not in {'closePath', 'endPath'}:
                continue
            bounds, area = BoundsPen(glyphs), AreaPen(glyphs)
            contour.replay(bounds)
            contour.replay(area)
            # Positive contours are transparent pockets. Upper snow caps sit
            # above y=580 in this 1000-unit font; letter counters stay empty.
            if bounds.bounds and area.value > 0 and bounds.bounds[1] >= 580 and bounds.bounds[3] >= 700:
                contour.replay(output)
            contour = RecordingPen()
        # Zero-area contours preserve the original bounding box without adding
        # visible marks. Both layers retain the same advances and font metrics.
        x0, y0, x1, y1 = original_bounds[name]
        if x1 > x0:
            for point in ((x0, y0), (x1, y1)):
                output.moveTo(point)
                output.lineTo(point)
                output.closePath()
        replacements[name] = output.glyph()
    for name, glyph in replacements.items():
        glyph.recalcBounds(font['glyf'])
        font['glyf'][name] = glyph
    for record in font['name'].names:
        value = {1: 'CF SnowCap Snow', 2: 'Regular', 3: 'CF-SnowCap-Snow-1',
                 4: 'CF SnowCap Snow', 6: 'CF-SnowCap-Snow',
                 16: 'CF SnowCap Snow', 17: 'Regular'}.get(record.nameID)
        if value:
            record.string = value.encode(record.getEncoding())
    font.save(ROOT / 'fonts/CF-SnowCap-Snow.ttf')


if __name__ == '__main__':
    build()
