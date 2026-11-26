"""Make the status icon's glow files from the brand mark's (Pillow only).

    python scripts/make-glow-assets.py

src/glance/ui/assets/glance-glow-<px>.png: the mark at <px>, its silhouette blurred, on a
square GLOW_PAD * px bigger on every side, white with the blur as alpha. The renderers tint
it with the theme's `icon_glow` colour and draw it behind the mark.
"""
from pathlib import Path

from PIL import Image, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "src/glance/ui/assets"
SIZES = (14, 16, 18, 20, 24, 28, 32, 36, 40, 48, 64, 128)  # theme.MARK_SIZES
GLOW_PAD = 0.25  # theme.GLOW_PAD


def glow(px: int) -> Image.Image:
    scale = 4  # blurred big and scaled down: smooth even at 14 px
    mark = Image.open(ASSETS / f"glance-mark-{px}.png").convert("RGBA")
    big = mark.resize((px * scale, px * scale), Image.LANCZOS)
    pad = round(px * GLOW_PAD) * scale
    canvas = Image.new("L", (big.width + 2 * pad, big.height + 2 * pad), 0)
    canvas.paste(big.getchannel("A"), (pad, pad))
    alpha = canvas.filter(ImageFilter.GaussianBlur(px * scale * 0.11))
    peak = max(1, alpha.getextrema()[1])
    alpha = alpha.point(lambda v: min(255, round(v * 255 / peak)))  # its densest part opaque
    out = Image.merge("RGBA", (*(Image.new("L", alpha.size, 255),) * 3, alpha))
    side = px + 2 * round(px * GLOW_PAD)
    return out.resize((side, side), Image.LANCZOS)


def main() -> None:
    for px in SIZES:
        glow(px).save(ASSETS / f"glance-glow-{px}.png", optimize=True)


if __name__ == "__main__":
    main()
