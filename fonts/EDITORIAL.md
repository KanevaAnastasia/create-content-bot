# Current approved titles

Styles 1/2/3/5/9 now use the frozen `editorial_approved/` assets via
`app/editorial_approved.py`. These override the legacy ASS font presets below.
See `docs/discipline-formats.md` for timing and automatic foreground compositing.

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
