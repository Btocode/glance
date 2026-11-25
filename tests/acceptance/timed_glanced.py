"""glanced with timing probes, for the latency gates (`python -m tests.acceptance.timed_glanced`).

The probes wrap the shipped classes from outside, so nothing in `src/` carries test code, and
each prints one text-free `log_event` line of numbers (they pass `GLANCE_STRICT_LOG=1`):

    chip_shown   ms_since_settle ms_since_text    the chip became visible
    chip_moved   ms ms_since_settle               snapshot received (and debounce fired) -> chip moved
    analyze_done ms backend                       analysis requested -> result in
    rewrite_first_delta ms backend                rewrite started -> first text in
    apply_sent   ms_since_request                 tone request -> apply sent to capture
    ghost_shown  ms_since_pause backend           caret idle (or hotkey) -> ghost drawn
"""
from __future__ import annotations

import sys
import time
from typing import Any

from glance.log import log_event

now = time.monotonic
S: dict[str, Any] = {"snap": 0.0, "text": 0.0, "settle": 0.0, "chip": None, "req": None,
                     "ac_idle": None}


def ms(t0: float) -> float:
    return round((now() - t0) * 1000, 2)


def backend_name(ai: Any) -> str:
    try:
        return str(ai._supervisor.active() or "none")
    except Exception:  # noqa: BLE001 - a probe never breaks the daemon
        return "unknown"


def install() -> None:
    from glance.daemon.ai import AiCoordinator
    from glance.daemon.controller import Controller
    from glance.features.autocomplete import Autocomplete

    # -- chip
    orig_init, orig_snap, orig_settled = (Controller.__init__, Controller.handle_snapshot,
                                          Controller._on_settled)

    def init(self: Any, *a: Any, **kw: Any) -> None:
        orig_init(self, *a, **kw)
        show, hide = self._overlay.show_chip, self._overlay.hide_chip

        def show_chip(view: Any, x: int, y: int) -> None:
            show(view, x, y)
            before = S["chip"]
            S["chip"] = (view.state, x, y)
            if before is None:
                since = ms(S["settle"]) if S["settle"] >= S["text"] else -1.0
                log_event("chip_shown", ms_since_settle=since, ms_since_text=ms(S["text"]))
            elif before[1:] != (x, y):
                log_event("chip_moved", ms=ms(S["snap"]), ms_since_settle=ms(S["settle"]))

        def hide_chip() -> None:
            hide()
            S["chip"] = None

        self._overlay.show_chip, self._overlay.hide_chip = show_chip, hide_chip

    def handle_snapshot(self: Any, s: Any) -> None:
        prev = self.current
        S["snap"] = now()
        if prev is None or prev.text != s.text:
            S["text"] = S["snap"]
        orig_snap(self, s)

    def on_settled(self: Any, *a: Any) -> None:
        S["settle"] = now()
        orig_settled(self, *a)

    Controller.__init__, Controller.handle_snapshot = init, handle_snapshot
    Controller._on_settled = on_settled

    # -- apply: the hotkey's request to the write-back being sent
    orig_req = Controller.request_apply

    def request_apply(self: Any, *a: Any, **kw: Any) -> str:
        t0, S["req"] = S["req"], None
        out = orig_req(self, *a, **kw)
        if out and t0 is not None:
            log_event("apply_sent", ms_since_request=ms(t0))
        return out

    Controller.request_apply = request_apply
    orig_apply = AiCoordinator.apply

    def apply(self: Any, *a: Any, **kw: Any) -> None:
        S["req"] = now()
        try:
            orig_apply(self, *a, **kw)
        finally:
            S["req"] = None

    AiCoordinator.apply = apply
    # A hotkey on a rewrite that is still loading is applied later, in _rewrite_done: keep the
    # first request's stamp for that case.
    orig_done = AiCoordinator._rewrite_done

    def rewrite_done(self: Any, rw: Any, *a: Any, **kw: Any) -> None:
        S["req"] = getattr(rw, "_t_req", None)
        orig_done(self, rw, *a, **kw)
        S["req"] = None

    AiCoordinator._rewrite_done = rewrite_done

    # -- analyze and rewrite
    orig_due, orig_adone = AiCoordinator._on_analyze_due, AiCoordinator._done

    def on_analyze_due(self: Any, s: Any, gen: int) -> None:
        self._t_analyze = now()
        orig_due(self, s, gen)

    def adone(self: Any, job: Any, s: Any, entry: Any, status: str) -> None:
        t0 = getattr(self, "_t_analyze", None)
        if entry is not None and t0 is not None and self._job is job:
            log_event("analyze_done", ms=ms(t0), backend=backend_name(self))
        orig_adone(self, job, s, entry, status)

    AiCoordinator._on_analyze_due, AiCoordinator._done = on_analyze_due, adone
    orig_start, orig_partial = AiCoordinator._start, AiCoordinator._render_partial

    def start(self: Any, rw: Any, s: Any) -> None:
        rw._t_start = now()
        rw._t_req = S["req"]
        orig_start(self, rw, s)

    def render_partial(self: Any, rw: Any, shielded: Any) -> None:
        t0 = getattr(rw, "_t_start", None)
        if t0 is not None:
            rw._t_start = None
            log_event("rewrite_first_delta", ms=ms(t0), backend=backend_name(self))
        orig_partial(self, rw, shielded)

    AiCoordinator._start, AiCoordinator._render_partial = start, render_partial

    # -- ghost: from the caret going idle (live) or the hotkey, to the ghost being drawn
    orig_idle, orig_trig, orig_present = (Autocomplete._on_idle, Autocomplete.trigger,
                                          Autocomplete._present)

    def on_idle(self: Any) -> None:
        S["ac_idle"] = S["text"]  # the pause began at the last text change
        orig_idle(self)

    def trigger(self: Any) -> None:
        S["ac_idle"] = now()
        orig_trig(self)

    def present(self: Any, suggestion: Any) -> None:
        orig_present(self, suggestion)
        if S["ac_idle"] is not None and self.visible:
            log_event("ghost_shown", ms_since_pause=ms(S["ac_idle"]), backend=backend_name(self))
            S["ac_idle"] = None

    Autocomplete._on_idle, Autocomplete.trigger = on_idle, trigger
    Autocomplete._present = present


def main() -> int:
    install()
    import os

    if os.environ.get("GLANCE_FAKE_COMPLETE") == "none":  # a backend with nothing to add
        from glance.daemon.main import FakeAiBackend

        FakeAiBackend.complete = lambda self, prefix, ctx: iter(())  # type: ignore[method-assign]
    from glance.daemon.main import main as glanced_main

    return glanced_main([])


if __name__ == "__main__":
    sys.exit(main())
