"""Render Glance Shot's card (macOS) and the selection result panel to PNG files.

    python tests/macos/render_shot.py OUT_DIR

The real MacShot / MacOverlay build them from synthetic views; the PNGs come from
`cacheDisplayInRect:toBitmapImageRep:` of Glance's own views (no screen capture, no
permission). It also prints the card's frames per step: on one side its top-left stays put.
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
from glance.platform.macos.shot import MacShot
from glance.protocol import Rect
from glance.shot import bar_model as bm
from glance.ui.panel_model import PanelView, ResultView


def save(view, path: Path) -> None:
    b = view.bounds()
    rep = view.bitmapImageRepForCachingDisplayInRect_(b)
    view.cacheDisplayInRect_toBitmapImageRep_(b, rep)
    rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(
        str(path), True)


def views() -> list[tuple[str, bm.ShotBarView]]:
    tabs = bm.buttons(vision=True, ai=True, images="ask", ocr=True, private=False)
    sel = lambda k: bm.buttons(vision=True, ai=True, images="ask", ocr=True,
                               private=False, shown=k)
    text = "Build failed\nConnectionRefusedError: [Errno 111] Connection refused\n" \
           "deploy.py, line 42: client.push(release)"
    return [
        ("start", bm.ShotBarView("760 × 300", tabs, tools=bm.tools())),
        ("text", bm.ShotBarView("760 × 300", sel("extract"), "Text copied to the clipboard",
                                result=bm.ShotResult("Text", text, "ready", bm.text_actions(),
                                                     note="Read on this computer"),
                                tools=bm.tools())),
        ("loading", bm.ShotBarView("760 × 300", sel("explain"),
                                   result=bm.ShotResult("Explanation", None, "loading",
                                                        note="Thinking…"), tools=bm.tools())),
        ("notice", bm.ShotBarView("760 × 300", sel("explain"),
                                  notice=bm.ShotNotice(bm.notice_text(
                                      "Anthropic (api.anthropic.com)")), tools=bm.tools())),
        ("long", bm.ShotBarView("760 × 300", sel("explain"), "Could not save the image", True,
                                result=bm.ShotResult("Explanation", " ".join(
                                    ["The deploy script could not reach the release server."]
                                    * 30), "ready", bm.ANSWER_ACTIONS, note="by claude"),
                                tools=bm.tools())),
        ("failed", bm.ShotBarView("760 × 300", sel("translate"),
                                  result=bm.ShotResult("Translation", None, "failed",
                                                       note="Needs an AI — Text works locally"),
                                  tools=bm.tools())),
    ]


def main(out: str) -> int:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)
    mon = monitors()[0]
    print("monitor", mon)
    region = Rect(mon.x + 80, mon.y + 120, 500, 260)
    for name, appearance in (("light", NSAppearanceNameAqua), ("dark", NSAppearanceNameDarkAqua)):
        app.setAppearance_(NSAppearance.appearanceNamed_(appearance))
        shot = MacShot(lambda _k: None, lambda fn: fn())
        for step, view in views():
            shot.show_bar(view, region)
            save(shot._bar.contentView(), out_dir / f"macos-shot-{step}-{name}.png")
            print(name, step, "card", shot.bar_geometry(), "ring", shot.frame_geometry(),
                  "ask", shot.ask_point())
        shot.hide_bar()
        print(name, "hidden", shot.bar_geometry(), shot.frame_geometry())
        ov = MacOverlay(lambda: None, lambda a, k: None, MacClock())
        point = (mon.x + 300, mon.y + 200)
        for state, r in (("loading", ResultView("Explain", "revenue grew 18% year over year")),
                         ("ready", ResultView("Explain", "revenue grew 18% year over year",
                                              "Sales income was 18% higher than in the same "
                                              "quarter last year, and fewer support requests "
                                              "were opened.", "ready"))):
            ov.show_panel(PanelView(mode="result", result=r), *point, None)
            save(ov._panel.contentView(), out_dir / f"macos-sel-result-{state}-{name}.png")
            print(name, "result", state, "point", point, "panel", ov.panel_geometry())
        ov.hide_panel()
    print(f"wrote {len(list(out_dir.glob('macos-*.png')))} files to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
