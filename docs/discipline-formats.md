# Discipline formats and approved title presets

Approved on 2026-11-07: styles **1, 2, 3, 5, 9**, revision `review_v4`.
Styles 6/7/10 remain available only as old draft data, not selectable for new titles.
`fonts/editorial_approved/approval.json` pins the fonts, contextual alternates,
geometry and original title outlines by SHA-256. Local review media is retained
in `artifacts/editorial_titles/approved_2026-11-07/`.

After entering source text in Discipline, choose:

- **О Васе**: third-person male narration beginning «Это Вася, и он…», an observable
  problem and contradiction, a connecting question, and small practical steps.
- **А что если…**: spoken first-person problem as the title, elaboration, a possible
  alternative explanation introduced with «А что если…», and practical steps.

Both accept arbitrary topics, retain script/audio approval and optional English
translation, and use voice `P1bg08DkjqiVEzOn76yG` for RU and EN. Vasily's dialogue
voice uses that same ID; the ordinary narrator and grandmother's grandson remain
unchanged. New formats skip the optional two-speaker intro by default.

Media uses the existing Story pipeline: ElevenLabs narration and silent Kling
clips based on semantic narration timing. Scene prompts favor concrete visual
explanations and allow object/graphic shots, with no compulsory Knopka quota,
first-person productivity books, or fabricated instant success at the end.

The style picker shows the user's RU/EN words with the saved fonts and geometry,
using the production ASS renderer. The default full-frame dim is 82/255 (32%).
Semantic parts keep fixed positions; individual words reveal at their own
ElevenLabs alignment starts when the headline matches the spoken opening.
Unspoken titles remain static rather than receiving invented speech timing.

Approved drawings are emitted as ASS paths, avoiding font substitution between
preview and final rendering. Original «Почему не ты / Why not you» contours and
ornaments are retained. Other phrases use the approved complete alphabets.
Styles 3/9 automatically place the upper semantic part behind the foreground
character. BiRefNet builds a mask from the actual new footage every half-second;
backward optical flow tracks it on every intermediate frame. A scene cut resets
the mask immediately. The full-frame dim stays below both text and character;
the remaining semantic parts stay in front. Existing reference-shot masks are
never reused for new footage. RU and EN retain their independent word timings.

One-time deployment setup (already performed on this host):
`poetry install`, then `.venv/bin/python scripts/setup_title_depth.py`.
The installer downloads the official BiRefNet General Lite ONNX model into
`artifacts/models/` and verifies its published checksum. Runtime inference is
local and does not send video frames to an external API. Dependencies and the
model are required; failures stop the title render rather than silently changing
the chosen style. Mask videos are atomically cached under
`artifacts/title_depth_masks/` by video content, timing and model version.

Sources: [BiRefNet](https://github.com/ZhengPeng7/BiRefNet) (MIT),
[official rembg model URL and checksum](https://github.com/danielgatis/rembg/blob/main/rembg/sessions/birefnet_general_lite.py).
The setup is tuned for short title cues. Neural segmentation is approximate,
so review the output around fine hair, rapid motion and shot transitions.

Verification: mode selection/resume/reset, scenario/refinement prompts, independent
RU/EN voice and word timing, old intro and ordinary Story regression tests,
font asset hashes, and real ffmpeg preview rendering. No new paid generation is
required for these checks.
