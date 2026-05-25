"""End-to-end: generate a still via Vertex, send to Syntax bot, poll, save MP4.

Loads ``.env`` from the project root (via :func:`app.env_utils.load_env_file`).
Requires ``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``, ``VERTEX_IMAGE_MODEL``,
Telethon env (``TELEGRAM_API_ID``, ``TELEGRAM_API_HASH``, session), and Syntax bot username.

Example::

    .venv/bin/python tools/syntax_vertex_e2e.py \\
      --image-prompt "Кухня, бабушка у плиты, вертикальный кадр" \\
      --video-prompt "Бабушка спокойно помешивает смесь в миске, тёплый свет" \\
      --poll-sec 30 \\
      --out /tmp/syntax_run.mp4
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.env_utils import load_env_file
from app.syntax_fallback_output import video_bytes_from_fallback_result
from app.syntax_video_fallback import try_generate_via_syntax
from app.vertex_images import generate_image_vertex


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vertex image -> Syntax video (Telethon).")
    parser.add_argument(
        "--image-prompt",
        default="",
        help="Prompt for Vertex still (9:16 enforced in SDK). Ignored if --from-image is set.",
    )
    parser.add_argument(
        "--from-image",
        default="",
        help="Use existing image file instead of Vertex generation.",
    )
    parser.add_argument(
        "--video-prompt",
        required=True,
        help="Plain text video prompt for Syntax (no JSON).",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output MP4 path (default: ./artifacts/syntax_e2e_<ts>.mp4).",
    )
    parser.add_argument(
        "--save-image",
        default="",
        help="If set, write generated/loaded frame PNG to this path.",
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=None,
        help="Override SYNTAX_FALLBACK_POLL_SEC (clamped 10–120 in transport).",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=None,
        help="Max wait for Syntax reply (default: SYNTAX_FALLBACK_TIMEOUT_SEC or 25 min).",
    )
    parser.add_argument(
        "--download-timeout-sec",
        type=int,
        default=300,
        help="Timeout for final MP4 download from direct/getvideo URL.",
    )
    args = parser.parse_args()

    load_env_file()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("app.syntax_telethon_fallback").setLevel(logging.INFO)
    if args.poll_sec is not None:
        os.environ["SYNTAX_FALLBACK_POLL_SEC"] = str(args.poll_sec)
    if args.timeout_sec is not None:
        os.environ["SYNTAX_FALLBACK_TIMEOUT_SEC"] = str(args.timeout_sec)

    if args.from_image:
        src = Path(args.from_image).expanduser()
        if not src.is_file():
            print(f"Missing image file: {src}", file=sys.stderr)
            return 2
        frame = src.read_bytes()
    else:
        ip = (args.image_prompt or "").strip()
        if not ip:
            print("Provide --image-prompt or --from-image.", file=sys.stderr)
            return 2
        print("Generating still via Vertex…", flush=True)
        frame = generate_image_vertex(ip)

    if args.save_image:
        _write_bytes(Path(args.save_image).expanduser(), frame)
        print(f"Saved frame: {args.save_image}", flush=True)

    timeout_raw = (os.getenv("SYNTAX_FALLBACK_TIMEOUT_SEC") or "").strip()
    timeout_sec: int | None = int(timeout_raw) if timeout_raw.isdigit() else None
    if args.timeout_sec is not None:
        timeout_sec = int(args.timeout_sec)

    poll_disp = (os.getenv("SYNTAX_FALLBACK_POLL_SEC") or "10").strip()
    tout_disp = str(timeout_sec) if timeout_sec is not None else "по умолчанию"
    print(
        "Отправка в Syntax (Telethon); опрос каждые "
        f"{poll_disp} с — в каждом цикле читается последнее входящее сообщение бота "
        f"(reply_to на наше сообщение не требуется). Таймаут ожидания: {tout_disp} с…",
        flush=True,
    )
    result = try_generate_via_syntax(
        prompt=args.video_prompt,
        model="veo3_fast",
        duration_sec=8,
        start_image=frame,
        end_image=None,
        timeout_sec=timeout_sec,
    )
    if not result.success:
        print("Не удалось: Syntax не вернул видео.", file=sys.stderr)
        print(result.provider_message, file=sys.stderr)
        return 1

    try:
        mp4 = video_bytes_from_fallback_result(
            result,
            download_timeout_sec=int(args.download_timeout_sec),
        )
    except Exception as e:
        print(f"Could not obtain MP4: {e}", file=sys.stderr)
        return 1

    import time

    out_path = Path(args.out).expanduser() if args.out else PROJECT_ROOT / "artifacts" / f"syntax_e2e_{int(time.time())}.mp4"
    _write_bytes(out_path, mp4)
    print(f"УСПЕХ: сохранено видео {out_path} ({len(mp4)} bytes)", flush=True)
    if result.video_uri:
        print(f"URI: {result.video_uri}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
