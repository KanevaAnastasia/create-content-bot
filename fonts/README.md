Place subtitle fonts here for FFmpeg/libass rendering.

Recommended files:
- Inter SemiBold (`Inter-SemiBold.ttf` or `.otf`)
- Noto Sans fallback (`NotoSans-Regular.ttf` or `.otf`)

`app.post_production` passes this directory to FFmpeg as `fontsdir` and uses
`Inter SemiBold` when available, then `Noto Sans` when available.
