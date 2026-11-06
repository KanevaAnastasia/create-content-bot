Typography reference review v3 — offline development tools, not bot runtime.

Inputs remain in local artifacts/editorial_titles: eight_real_drafts originals,
reference_fonts, style1_letters_v3, eight_styles_v2/video_frames and style_1
approved overlays, timed_title RU/EN audio and word timing JSON. These review
assets and third-party font files are intentionally not committed.
Dependencies: Pillow, fontTools; project ffmpeg resolver. The existing local
fontTools installation is /tmp/advice-font-tools; a normal installation also works.
Run from /opt/content_fabric, in order:
  .venv/bin/python scripts/typography_review_v3/rebuild_reference.py
  .venv/bin/python scripts/typography_review_v3/build_architectural_font.py
  .venv/bin/python scripts/typography_review_v3/build_unicode_fonts.py
  .venv/bin/python scripts/typography_review_v3/finalize_fonts.py
  .venv/bin/python scripts/typography_review_v3/build_delivery.py --video
  .venv/bin/python scripts/typography_review_v3/validate_delivery.py

Reference word outlines in review_v4/fonts are measurement assets, not full
alphabets. Actual adapted alphabets are in review_v4/unicode_fonts. Style 1's
approved first title keeps its previous contours; other phrases use font letters.
Every transformed glyph is decomposed before transforming: retaining components
would apply scale/shear twice when their shared bases are transformed as well.
Contextual swashes affect single initial/final letters, not complete words.

Visual comparison remains required. Unicode coverage and successful video decode
do not prove an exact stylistic match to the generated sketch. These fonts and
the shot-specific foreground mask are not installed in the Telegram bot.

Revision 4: tighter/heavier style 5, directional stroke contrast in style 9,
connected-component body glyph extraction in styles 6/10. Outputs: review_v4.

The before/after comparison additionally reads retained review_v3 rendered images.
