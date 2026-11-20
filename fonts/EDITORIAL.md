# Current approved titles

New selections of styles 1, 2, 3, 5 and 9 use `editorial_selected/selection.json`.
It records the typeface, weight, SHA-256, bounding box and ink color for each role.
The picker stores the revision in the draft. Drafts saved before this revision
use their existing artwork in `editorial_approved/`.

Both the preview and the final video use the same ASS paths. For the reference
title, the paths come from the saved lettering masks. For other text, the renderer
loads the exact font files in local Playwright Chromium and shapes each semantic
part. Style 3 uses the bright pink lettering from its original sketch. Style 9
uses TD Ciryulnik, Cormorant Garamond SemiBold Italic and The Youngest Script.
Both use the letterforms without decorative underlines.
On a new render host, provision the manifest entries in
`fonts/editorial_local/` from the licensed desktop collection and install
Playwright Chromium. The renderer checks each file against its recorded hash.
See `docs/discipline-formats.md` for timing and automatic foreground compositing.

## Visual review

Send the real RU and EN frames to the project chat «Ревью оформления заголовков»
for review when changing the look of a title. Supply the matching early sketch
from `artifacts/editorial_titles/eight_real_drafts/`, the selected lettered
variant from `font_research_nov20/reference-match/` in the same archive, and
the production render for both the short title and a longer phrase.
The reviewer compares the lettering, color, baseline, spacing and subject overlap
on the images and records a decision for each style. Submit a new render for a
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
