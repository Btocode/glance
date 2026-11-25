"""GTK 3 test target: a TextView, an Entry and a password Entry in one window.

    gtk_app.py [--text S] [--wm-class NAME] [--timing] [--undo]

`--undo` uses a GtkSourceView (what gedit uses) in place of the TextView: a GTK 3 TextView
has no undo at all, so Ctrl+Z can only be tested against the source view.

Prints `READY` once the window is mapped. On SIGUSR1 it prints the TextView's buffer as one
JSON string line, which is the ground truth for write-back tests. On SIGUSR2 it prints
`CARET <n>`, the insert mark's offset in code points (the acceptance capture matrix).
"""
import argparse
import json
import signal
import sys
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

TITLE = "glance-test-app"


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="")
    ap.add_argument("--wm-class", default=None)
    ap.add_argument("--timing", action="store_true",
                    help="print `DELTA <ns>` per buffer change: key-press to the buffer changing")
    ap.add_argument("--accepts-tab", action="store_true",
                    help="Tab inserts a tab character instead of moving the focus")
    ap.add_argument("--undo", action="store_true",
                    help="use a GtkSourceView, which has Ctrl+Z (a GTK 3 TextView has not)")
    args = ap.parse_args(argv)

    if args.wm_class:
        GLib.set_prgname(args.wm_class)
        Gdk.set_program_class(args.wm_class)

    win = Gtk.Window(title=TITLE)
    win.set_default_size(480, 320)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
    if args.undo:
        gi.require_version("GtkSource", "4")
        from gi.repository import GtkSource

        view = GtkSource.View(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        view.get_buffer().begin_not_undoable_action()  # the initial text is not an edit
        view.get_buffer().set_text(args.text)
        view.get_buffer().end_not_undoable_action()
    else:
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=args.accepts_tab)
        view.get_buffer().set_text(args.text)
    scroller = Gtk.ScrolledWindow(vexpand=True)
    scroller.add(view)
    box.pack_start(scroller, True, True, 0)
    box.pack_start(Gtk.Entry(), False, False, 0)
    box.pack_start(
        Gtk.Entry(visibility=False, input_purpose=Gtk.InputPurpose.PASSWORD), False, False, 0)
    win.add(box)

    def dump() -> bool:
        buf = view.get_buffer()
        _say(json.dumps(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)))
        return GLib.SOURCE_CONTINUE

    def caret() -> bool:
        buf = view.get_buffer()
        _say(f"CARET {buf.get_iter_at_mark(buf.get_insert()).get_offset()}")
        return GLib.SOURCE_CONTINUE

    if args.timing:
        pressed: list[int] = []

        def on_key(_w: Gtk.Widget, _e: Gdk.EventKey) -> bool:  # runs before the class handler
            pressed.append(time.monotonic_ns())
            return False

        def on_changed(_buf: Gtk.TextBuffer) -> None:
            if pressed:
                t0 = pressed[-1]
                pressed.clear()
                _say(f"DELTA {time.monotonic_ns() - t0}")

        view.connect("key-press-event", on_key)
        view.get_buffer().connect("changed", on_changed)

    def on_map(_widget: Gtk.Widget, _event: Gdk.Event) -> bool:
        _say("READY")
        return False

    win.connect("map-event", on_map)
    win.connect("destroy", Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, dump)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, caret)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
    view.grab_focus()
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
