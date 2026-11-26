"""Frames for the README's demo GIF, and the GIF made from them.

    demo_frames.py record DIR      on a private Xvfb only (scripts/make-demo.sh starts one)
    demo_frames.py assemble DIR OUT.gif [--width 640]

`record` plays the overlay probes' scenes on $DISPLAY: text being typed, the status icon,
Super+Alt+G opening the suggestion strip, Alt+Enter applying it; then a selection, the Glance
mark, its action bar and an Explain answer. It saves DIR/NNNN.png plus DIR/frames.json
(file, seconds). `assemble` makes the GIF with ffmpeg (palettegen) when it is installed,
else with Pillow.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
W, H = 960, 400  # every frame


def record(out_dir: Path) -> int:
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "desktop")]
    import cairo
    import overlay_probe as op
    import selection_probe as sp
    from gi.repository import Gdk
    from Xlib import X, display
    from Xlib.ext import xtest

    from glance.ui import selection_bar as sb
    from glance.ui.overlay_gtk import GtkOverlay
    from glance.ui.panel_model import PanelView, ResultView
    from glance.ui.status_model import StatusState, StatusView

    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[str, float]] = []
    overlay = GtkOverlay(lambda: None, lambda action, a: None)
    overlay._theme.force(False)
    d = display.Display()
    holder = d.screen().root.create_window(0, 0, 1280, 800, 0, d.screen().root_depth,
                                           background_pixel=d.screen().white_pixel)
    holder.map()
    d.sync()
    op.pump(0.2)
    holder.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    xtest.fake_input(d, X.MotionNotify, x=2, y=2)  # the pointer out of the way
    d.sync()

    def frame(x0: int, y0: int, seconds: float, caption: str = "", keys: str = "") -> None:
        """The screen at (x0, y0, W, H), the status icon drawn as a compositor shows it
        (Xvfb has none), a caption and a key badge."""
        op.pump(0.12)
        icon = overlay.status_window
        drawn = icon.get_visible() and not overlay._composited
        if drawn:
            ix, iy = icon.get_position()
            icon.hide()
            op.pump(0.1)
        pix = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x0, y0, W, H)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        cr = cairo.Context(surface)
        Gdk.cairo_set_source_pixbuf(cr, pix, 0, 0)
        cr.paint()
        if drawn:
            cr.save()
            cr.translate(ix - x0, iy - y0)
            cr.push_group()
            overlay._composited = True
            overlay._draw_icon(icon, cr)
            overlay._composited = False
            cr.pop_group_to_source()
            cr.paint()
            cr.restore()
            icon.show()
            op.pump(0.1)
        if caption:
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(17)
            cr.set_source_rgba(0.06, 0.09, 0.12, 0.9)
            cr.rectangle(0, H - 44, W, 44)
            cr.fill()
            x = 18.0
            if keys:
                ext = cr.text_extents(keys)
                cr.set_source_rgb(0.06, 0.73, 0.51)  # the brand emerald
                _rounded(cr, x - 6, H - 35, ext.x_advance + 12, 26, 6)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(x, H - 16)
                cr.show_text(keys)
                x += ext.x_advance + 18
            cr.set_source_rgb(0.92, 0.95, 0.97)
            cr.move_to(x, H - 16)
            cr.show_text(caption)
        name = f"{len(frames):04d}.png"
        surface.write_to_png(str(out_dir / name))
        frames.append((name, seconds))

    # ---- 1. typing, the icon, Super+Alt+G, Alt+Enter (X11 overlay)
    page = op.scenery()
    x0, y0 = op.ANCHOR.field.x - 150, op.ANCHOR.field.y - 110
    page.label.set_text("")
    frame(x0, y0, 0.6, "Type in any app")
    for n in range(4, len(op.TYPED) + 4, 5):
        page.label.set_text(op.TYPED[:n])
        frame(x0, y0, 0.07, "Type in any app")
    overlay.show_status(StatusView(StatusState.WORKING, spin=True), op.ANCHOR)
    frame(x0, y0, 0.7, "Glance checks the text")
    overlay.show_status(StatusView(StatusState.READY, op.STRIP.n_fixes), op.ANCHOR)
    frame(x0, y0, 1.4, "The icon counts what it found")
    overlay.show_strip(op.STRIP, op.ANCHOR)
    overlay.show_status(StatusView(StatusState.READY, op.STRIP.n_fixes), op.ANCHOR)
    frame(x0, y0, 3.0, "every fix, highlighted", keys="Super+Alt+G")
    overlay.hide_strip()
    overlay.hide_status()
    page.label.set_text("".join(s.text for s in op.STRIP.segments))
    frame(x0, y0, 2.4, "applied, one Ctrl+Z undoes it", keys="Alt+Enter")
    page.label.set_text(op.TYPED)
    overlay.show_strip(op.TONE_STRIP, op.ANCHOR)
    frame(x0, y0, 2.6, "or rewrite it in a tone: Friendlier, Shorter, Professional")
    overlay.hide_strip()
    page.page.hide()
    page.field.hide()

    # ---- 2. a selection: the mark, the bar, Explain
    sel_page = sp.Page()
    op.pump(0.3)
    end = sel_page.selection_end()
    mark_xy = sb.place_mark(*end, overlay.monitors())
    sx0, sy0 = 20, 30
    frame(sx0, sy0, 1.0, "Select text anywhere")
    overlay.show_selmark(sb.mark_view(), *mark_xy)
    frame(sx0, sy0, 1.3, "the Glance mark appears beside it")
    overlay.show_selmark(sb.bar_view("English", hint="Super+Alt+A"), *mark_xy)
    frame(sx0, sy0, 1.8, "Explain, Summarize, Translate, Fix, Rewrite, Reply")
    overlay.hide_selmark()
    src = sb.excerpt(sp.SELECTED)
    for state, text, secs, caption in (
            ("loading", None, 0.9, "Explain"),
            ("ready", sp.EXPLAIN, 3.6, "the answer, right where you are reading")):
        view = PanelView(mode="result", result=ResultView("Explain", src, text, state))
        for _ in range(2):  # the second placement knows the panel's real size
            overlay.show_panel(view, end[0], end[1] + 20)
            op.pump(0.3)
        frame(sx0, sy0, secs, caption)
    overlay.hide_panel()
    (out_dir / "frames.json").write_text(json.dumps(frames))
    return 0


def _rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    import math

    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def assemble(src: Path, out: Path, width: int = 800) -> int:
    frames = [(src / name, float(secs)) for name, secs in
              json.loads((src / "frames.json").read_text())]
    out.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        concat = src / "concat.txt"
        lines = []
        for path, secs in frames:
            lines += [f"file '{path}'", f"duration {secs:.3f}"]
        lines.append(f"file '{frames[-1][0]}'")  # the concat demuxer drops the last duration
        concat.write_text("\n".join(lines) + "\n")
        vf = (f"scale={width}:-1:flags=lanczos,split[a][b];"
              "[a]palettegen=max_colors=96:stats_mode=diff[p];"
              "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(concat), "-vf", vf, "-loop", "0", str(out)], check=True)
    else:
        from PIL import Image

        images = []
        for path, _ in frames:
            im = Image.open(path).convert("RGB")
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
            images.append(im.quantize(colors=96, method=Image.Quantize.MEDIANCUT))
        images[0].save(out, save_all=True, append_images=images[1:], loop=0, optimize=True,
                       duration=[round(s * 1000) for _, s in frames], disposal=1)
    print(f"{out} ({out.stat().st_size / 1e6:.2f} MB, {len(frames)} frames)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "record":
        return record(Path(argv[1]))
    if len(argv) >= 3 and argv[0] == "assemble":
        width = int(argv[argv.index("--width") + 1]) if "--width" in argv else 800
        return assemble(Path(argv[1]), Path(argv[2]), width)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
