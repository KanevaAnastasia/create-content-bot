from pathlib import Path
import subprocess

from app.ai_approval_service import _resolve_ffmpeg
from app.post_production import _normalize_segment


def main() -> None:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")
    video = Path("/opt/content_fabric/artifacts/vertex_video_probe_default_region_after_fix.mp4")
    outdir = Path("/opt/content_fabric/outputs/test_merge_debug")
    outdir.mkdir(parents=True, exist_ok=True)
    norm = outdir / "norm.mp4"
    _normalize_segment(ffmpeg=ffmpeg, src_path=video, dest_path=norm, speed_coef=1.2)
    sub = outdir / "subtitles.srt"
    sub.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\nТест субтитров для оценки стиля\n",
        encoding="utf-8",
    )
    subtitle_filter = (
        f"subtitles={str(sub)}:"
        "force_style='FontName=Arial,Fontsize=22,Bold=1,PrimaryColour=&H00FFFFFF&,"
        "OutlineColour=&H000000&,BorderStyle=3,Outline=3,Shadow=0,MarginV=160,Alignment=2'"
    )
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
        f"[0:v]{subtitle_filter}[v];[1:a]volume=0.1800[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[a]",
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
