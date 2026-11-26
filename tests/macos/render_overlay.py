"""Render the macOS overlay's views (status icon, strip, drawer) to PNG files, off screen.

    python tests/macos/render_overlay.py OUT_DIR

The panels are built by the real MacOverlay from synthetic view models and drawn into
bitmaps with `cacheDisplayInRect:toBitmapImageRep:`: no screen capture, nothing on screen
is read, so it works with the screen locked. Light and dark appearances.
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

from glance.diffing import DiffOp
from glance.platform.macos.overlay import MacOverlay
from glance.platform.macos.runloop import MacClock
from glance.protocol import Rect
from glance.ui.panel_model import IssueRow, PanelView, ToneChip
from glance.ui.placement import Anchor
from glance.ui.status_model import StatusState, StatusView
from glance.ui.strip_model import Segment, StripView


def save(view, path: Path, scale: float = 2.0) -> None:
    b = view.bounds()
    rep = view.bitmapImageRepForCachingDisplayInRect_(b)
    view.cacheDisplayInRect_toBitmapImageRep_(b, rep)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(str(path), True)


def main(out: str) -> int:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)
    clock = MacClock()
    anchor = Anchor(caret=Rect(420, 300, 1, 17), field=Rect(200, 280, 560, 120),
                    window=Rect(180, 200, 600, 400), pid=1)
    strip = StripView(segments=(Segment("Thanks, I ", False), Segment("have", True),
                                Segment(" attached the ", False), Segment("files", True),
                                Segment(" you asked for.", False)), n_fixes=2)
    chips = (ToneChip("friendlier", "Friendlier", 1, "Thanks so much, I've attached them!",
                      "ready"),
             ToneChip("direct", "More direct", 2, None, None),
             ToneChip("shorter", "Shorter", 3, None, "loading"),
             ToneChip("professional", "Professional", 4, None, None),
             ToneChip("custom", "Rewrite as…", None))
    panel = PanelView(tone="Neutral", tone_reason="reads fine for a team channel",
                      tone_chips=chips, profile_label="Team chat",
                      issues=(IssueRow("i1", "Thanks, I has attached", "Use have", ("have",),
                                       "Agreement", "has"),
                              IssueRow("i2", "attached the file you", "Plural", ("files",),
                                       "Number", "file")),
                      can_fix_all=True)
    selection = PanelView(tone_chips=chips[:4], mode="selection", can_paste=True,
                          preview=(DiffOp("eq", "These "), DiffOp("del", "is"),
                                   DiffOp("ins", "are"), DiffOp("eq", " the notes.")),
                          preview_state="ready", profile_label="Email")
    for name, appearance in (("light", NSAppearanceNameAqua), ("dark", NSAppearanceNameDarkAqua)):
        app.setAppearance_(NSAppearance.appearanceNamed_(appearance))
        ov = MacOverlay(lambda: None, lambda a, k: None, clock)
        for state, view in (("ready", StatusView(StatusState.READY, count=2)),
                            ("idle", StatusView(StatusState.IDLE)),
                            ("working", StatusView(StatusState.WORKING, spin=True)),
                            ("muted", StatusView(StatusState.MUTED, message="x"))):
            ov.show_status(view, anchor)
            save(ov._icon.contentView(), out_dir / f"macos-icon-{state}-{name}.png")
        ov.show_strip(strip, anchor)
        save(ov._strip.contentView(), out_dir / f"macos-strip-{name}.png")
        ov.show_panel(panel, 0, 0, anchor)
        save(ov._panel.contentView(), out_dir / f"macos-drawer-{name}.png")
        ov.hide_panel()
        ov.hide_strip()
        ov.show_panel(selection, 400, 300, None)
        save(ov._panel.contentView(), out_dir / f"macos-selection-{name}.png")
        ov.hide_panel()
        ov.hide_status()
    print(f"wrote {len(list(out_dir.glob('macos-*.png')))} files to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
