"""Turn the Gemini brand images into Glance's icon and logo set (Pillow only)."""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SRC = Path(__file__).parent / "src"
OUT = Path(__file__).parent / "out"
BG = (15, 15, 15)          # Studio Dark #0F0F0F (brand sheet)
INK_DARK = (250, 250, 250)  # Mist White #FAFAFA
INK_LIGHT = (15, 15, 15)


def extract(img: Image.Image, bg: tuple[int, int, int], lo: int, hi: int) -> Image.Image:
    """Lift a glowing mark off a flat dark background: alpha from brightness above `bg`,
    colour un-premultiplied so the mark looks the same on any background."""
    img = img.convert("RGB")
    out = []
    for r, g, b in img.getdata():
        m = max(r - bg[0], g - bg[1], b - bg[2])
        a = 0.0 if m <= lo else 1.0 if m >= hi else (m - lo) / (hi - lo)
        if a <= 0:
            out.append((0, 0, 0, 0))
            continue
        c = [max(0, min(255, round((v - k * (1 - a)) / a))) for v, k in zip((r, g, b), bg)]
        out.append((*c, round(a * 255)))
    res = Image.new("RGBA", img.size)
    res.putdata(out)
    return res


def green_region(img: Image.Image, grow: int) -> Image.Image:
    """Mask of where the mark is: green-dominant pixels, grown so its white highlights
    and soft glow are inside."""
    img = img.convert("RGB")
    m = Image.new("L", img.size)
    m.putdata([255 if g - (r + b) / 2 > 18 else 0 for r, g, b in img.getdata()])
    for _ in range(grow // 4):
        m = m.filter(ImageFilter.MaxFilter(9))
    return m


def trim(img: Image.Image, pad: int) -> Image.Image:
    box = img.getchannel("A").point(lambda v: 255 if v > 48 else 0).getbbox()
    x0, y0, x1, y1 = box
    return img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))


def squared(img: Image.Image) -> Image.Image:
    s = max(img.size)
    c = Image.new("RGBA", (s, s))
    c.paste(img, ((s - img.width) // 2, (s - img.height) // 2))
    return c


def rounded_mask(size: int, radius_frac: float = 0.225) -> Image.Image:
    big = size * 4
    m = Image.new("L", (big, big), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * radius_frac), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def tile(glyph: Image.Image, size: int = 1024) -> Image.Image:
    """App icon: dark rounded square (subtle top-light gradient + hairline), the mark centred."""
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        grad.putpixel((0, y), tuple(round(30 - 12 * t) for _ in range(3)))
    body = grad.resize((size, size))
    rim = Image.new("RGB", (size, size), (58, 58, 58))
    inner = rounded_mask(size - 6)
    body_m = Image.new("L", (size, size), 0)
    body_m.paste(inner, (3, 3))
    base = Image.composite(body, rim, body_m).convert("RGBA")
    g = squared(glyph)
    g = g.resize((int(size * 0.70),) * 2, Image.LANCZOS)
    base.alpha_composite(g, ((size - g.width) // 2, (size - g.height) // 2))
    base.putalpha(rounded_mask(size))
    return base


def symbolic(glyph: Image.Image, colour: tuple[int, int, int], size: int) -> Image.Image:
    """Tray icon: one flat colour, no glow, no sparkle (it vanishes at 16-24 px)."""
    a = glyph.getchannel("A").point(lambda v: 255 if v > 150 else 0)
    # drop the sparkle: keep only the biggest blob's bounding area (sparkle sits top-right, apart)
    w, h = a.size
    a = a.crop((0, int(h * 0.12), int(w * 0.92), h))  # sparkle lives in the top-right corner
    a = a.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    sq = squared(Image.merge("RGBA", (*[Image.new("L", a.size, c) for c in colour], a)))
    pad = int(sq.width * 0.06)
    canvas = Image.new("RGBA", (sq.width + 2 * pad,) * 2)
    canvas.paste(sq, (pad, pad))
    return canvas.resize((size, size), Image.LANCZOS)


def text_start(mark: Image.Image) -> int:
    """First column of the letters: the end of the widest empty column run in the middle."""
    a = mark.getchannel("A")
    w, h = a.size
    empty = [a.crop((x, 0, x + 1, h)).getextrema()[1] < 40 for x in range(w)]
    best, run, start = (0, 0), 0, 0
    for x in range(int(w * 0.2), int(w * 0.6)):
        if empty[x]:
            run += 1
            if run > best[0]:
                best = (run, x + 1)
        else:
            run = 0
    return best[1]


def recolour_text(mark: Image.Image, ink: tuple[int, int, int], glow_gamma: float = 1.0) -> Image.Image:
    """Grey/white (unsaturated) pixels right of the mark are the letters: paint them `ink`."""
    x0 = text_start(mark)
    w = mark.width
    out = []
    for i, (r, g, b, a) in enumerate(mark.getdata()):
        if i % w < x0:
            out.append((r, g, b, round(255 * (a / 255) ** glow_gamma)))
        elif a and max(r, g, b) - min(r, g, b) < 40:
            out.append((*ink, a))
        else:
            out.append((r, g, b, a))
    res = Image.new("RGBA", mark.size)
    res.putdata(out)
    return res


def main() -> None:
    for d in ("icons", "symbolic", "logo", "social"):
        (OUT / d).mkdir(parents=True, exist_ok=True)

    icon = Image.open(SRC / "icon.jpeg").convert("RGB")
    # the mark sits on the tile's ~#1d1d1d face; cut inside the tile so its rim is not lifted
    face = icon.crop((190, 190, 835, 835))
    lifted = extract(face, (29, 29, 29), 6, 120)
    lifted.putalpha(ImageChops.multiply(lifted.getchannel("A"), green_region(face, 24)))
    glyph = trim(lifted, 12)
    glyph.save(OUT / "logo" / "glance-mark.png")

    app = tile(glyph)
    app.save(OUT / "icons" / "glance-1024.png")
    for s in (16, 22, 24, 32, 48, 64, 128, 256, 512):
        app.resize((s, s), Image.LANCZOS).save(OUT / "icons" / f"glance-{s}.png")
    app.save(OUT / "icons" / "glance.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    app.save(OUT / "icons" / "glance.icns")

    for name, colour in (("white", INK_DARK), ("black", INK_LIGHT)):  # for a light / dark panel
        for s in (16, 22, 24, 32, 48):
            symbolic(glyph, colour, s).save(OUT / "symbolic" / f"glance-symbolic-{name}-{s}.png")
        symbolic(glyph, colour, 256).save(OUT / "symbolic" / f"glance-symbolic-{name}-256.png")

    wm = Image.open(SRC / "wordmark.jpeg").convert("RGB")
    mark = trim(extract(wm, (17, 17, 15), 6, 150), 24)
    on_dark = recolour_text(mark, INK_DARK)
    on_light = recolour_text(mark, INK_LIGHT, glow_gamma=1.8)
    on_dark.save(OUT / "logo" / "glance-logo-on-dark.png")
    on_light.save(OUT / "logo" / "glance-logo-on-light.png")

    # GitHub social preview: 1280x640, logo centred on Studio Dark
    soc = Image.new("RGBA", (1280, 640), (*BG, 255))
    lg = on_dark.copy()
    lg.thumbnail((900, 360), Image.LANCZOS)
    soc.alpha_composite(lg, ((1280 - lg.width) // 2, (640 - lg.height) // 2))
    soc.convert("RGB").save(OUT / "social" / "glance-social-preview.png")

    # previews for checking: each variant on dark and light
    prev = Image.new("RGBA", (1400, 900), (*BG, 255))
    ImageDraw.Draw(prev).rectangle((700, 0, 1400, 900), fill=(250, 250, 250, 255))
    a = on_dark.copy(); a.thumbnail((620, 240)); prev.alpha_composite(a, (40, 40))
    b = on_light.copy(); b.thumbnail((620, 240)); prev.alpha_composite(b, (740, 40))
    for i, s in enumerate((256, 64, 32, 16)):
        im = app.resize((s, s), Image.LANCZOS)
        prev.alpha_composite(im, (40 + i * 300, 360)); prev.alpha_composite(im, (740 + i * 160, 360))
    for i, s in enumerate((48, 24, 16)):
        prev.alpha_composite(symbolic(glyph, INK_DARK, s), (40 + i * 120, 700))
        prev.alpha_composite(symbolic(glyph, INK_LIGHT, s), (740 + i * 120, 700))
    prev.convert("RGB").save(OUT / "preview.png")


if __name__ == "__main__":
    main()
