"""Drives the Settings window on whatever $DISPLAY is (a private Xvfb, see
tests/test_settings_xvfb.py) with fake glanced services and a config file in a temp dir;
prints one JSON line. With GLANCE_SHOTS_DIR set it also saves settings-<section>-<theme>.png
there."""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from glance.audience.overrides import Overrides
from glance.doctor.checks import CheckResult
from glance.ui import settings_model as sm
from glance.ui.settings_gtk import SettingsWindow

MAIN = threading.get_ident()
CONFIG = """# my glance config
[general]
check = "shortcut"  # keep this comment

[general.hotkeys]
shot = "Super+Alt+x"

[apps.slack]
capture = true
"""


def pump(seconds):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def walk(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from walk(child)


def texts(win):
    return [w.get_text() for w in walk(win.window) if isinstance(w, Gtk.Label)
            and w.get_mapped()]


class FakeServices:
    def __init__(self):
        self.calls = []
        self.threads = set()
        self.key = {"anthropic": True, "openai": False}

    def _note(self, *call):
        self.threads.add(threading.get_ident() != MAIN)
        self.calls.append(list(call))
        time.sleep(0.15)  # a real ctl round trip: must not freeze the window

    def backends(self, refresh=False):
        self._note("backends", refresh)
        return {"ok": True, "choice": "claude", "backends": {
            "claude": {"installed": True, "signed_in": True},
            "codex": {"installed": True, "signed_in": False},
            "gemini": {"installed": True, "signed_in": None},
            "opencode": {"installed": False, "signed_in": None},
            "api": {"installed": True, "signed_in": True}}}

    def use_backend(self, name):
        self._note("use", name)
        return {"ok": True}

    def keys(self):
        self._note("keys")
        return {"ok": True, "keys": dict(self.key)}

    def set_key(self, provider, key):
        self._note("set_key", provider, len(key))
        self.key[provider] = True
        return {"ok": True}

    def remove_key(self, provider):
        self._note("remove_key", provider)
        self.key[provider] = False
        return {"ok": True}

    def usage(self):
        self._note("usage")
        total = {"calls": 26, "failed": 0, "input_tokens": 9000, "output_tokens": 3300,
                 "tokens_known": True, "cost_usd": 0.04}
        month = {"calls": 412, "failed": 2, "input_tokens": 150000, "output_tokens": 51000,
                 "tokens_known": True, "cost_usd": 0.87}
        return {"ok": True, "periods": {"today": {"total": total},
                                        "this_month": {"total": month}},
                "monthly_cost_limit_usd": 5.0, "limit_reason": None}

    def status(self):
        self._note("status")
        return {"ok": True, "hotkeys_problems": "pause:conflict"}

    def doctor(self):
        self._note("doctor")
        return [CheckResult("session", "Desktop session", "ok", "X11"),
                CheckResult("a11y", "Accessibility", "ok", "on"),
                CheckResult("autostart", "Start at login", "warn", "glance.service is disabled",
                            "run `glance doctor --fix`")]


class FakeLogin:
    mac = False

    def __init__(self):
        self.state = True
        self.sets = []

    def get(self):
        time.sleep(0.1)
        return self.state

    def set(self, on):
        self.sets.append(on)
        self.state = on
        return True


def shot(win, path):
    gdk = win.window.get_window()
    x, y = gdk.get_origin()[1:]
    w, h = gdk.get_width(), gdk.get_height()
    Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x, y, w, h).savev(
        str(path), "png", [], [])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="glance-settings-"))
    cfg = tmp / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    written = []
    model = sm.SettingsModel(cfg, on_written=lambda: written.append(1), mac=False,
                             overrides=Overrides(tmp / "overrides.json"))
    services, login, opened = FakeServices(), FakeLogin(), []
    win = SettingsWindow(model, services, start_at_login=login, open_uri=opened.append)
    win.window.move(0, 0)
    out = {}
    t0 = time.monotonic()
    win.show("general")
    pump(0.05)
    out["open_seconds"] = round(time.monotonic() - t0, 3)
    pump(0.4)
    out["general_texts"] = texts(win)
    shots = os.environ.get("GLANCE_SHOTS_DIR")

    # every section draws, in both themes
    for dark in (False, True):
        win.theme.force(dark)
        for key, _title in sm.SECTIONS:
            win.show_section(key)
            pump(0.6 if key in ("ai", "shortcuts") else 0.15)
            if key == "about" and dark is False:
                win._run_doctor()
                pump(0.5)
            if shots:
                shot(win, Path(shots) / f"settings-{key}-{'dark' if dark else 'light'}.png")
            out[f"texts_{key}"] = texts(win)
            out.setdefault("widths", []).append(win.window.get_allocated_width())
    win.theme.force(False)

    # writes go to the file, keep its comments, and call on_written (the live apply)
    out["write_ok"] = model.set(next(f for f in sm.GENERAL if f.key == ("ai", "max_words")), "800")
    out["write_bad"] = model.set(next(f for f in sm.GENERAL if f.key == ("ai", "max_words")), "9")
    text = cfg.read_text(encoding="utf-8")
    out["file_keeps_comment"] = "# keep this comment" in text and "max_words = 800" in text
    out["written"] = len(written)

    # a shortcut is captured, a taken one refused and shown under its row
    win.show_section("shortcuts")
    pump(0.3)
    win.apply_chord("check", "Super+Alt+k")  # KDE's keyboard layout switch
    pump(0.1)
    out["conflict_text"] = [t for t in texts(win) if t.startswith("Super+Alt+k:")]
    win.apply_chord("check", "Super+Alt+j")
    pump(0.1)
    out["chord_written"] = 'check = "Super+Alt+j"' in cfg.read_text(encoding="utf-8")
    out["live_problem"] = "Another program already uses it" in texts(win)

    # the API key goes to the service and is never left in the entry
    win.show_section("ai")
    pump(0.6)
    entries = [w for w in walk(win.window) if isinstance(w, Gtk.Entry) and w.get_mapped()
               and not w.get_visibility()]
    entries[0].set_text("sk-secret-probe")
    entries[0].emit("activate")
    pump(0.6)
    out["key_calls"] = [c for c in services.calls if c[0] == "set_key"]
    out["key_left_in_widget"] = any(
        isinstance(w, Gtk.Entry) and "sk-secret" in w.get_text() for w in walk(win.window))
    out["calls_off_main"] = services.threads == {True}
    out["opened"] = opened
    print(json.dumps(out))


if __name__ == "__main__":
    main()
    sys.exit(0)
