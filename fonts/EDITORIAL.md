# Current approved titles

Styles 1, 2, 3, 5 and 9 use `editorial_selected/selection.json` for every render.
It records the typeface, weight, SHA-256, bounding box and ink color for each role.
The picker records the revision for reference. Previously saved drafts use
their selected style number with the current fonts when previewed or rebuilt.

Both the preview and the final video use the same ASS paths. For every title,
including «Почему не ты?», the renderer loads the actual font files into Chromium,
checks their hashes, and shapes each semantic part. The 30 comparison specimens
in `tests/fixtures/editorial_letters/` are for verification. Style 3 pairs Bebas
Neue Bold and Cormorant Garamond SemiBold Italic with bright pink SignPainter
HouseScript. Style 9
uses TD Ciryulnik, Cormorant Garamond SemiBold Italic and The Youngest Script.
Both use the letterforms without decorative underlines.
On a new render host, provision the manifest entries in
`fonts/editorial_local/` from the licensed desktop collection and install
Playwright Chromium. The original Manrope and Cormorant Garamond variable fonts
use weights 700 and 600. Their licenses are in `CF-Manrope-OFL.txt` and
`CF-Cormorant-OFL.txt`.
See `docs/discipline-formats.md` for timing and automatic foreground compositing.

## Visual review

Send the real RU and EN frames to the project chat «Ревью оформления заголовков»
for review when changing the look of a title. Supply the matching early sketch
from `artifacts/editorial_titles/eight_real_drafts/`, the selected lettered
variant from `font_research_nov20/reference-match/` in the same archive, and
the production render for both the short title and a longer phrase.
The reviewer compares the lettering, color, baseline, spacing and subject overlap
on the images, verifies the source file, face, checksum and glyph coverage, and
types additional Russian and English phrases through the renderer. Record the
source of each file (download, project archive or installed collection) and the
review decision for each style. Submit a new render for a
style the reviewer returns for corrections.

# Legacy presets (kept for older drafts)

# Discipline title fonts

The eight presets (1, 2, 3, 5, 6, 7, 9, 10) use local static fonts so preview
cards and final ASS rendering agree. Both Russian (including Ё/ё) and English
alphabets were checked in each font's cmap.

Source: https://github.com/google/fonts/tree/main/ofl

| Local family | Upstream folder | Instance |
| --- | --- | --- |
| CF Oswald | oswald | weight 600 |
| CF Manrope | manrope | weight 700 |
| CF Montserrat | montserrat | weight 800 |
| CF Cormorant | cormorantgaramond | weight 600, upright |
| CF CormorantItalic | cormorantgaramond | weight 600, italic |
| CF Oranienbaum | oranienbaum | Regular |
| CF Caveat | caveat | weight 700 |
| CF MarckScript | marckscript | Regular |

Variable fonts were instantiated with fontTools.varLib.instancer; local family,
full and PostScript names use a CF prefix to avoid collisions with system fonts.
Original copyright records are preserved. Adjacent CF-*-OFL.txt files contain
upstream licenses. These are reproducible font-based interpretations of the
approved image mockups, not fonts extracted from generated pictures.

`app/editorial_titles.py` holds layout, colors, fitting, and shared ASS events.
The title is segmented semantically into three units, preserving its words.
One- and two-word titles use one or two units. The default full-frame black scrim
is 23% opaque and lasts only as long as the title cue. These presets currently
show the full composition during that cue; word-level reveal is a separate step.
