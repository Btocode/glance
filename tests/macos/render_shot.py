"""Render Glance Shot's card (macOS) and the selection result panel to PNG files.

    python tests/macos/render_shot.py OUT_DIR

The real MacShot / MacOverlay build them from synthetic views; the PNGs come from
`cacheDisplayInRect:toBitmapImageRep:` of Glance's own views (no screen capture, no
permission). It also prints the card's frames per step: its top-left (the toolbar) stays put
for the whole shot. Mark is driven too: strokes are synthesized on the Marker, Done gives a
marked PNG of the same pixel size, checked here.
"""
from __future__ import annotations

import sys
from pathlib import Path

from AppKit import (
    NSAppearance,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSApplication,
    NSBitmapImageFileTypePNG,
)

from glance.platform.macos.appkit_util import monitors
from glance.platform.macos.overlay import MacOverlay
from glance.platform.macos.runloop import MacClock
from glance.platform.macos.shot import MacShot, png_of
from glance.protocol import Rect
from glance.shot import bar_model as bm
from glance.shot.image import Shot, png_size
from glance.ui.panel_model import PanelView, ResultView


def save(view, path: Path) -> None:
    b = view.bounds()
    rep = view.bitmapImageRepForCachingDisplayInRect_(b)
    view.cacheDisplayInRect_toBitmapImageRep_(b, rep)
    rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(
        str(path), True)


def views() -> list[tuple[str, bm.ShotBarView]]:
    tabs = bm.buttons(vision=True, ai=True, images="ask", ocr=True, private=False)

    def sel(k: str) -> tuple[bm.ShotButton, ...]:
        return bm.buttons(vision=True, ai=True, images="ask", ocr=True, private=False, shown=k)

    text = "Build failed\nConnectionRefusedError: [Errno 111] Connection refused\n" \
           "deploy.py, line 42: client.push(release)"
    size, tools = "760 × 300", bm.tools()
    return [
        ("start", bm.ShotBarView(size, tabs, tools=tools)),
        ("copied", bm.ShotBarView(size, tabs, "Image copied to the clipboard", tools=tools)),
        ("text", bm.ShotBarView(size, sel("extract"), "Text copied to the clipboard",
                                result=bm.ShotResult("Text", text, "ready", bm.text_actions(),
                                                     note="Read on this computer"),
                                tools=tools, expanded=True)),
        ("choose", bm.ShotBarView(size, sel("translate"),
                                  result=bm.ShotResult(bm.TRANSLATE_TO, None, "choose",
                                                       choices=bm.languages("English")),
                                  tools=tools, expanded=True)),
        ("asking", bm.ShotBarView(size, sel("ask"), tools=tools, expanded=True, asking=True)),
        ("loading", bm.ShotBarView(size, sel("explain"),
                                   result=bm.ShotResult("Explanation", None, "loading",
                                                        note="Thinking…"),
                                   tools=tools, expanded=True)),
        ("notice", bm.ShotBarView(size, sel("explain"),
                                  notice=bm.ShotNotice(bm.notice_text(
                                      "Anthropic (api.anthropic.com)")),
                                  tools=tools, expanded=True)),
        ("long", bm.ShotBarView(size, sel("explain"), "Could not save the image", True,
                                result=bm.ShotResult("Explanation", " ".join(
                                    ["The deploy script could not reach the release server."]
                                    * 30), "ready", bm.ANSWER_ACTIONS, note="by claude"),
                                tools=tools, expanded=True)),
        ("failed", bm.ShotBarView(size, sel("translate"),
                                  result=bm.ShotResult("Translation", None, "failed",
                                                       note="Needs an AI — Text works locally"),
                                  tools=tools, expanded=True)),
    ]


def sample_png(w: int, h: int) -> bytes:
    """A white image with a dark title bar and a few grey lines (a stand-in screenshot)."""
    import Quartz as Q

    space = Q.CGColorSpaceCreateWithName(Q.kCGColorSpaceSRGB)
    ctx = Q.CGBitmapContextCreate(None, w, h, 8, 0, space, Q.kCGImageAlphaPremultipliedLast)
    Q.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Q.CGContextFillRect(ctx, Q.CGRectMake(0, 0, w, h))
    Q.CGContextSetRGBFillColor(ctx, 0.12, 0.14, 0.17, 1)
    Q.CGContextFillRect(ctx, Q.CGRectMake(60, h - 110, w * 0.4, 40))
    Q.CGContextSetRGBFillColor(ctx, 0.6, 0.62, 0.66, 1)
    for i in range(4):
        Q.CGContextFillRect(ctx, Q.CGRectMake(60, h - 170 - i * 44, w * (0.75 - i * 0.1), 18))
    return png_of(Q.CGBitmapContextCreateImage(ctx))


def red_at(png: bytes, x: int, y: int) -> bool:
    from AppKit import NSBitmapImageRep
    from Foundation import NSData

    rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithBytes_length_(png, len(png)))
    c = rep.colorAtX_y_(x, y)
    return c.redComponent() > 0.8 and c.greenComponent() < 0.45


def mark(shot: MacShot, region: Rect, out_dir: Path, name: str) -> None:
    """Mark, driven: a box, an arrow and a highlight; the canvas and its tools saved; Done."""
    png = sample_png(region.w * 2, region.h * 2)
    got: list = []
    shot.annotate(Shot(png, region.w * 2, region.h * 2, region), got.append)
    m = shot.marker
    print(name, "mark: card hidden", shot.bar_geometry() is None)
    for tool, a, b in (("box", (40, 40), (220, 120)), ("arrow", (420, 220), (240, 130)),
                       ("highlight", (60, 180), (380, 180))):
        m.act(f"mark_{tool}")
        m.press(*a)
        m.drag((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        m.release(*b)
    m.act("mark_arrow")
    save(m.canvas, out_dir / f"macos-shot-mark-{name}.png")
    save(m.bar.contentView(), out_dir / f"macos-shot-mark-tools-{name}.png")
    m.act("mark_undo")
    m.act("mark_highlight")
    m.press(60, 180)
    m.release(380, 180)
    m.finish()
    assert len(got) == 1 and got[0] is not None and got[0] != png, "Mark gave no new PNG"
    assert png_size(got[0]) == png_size(png), (png_size(got[0]), png_size(png))
    assert red_at(got[0], 80, 160), "no red box edge at (40, 80) logical"
    assert not red_at(png, 80, 160)
    (out_dir / f"macos-shot-marked-{name}.png").write_bytes(got[0])
    print(name, "mark: marked PNG", png_size(got[0]), "ok; marker gone", shot.marker is None)
    # Cancel gives None
    got2: list = []
    shot.annotate(Shot(png, region.w * 2, region.h * 2, region), got2.append)
    shot.marker.press(10, 10)
    shot.marker.cancel()
    assert got2 == [None], got2


def main(out: str) -> int:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)
    mon = monitors()[0]
    print("monitor", mon)
    region = Rect(mon.x + 80, mon.y + 120, 500, 260)
    acts: list = []
    for name, appearance in (("light", NSAppearanceNameAqua), ("dark", NSAppearanceNameDarkAqua)):
        app.setAppearance_(NSAppearance.appearanceNamed_(appearance))
        shot = MacShot(lambda k, a: acts.append((k, a)), lambda fn: fn())
        tops = set()
        for step, view in views():
            shot.show_bar(view, region)
            save(shot._bar.contentView(), out_dir / f"macos-shot-{step}-{name}.png")
            g = shot.bar_geometry()
            tops.add(g[:2])
            print(name, step, "card", g, "ring", shot.frame_geometry(),
                  "ask", shot.ask_point())
            if step == "asking":
                print(name, "asking: key", bool(shot._bar.isKeyWindow()),
                      "field", shot._field is not None)
                shot._field.setStringValue_("what does this error mean?")
                shot.ask_key("submit")
                print(name, "asked", acts[-1])
                shot.show_bar(view, region)
                shot.ask_key("cancel")
                print(name, "cancel", acts[-1])
        print(name, "toolbar top-left per step", tops)
        assert len(tops) == 1, tops
        mark(shot, region, out_dir, name)
        print(name, "card back after Mark", shot.bar_geometry())
        shot.hide_bar()
        print(name, "hidden", shot.bar_geometry(), shot.frame_geometry())
        ov = MacOverlay(lambda: None, lambda a, k: None, MacClock())
        point = (mon.x + 300, mon.y + 200)
        for state, r in (("loading", ResultView("Explain", "revenue grew 18% year over year")),
                         ("ready", ResultView("Explain", "revenue grew 18% year over year",
                                              "Sales income was 18% higher than in the same "
                                              "quarter last year, and fewer support requests "
                                              "were opened.", "ready")),
                         ("choose", ResultView("Translate to…", "revenue grew 18% year over "
                                               "year", None, "ready", choices=(
                                                   *((f"lang:{n}", n) for n in (
                                                       "English", "Bangla", "Hindi", "Spanish",
                                                       "French", "German", "Arabic", "Chinese",
                                                       "Japanese", "Portuguese")),
                                                   ("lang_other", "Other…")),
                                               chosen="lang:Bangla"))):
            ov.show_panel(PanelView(mode="result", result=r), *point, None)
            save(ov._panel.contentView(), out_dir / f"macos-sel-result-{state}-{name}.png")
            print(name, "result", state, "point", point, "panel", ov.panel_geometry())
        ov.hide_panel()
    print(f"wrote {len(list(out_dir.glob('macos-*.png')))} files to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
