"""Drives one EditWindow in-process on $DISPLAY (a private Xvfb) and prints what it saw as
one JSON line. Buttons are pressed with `clicked()`; the AI is a FakeBackend."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from glance.backends.budget import Budget
from glance.backends.supervisor import BackendSupervisor
from glance.config import BackendCfg
from glance.features.edit_session import EditSession, prompt_ctx
from glance.grammar.filters import IssueFilter
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.protocol import Rect
from glance.ui.edit_window_gtk import SIZE, EditWindow
from tests.fakes import FakeBackend, FakeClock, FakeHarper

REWRITE = "Fix the bug in [which file?]"


def pump(seconds: float = 0.2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    clock = FakeClock()
    cfg = BackendCfg(choice="claude")
    backend = FakeBackend("claude", deltas=(REWRITE,))
    sup = BackendSupervisor({"claude": backend}, lambda: cfg,
                            Budget(clock, lambda: cfg, tmp / "usage.json"), clock,
                            lambda: False, lambda st: None)
    filt = IssueFilter(PersonalDictionary(tmp / "d.txt"), IgnoredRules(tmp / "i.json"), [])
    harper = FakeHarper({"teh": ("SpellCheck", ("the", "tech"))})
    session = EditSession("fix teh bug", harper, filt, sup, prompt_ctx(),
                          lambda work, done: done(work()))
    done: list = []
    over = Rect(100, 50, 1000, 700)
    w = EditWindow(session, over, lambda a, t: done.append([a, t]))
    pump(0.5)
    out: dict = {}
    x, y = w.window.get_position()
    out["centre"] = [x + SIZE[0] // 2, y + SIZE[1] // 2]
    out["title"] = w.window.get_title()
    out["keep_above_hint"] = w.window.get_type_hint().value_nick
    out["chips"] = [b.get_label() for b in w.chip_buttons.values()]
    b = w.buffer
    out["underlined"] = [b.get_iter_at_offset(n).has_tag(w._issue_tag) for n in (3, 4, 6, 7)]
    out["issues_label"] = w.issues_label.get_text()
    out["preview_visible_before"] = w.preview_box.get_visible()

    w.chip_buttons["sharpen_prompt"].clicked()
    pump()
    out["preview_visible"] = w.preview_box.get_visible()
    out["preview_text"] = w.preview_label.get_text()
    out["use_sensitive"] = w.use_button.get_sensitive()
    w.use_button.clicked()
    pump()
    out["after_use"] = w.text()
    out["preview_visible_after_use"] = w.preview_box.get_visible()
    w.undo_button.clicked()
    pump()
    out["after_undo"] = w.text()

    row = w.issue_list.get_children()[0].get_child()
    fixes = [c for c in row.get_children() if isinstance(c, Gtk.Button)]
    out["fix_labels"] = [c.get_label() for c in fixes]
    fixes[1].clicked()
    pump()
    out["after_fix"] = w.text()
    out["issues_after_fix"] = w.issues_label.get_text()

    b.insert(b.get_end_iter(), " now", -1)
    pump(0.6)  # past the issue refresh
    out["session_follows_typing"] = session.text

    w.chip_buttons["custom"].clicked()
    pump()
    out["custom_row_visible"] = w.custom_row.get_visible()
    w.custom_entry.set_text("as a list")
    w.custom_entry.activate()
    pump()
    out["custom_call"] = [backend.calls[-1].tone, backend.calls[-1].custom]

    w.window.close()  # the window manager's close button
    pump()
    out["done"] = done
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
