"""A stand-in glanced that only answers `ping` and `edit_session`, with the real GTK window.

    edit_host.py   (uses $DISPLAY and $XDG_RUNTIME_DIR; prints READY when ctl.sock is up)

It wires `gtk_edit_host` the way glanced will (grammar, issue filter, supervisor, a worker
pool for rewrites), with the daemon's test AI backend: no real AI call is made. The data
directory comes from $XDG_DATA_HOME, so it is the test's own.
"""
from __future__ import annotations

import signal
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk


def main() -> int:
    if not Gtk.init_check(None)[0]:
        print("no display", file=sys.stderr)
        return 1
    from glance.backends.budget import Budget
    from glance.backends.supervisor import BackendSupervisor
    from glance.clock import GLibClock
    from glance.config import BackendCfg
    from glance.daemon.main import FakeAiBackend, build_issue_filter
    from glance.grammar.harper import HarperClient
    from glance.ipc import JsonLineServer
    from glance.paths import ctl_sock, dictionary_path, harper_bin, ignored_path, usage_path
    from glance.ui.edit_window_gtk import gtk_edit_host

    clock = GLibClock()
    harper = HarperClient(harper_bin(), dictionary_path())
    if harper_bin().exists():
        harper.start()
    issue_filter, _detector = build_issue_filter(dictionary_path(), ignored_path())
    cfg = BackendCfg(choice="fake", enabled=())
    supervisor = BackendSupervisor({"fake": FakeAiBackend()}, lambda: cfg,
                                   Budget(clock, lambda: cfg, usage_path()), clock,
                                   lambda: False, lambda st: None)

    def run_bg(work, done) -> None:
        def job() -> None:
            result = work()
            GLib.idle_add(lambda: done(result) and False)

        threading.Thread(target=job, daemon=True).start()

    host = gtk_edit_host(harper, issue_filter, supervisor, run_bg)

    def on_ctl(conn, msg_type, payload) -> None:  # reader thread
        def reply(msg: dict) -> None:
            conn.send(msg_type, {**msg, "id": payload.get("id")})

        def answer() -> bool:
            try:
                if msg_type == "ping":
                    reply({"ok": True})
                elif msg_type == "edit_session":
                    host.handle(payload, reply)
                else:
                    reply({"ok": False, "error": "unknown request"})
            except ConnectionError:
                pass
            return False

        GLib.idle_add(answer)

    server = JsonLineServer(ctl_sock(), on_ctl)
    server.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: Gtk.main_quit() or False)
    print("READY", flush=True)
    try:
        Gtk.main()
    finally:
        server.stop()
        harper.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
