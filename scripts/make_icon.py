"""Regenerates assets/moonberry.ico (the shortcut / app icon).

Dev-only helper -- needs Pillow (`pip install pillow`), which the app
itself doesn't. Draws at a large size and downsamples so every icon
size stays smooth.

    python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

BIG = 1024
SIZES = [16, 24, 32, 48, 64, 128, 256]
OUT = Path(__file__).resolve().parent.parent / "assets" / "moonberry.ico"

NIGHT = (27, 31, 64, 255)
BERRY = (142, 58, 196, 255)
BERRY_DARK = (104, 36, 150, 255)
SHINE = (224, 190, 255, 255)
LEAF = (86, 190, 120, 255)
MOON = (255, 226, 140, 255)


def circle(draw, cx, cy, r, fill):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def render() -> Image.Image:
    img = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Night-sky tile.
    d.rounded_rectangle((32, 32, BIG - 32, BIG - 32), radius=200, fill=NIGHT)

    # Crescent moon, top-right: a disc with an offset disc punched out.
    moon = Image.new("L", (BIG, BIG), 0)
    md = ImageDraw.Draw(moon)
    circle(md, 700, 300, 190, 255)
    circle(md, 620, 240, 170, 0)
    img.paste(Image.new("RGBA", (BIG, BIG), MOON), (0, 0), moon)

    # Berry, bottom-left, with a darker rim and a highlight.
    circle(d, 430, 610, 290, BERRY_DARK)
    circle(d, 420, 598, 268, BERRY)
    circle(d, 330, 505, 70, SHINE)

    # Leaf on top of the berry.
    leaf = Image.new("L", (BIG, BIG), 0)
    ld = ImageDraw.Draw(leaf)
    ld.ellipse((400, 250, 640, 370), fill=255)
    leaf = leaf.rotate(25, center=(470, 330))
    img.paste(Image.new("RGBA", (BIG, BIG), LEAF), (0, 0), leaf)

    return img.filter(ImageFilter.SMOOTH)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    big = render()
    base = big.resize((256, 256), Image.LANCZOS)
    base.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
