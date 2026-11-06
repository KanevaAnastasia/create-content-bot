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
The reference-video foreground mask is deliberately not applied to new footage:
automatic text-behind-character compositing is not implemented for arbitrary
shots. Styles 3/9 retain their typography but currently render in the foreground.

Verification: mode selection/resume/reset, scenario/refinement prompts, independent
RU/EN voice and word timing, old intro and ordinary Story regression tests,
font asset hashes, and real ffmpeg preview rendering. No new paid generation is
required for these checks.
