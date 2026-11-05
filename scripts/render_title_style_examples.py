"""Render review images through the production title renderer."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.post_production import render_title_overlay_preview


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--background", type=Path, default=ROOT / "assets/locations/kitchen/base.png")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/title_style_examples")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with Image.open(args.background) as source:
        background = ImageOps.fit(source.convert("RGB"), (1080, 1920))
    examples = [
        ("borsch_orange", "БОРЩ", "#FF4400"),
        ("beer_cream", "СЛИВОЧНОЕ ПИВО", "#FFF7E8"),
        ("beer_pink", "СЛИВОЧНОЕ ПИВО", "#FF4F7B"),
    ]
    sheet = Image.new("RGB", (1080, 640))
    for index, (name, title, color) in enumerate(examples):
        path = args.output / f"{name}.png"
        path.write_bytes(render_title_overlay_preview(
            background, language="ru", title=title, title_style={"title_color": color},
        ))
        with Image.open(path) as image:
            sheet.paste(image.resize((360, 640), Image.Resampling.LANCZOS), (index * 360, 0))
    sheet.save(args.output / "examples.png")
    print(args.output / "examples.png")


if __name__ == "__main__":
    main()
