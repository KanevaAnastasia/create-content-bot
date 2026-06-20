from pathlib import Path
import subprocess

from app.ai_approval_service import _resolve_ffmpeg
from app.post_production import (
    BACKGROUND_MUSIC_ATTENUATION_DB,
    DEFAULT_BACKGROUND_MUSIC_VOLUME,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    _apply_db_attenuation,
    _build_burned_subtitle_cues,
    _normalize_segment,
    _subtitle_filter,
    _write_burned_subtitle_ass,
)


def main() -> None:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")
    video = Path("/opt/content_fabric/artifacts/vertex_video_probe_default_region_after_fix.mp4")
    outdir = Path("/opt/content_fabric/outputs/test_merge_debug")
    outdir.mkdir(parents=True, exist_ok=True)
    norm = outdir / "norm.mp4"
    _normalize_segment(ffmpeg=ffmpeg, src_path=video, dest_path=norm, speed_coef=1.2)
    sub = outdir / "subtitles.ass"
    music_volume = _apply_db_attenuation(DEFAULT_BACKGROUND_MUSIC_VOLUME, BACKGROUND_MUSIC_ATTENUATION_DB)
    cues = _build_burned_subtitle_cues(
        {"start_sec": 0.0, "end_sec": 3.0, "text": "Тест субтитров для оценки стиля"},
        video_width=TARGET_WIDTH,
        video_height=TARGET_HEIGHT,
    )
    _write_burned_subtitle_ass(sub, cues, video_width=TARGET_WIDTH, video_height=TARGET_HEIGHT)
    subtitle_filter = _subtitle_filter(sub)
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(norm),
        "-stream_loop",
        "-1",
        "-i",
        "/Users/anastasia/Downloads/Cooking-Show-chosic.com_.mp3",
        "-filter_complex",
        f"[0:v]{subtitle_filter}[v];[1:a]volume={music_volume:.4f}[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(outdir / "test.mp4"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.returncode)
    print(proc.stderr[-6000:])


if __name__ == "__main__":
    main()
