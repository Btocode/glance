"""Drives the onboarding window and the tray's backend menu on whatever $DISPLAY is; prints
one JSON line. Run by tests/test_onboarding_xvfb.py against a private Xvfb server."""
import json
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from glance.backends.base import Health
from glance.ui.onboarding_gtk import OnboardingWindow
from glance.ui.onboarding_model import build_rows
from glance.ui.tray_gtk import BackendMenu


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


def labels(win):
    return [w.get_text() for w in walk(win.window) if isinstance(w, Gtk.Label)]


def button(win, label, nth=0):
    found = [w for w in walk(win.window) if isinstance(w, Gtk.Button) and w.get_label() == label]
    return found[nth]


state = {"healths": {"claude": Health(True, True, ""), "codex": Health(True, False, ""),
                     "gemini": Health(True, None, ""), "opencode": Health(False, None, ""),
                     "api": Health(True, False, "")}, "choice": None}
calls = {"use": [], "test": [], "key": [], "api": [], "rows_threads": [], "test_threads": []}
main_thread = threading.get_ident()


def rows():
    calls["rows_threads"].append(threading.get_ident())
    time.sleep(0.3)  # a health check: must not freeze the window
    return build_rows(state["healths"], state["choice"])


def on_use(name):
    calls["use"].append(name)
    state["choice"] = name


def on_test(name, done):
    calls["test_threads"].append(threading.get_ident())
    calls["test"].append(name)
    time.sleep(0.2)
    done(name != "gemini", "ok" if name != "gemini" else "auth: run `gemini` to authenticate")


win = OnboardingWindow(rows, on_use, on_test, lambda p, k: calls["key"].append((p, k)),
                       lambda p, m, u: calls["api"].append((p, m, u)))
t0 = time.monotonic()
win.show()
show_seconds = time.monotonic() - t0
pump(0.1)
frozen_check = []
GLib.timeout_add(50, lambda: frozen_check.append(time.monotonic()) or len(frozen_check) < 5)
pump(0.8)
out = {"show_seconds": show_seconds, "ticks_during_refresh": len(frozen_check)}
out["headline"] = [t for t in labels(win) if "AI" in t][:1]
out["texts"] = labels(win)
out["test_note_count"] = labels(win).count("Test sends one short sample sentence")
use_buttons = [w for w in walk(win.window) if isinstance(w, Gtk.Button)
               and w.get_label() in ("Use this", "In use")]
out["use_sensitive"] = [b.get_sensitive() for b in use_buttons]  # claude, codex, gemini, opencode, api
out["window_hint_focus"] = win.window.get_accept_focus()

button(win, "Test", 0).clicked()
pump(0.1)
out["testing_text"] = "Testing…" in labels(win)
pump(1.0)
out["test_result"] = [t for t in labels(win) if t.startswith(("✓", "✗"))]
button(win, "Test", 2).clicked()  # gemini
pump(1.0)
out["test_results_after_gemini"] = [t for t in labels(win) if t.startswith(("✓", "✗"))]

button(win, "Use this", 0).clicked()
pump(1.0)
out["used"] = calls["use"]
out["headline_after_use"] = [t for t in labels(win) if t.startswith("AI:")]
out["in_use_button"] = [w.get_label() for w in walk(win.window)
                        if isinstance(w, Gtk.Button) and w.get_label() == "In use"]

api = win._widgets["api"].extra
out["key_visible"] = api["key"].get_visibility()
api["provider"].set_active(1)
api["model"].set_text("gpt-x")
api["key"].set_text("sk-probe-key")
api["save"].clicked()
out["key_after_click_in_widget"] = api["key"].get_text()
pump(1.0)
out["key_calls"] = calls["key"]
out["api_calls"] = calls["api"]
out["key_status"] = api["status"].get_text()

win.window.emit("delete-event", None)
out["hidden_not_destroyed"] = not win.window.get_visible() and win.window.get_realized()
out["threads_off_main"] = (all(t != main_thread for t in calls["rows_threads"])
                           and all(t != main_thread for t in calls["test_threads"]))

picked = []
setup = []
menu = BackendMenu(Gtk, picked.append, lambda: setup.append(1))


def submenu_labels():
    return [c.get_label() for c in menu.submenu.get_children()
            if isinstance(c, Gtk.MenuItem) and c.get_label()]


out["menu_none"] = submenu_labels()
menu.set_rows(build_rows(state["healths"], "claude"))
out["menu_ready"] = submenu_labels()
radios = [c for c in menu.submenu.get_children() if isinstance(c, Gtk.RadioMenuItem)]
out["menu_active"] = [r.get_label() for r in radios if r.get_active()]
out["menu_picked_on_rebuild"] = list(picked)
state["healths"]["codex"] = Health(True, True, "")
menu.set_rows(build_rows(state["healths"], "claude"))
radios = [c for c in menu.submenu.get_children() if isinstance(c, Gtk.RadioMenuItem)]
radios[1].set_active(True)
out["menu_picked"] = list(picked)
next(c for c in menu.submenu.get_children() if c.get_label() == "Set up…").activate()
out["menu_setup"] = list(setup)
win.close()
print(json.dumps(out))
sys.exit(0)
