"""glance-capture: follows the focused field and reports it to glanced.

    glance-capture            connect to glanced's capture socket
    glance-capture --print    print SNAPSHOT / FOCUS_LOST lines to stdout instead; the text
                              is replaced by its length and a short hash
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from glance.log import fingerprint, log_event
from glance.protocol import (
    APPLY,
    APPLY_RESULT,
    HELLO,
    PASTE,
    RESET,
    REVERT,
    SNAPSHOT,
    ApplyRequest,
    ApplyResult,
)
from glance.writeback.activity import TypingWatch
from glance.writeback.engine import PASTE_ID, normalize

RECONNECT_MS = 500


def die_with_parent() -> None:
    """A kill -9 of glanced must not leave this process serving a dead socket: SIGTERM
    when the parent goes (PR_SET_PDEATHSIG = 1), and at once if it is already gone."""
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:  # noqa: BLE001 - not Linux / no libc: capture just outlives it
        return
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)


def printable(msg_type: str, payload: dict[str, Any], with_text: bool = False) -> str:
    """One stdout line for `--print`; the field text is never in it unless `with_text`."""
    if msg_type == SNAPSHOT and not with_text:
        payload = {**payload, "text": fingerprint(payload.get("text", ""))}
    return json.dumps({"t": msg_type, "p": payload}, ensure_ascii=False)


def watch_snapshot(watch: TypingWatch, msg_type: str, payload: dict[str, Any]) -> None:
    """Show the watch every ungated snapshot capture sends: a changed text is typing."""
    if msg_type != SNAPSHOT or payload.get("gated"):
        return
    field_id, text = payload.get("field_id"), payload.get("text")
    if isinstance(field_id, str) and isinstance(text, str):
        watch.observe(field_id, text)


def _apply_request(payload: dict[str, Any]) -> ApplyRequest | None:
    """The request in an APPLY payload, or None if it is not a well-formed one."""
    try:
        req = ApplyRequest.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None
    strings = (req.apply_id, req.field_id, req.expected_sha256, req.method,
               req.edit.replacement)
    return req if all(isinstance(v, str) for v in strings) else None


def _paste(payload: dict[str, Any], engine: Any,
           is_terminal: Callable[[str], bool]) -> ApplyResult:
    """An unverified paste for select-anything. Never into a terminal, whatever glanced says."""
    text, window, wm_class = (payload.get("text"), payload.get("window", 0),
                              payload.get("wm_class", ""))
    if not (isinstance(text, str) and isinstance(window, int) and isinstance(wm_class, str)):
        return ApplyResult(PASTE_ID, "failed", "bad request")
    if engine is None:
        return ApplyResult(PASTE_ID, "failed", "writeback unavailable")
    if is_terminal(wm_class):
        return ApplyResult(PASTE_ID, "refused", "no paste into a terminal")
    try:
        result = engine.paste_unverified(text, window)
    except Exception as e:  # noqa: BLE001 - the daemon must still get its answer
        log_event("writeback_error", kind=type(e).__name__)
        return ApplyResult(PASTE_ID, "failed", "internal error; use Copy instead")
    return ApplyResult(PASTE_ID, result.status, result.detail)


def handle_command(
    msg_type: str, payload: dict[str, Any], engine: Any = None,
    is_terminal: Callable[[str], bool] = lambda _wm_class: False,
) -> tuple[str, dict[str, Any]] | None:
    """Answer a message from glanced; None when it needs no reply.

    `engine` is the write-back engine; without one every APPLY / REVERT / PASTE fails
    cleanly. Each of them always gets exactly one APPLY_RESULT, whatever goes wrong.
    `is_terminal` says whether a window class is a terminal (no paste there).
    """
    if msg_type == PASTE:
        reply = _paste(payload, engine, is_terminal).to_dict()
        if "id" in payload:
            reply["id"] = payload["id"]
        return APPLY_RESULT, reply
    if msg_type not in (APPLY, REVERT):
        return None
    apply_id = payload.get("apply_id")
    apply_id = apply_id if isinstance(apply_id, str) else ""
    if engine is None:
        result = ApplyResult(apply_id, "failed", "writeback unavailable")
    else:
        try:
            if msg_type == REVERT:
                result = engine.revert_last()
            else:
                req = _apply_request(payload)
                result = (engine.apply(req) if req is not None
                          else ApplyResult(apply_id, "failed", "bad request"))
        except Exception as e:  # noqa: BLE001 - the daemon must still get its answer
            log_event("writeback_error", kind=type(e).__name__)
            result = ApplyResult(apply_id, "failed", "internal error; use Copy instead")
    reply = result.to_dict()
    if "id" in payload:
        reply["id"] = payload["id"]
    return APPLY_RESULT, reply


class Serialized:
    """Calls `fn` for one message at a time, in arrival order.

    A message that arrives while `fn` is still running (the engine lets the main loop
    dispatch between its steps) waits its turn instead of running nested inside it.
    """

    def __init__(self, fn: Callable[..., None]) -> None:
        self._fn = fn
        self._backlog: deque[tuple[Any, ...]] = deque()
        self._busy = False

    def __call__(self, *args: Any) -> None:
        self._backlog.append(args)
        if self._busy:
            return
        self._busy = True
        try:
            while self._backlog:
                self._fn(*self._backlog.popleft())
        finally:
            self._busy = False


def main(argv: list[str] | None = None) -> int:
    # >>> macOS: capture runs on the Accessibility API there (src/glance/platform/macos/capture.py)
    if sys.platform == "darwin":
        from glance.platform.macos.capture import main as macos_main

        return macos_main(argv)
    # <<< macOS
    ap = argparse.ArgumentParser(prog="glance-capture")
    ap.add_argument("--print", dest="print_mode", action="store_true",
                    help="print snapshot metadata to stdout instead of connecting to glanced")
    ap.add_argument("--with-text-for-tests", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    with_text = args.with_text_for_tests and os.environ.get("GLANCE_TEST") == "1"
    if not args.print_mode:
        die_with_parent()

    from gi.repository import GLib

    from glance.capture.atspi_port import AtspiPort
    from glance.capture.ports import A11yField
    from glance.capture.tracker import FocusTracker
    from glance.clock import GLibClock
    from glance.config import Config, ConfigError, ConfigWatcher
    from glance.ipc import JsonLineClient
    from glance.paths import capture_sock, config_path
    from glance.privacy.gate import PrivacyGate

    clock = GLibClock()
    loop = GLib.MainLoop()
    client: JsonLineClient | None = None
    connected = False
    watch = TypingWatch(fold=normalize)  # when the user last typed: no write-back then

    def emit(msg_type: str, payload: dict[str, Any]) -> None:
        watch_snapshot(watch, msg_type, payload)
        if args.print_mode:
            sys.stdout.write(printable(msg_type, payload, with_text) + "\n")
            sys.stdout.flush()
        elif connected and client is not None:
            try:
                client.send(msg_type, payload)
            except OSError:
                pass  # the reader thread reports the disconnect

    def on_config(cfg: Config) -> None:
        nonlocal gate
        gate = PrivacyGate(cfg)
        tracker.set_gate(gate)
        log_event("capture_config_reloaded")

    def on_config_error(err: ConfigError) -> None:
        log_event("capture_config_invalid")

    watcher = ConfigWatcher(config_path(), clock, on_config, on_config_error)
    from glance.session import GNOME_WAYLAND, session_kind

    wayland = session_kind(os.environ) == GNOME_WAYLAND
    bus: Any = None
    if wayland:
        # GNOME on Wayland: windows, keys and the clipboard come from Glance's shell extension
        from glance.capture.windows_shell import ShellScreen
        from glance.shellbus import CAPTURE_NAME, GioShellBus, ShellUnavailable, own_name

        screens: list[Any] = []

        def on_extension(available: bool) -> None:
            if available and screens:
                try:
                    screens[0].on_state(bus.call("FocusedWindow"))
                except ShellUnavailable:
                    pass

        bus = GioShellBus(on_change=on_extension)
        # The extension serves only the owners of Glance's names, checked by uid and
        # command line: take ours before the first call.
        own_name(bus.connection, CAPTURE_NAME)
        screens.append(ShellScreen(bus))
        port = AtspiPort(screens[0])
    else:
        port = AtspiPort()
    gate = PrivacyGate(watcher.current)
    tracker = FocusTracker(port, gate, emit, clock)

    engine = keys = clip = None
    if not args.print_mode:
        from glance.writeback.engine import WriteBackEngine

        try:
            if wayland:
                from glance.writeback.clipboard_shell import ShellClipboard
                from glance.writeback.keys_shell import ShellKeys

                keys, clip = ShellKeys(bus), ShellClipboard(bus)
            else:
                from glance.writeback.clipboard_xlib import XlibClipboard
                from glance.writeback.keys_xlib import XlibKeys

                keys, clip = XlibKeys(), XlibClipboard()
        except Exception as e:  # noqa: BLE001 - capture is still useful read-only
            log_event("writeback_unavailable", kind=type(e).__name__)
        else:
            context = GLib.MainContext.default()

            def focused_now() -> A11yField | None:
                # The engine runs inside a main-loop callback, and focus changes reach the
                # tracker through that loop. Dispatch what has arrived first, or the engine
                # would be told about a field that lost the focus while it was working and
                # would send its keys to whatever has it now.
                while context.pending():
                    context.iteration(False)
                return tracker.current_field()

            engine = WriteBackEngine(
                focused_now, keys, clip,
                app_cfg=lambda wm_class: watcher.current.app(wm_class),
                sleep=time.sleep,
                allowed=lambda meta: gate.evaluate(meta).allowed,
                watch=watch,
            )

    def reset_listeners() -> None:
        try:
            port.reregister()
        except Exception as e:  # noqa: BLE001 - a dead a11y bus must not end capture
            log_event("capture_reset_failed", kind=type(e).__name__)
        tracker.resync_all()
        log_event("capture_reset")

    def is_terminal(wm_class: str) -> bool:
        folded = wm_class.casefold()
        return any(t and t.casefold() in folded
                   for t in watcher.current.privacy.terminal_wm_classes)

    def answer(msg_type: str, payload: dict[str, Any]) -> None:
        if msg_type == RESET:  # glanced woke from suspend
            reset_listeners()
            return
        reply = handle_command(msg_type, payload, engine, is_terminal)
        if reply is not None:
            emit(*reply)

    on_message = Serialized(answer)  # main loop; an apply takes up to a few seconds

    def on_disconnect() -> None:  # main loop
        nonlocal connected
        if connected:
            connected = False
            log_event("capture_disconnected")
            clock.call_later(RECONNECT_MS, try_connect)

    def try_connect() -> None:
        nonlocal connected
        assert client is not None
        try:
            client.connect(timeout_s=0)
        except ConnectionError:
            clock.call_later(RECONNECT_MS, try_connect)
            return
        connected = True
        log_event("capture_connected")
        emit(HELLO, {"role": "capture", "pid": os.getpid()})
        tracker.resync()

    if not args.print_mode:
        # The client calls back on its reader thread; everything else lives on the main loop.
        client = JsonLineClient(
            capture_sock(),
            on_message=lambda t, p: clock.post(lambda: on_message(t, p)),
            on_disconnect=lambda: clock.post(on_disconnect),
        )
        clock.post(try_connect)

    def quit_loop() -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, quit_loop)
    watcher.start()
    tracker.start()
    log_event("capture_started", mode="print" if args.print_mode else "socket")
    try:
        loop.run()
    finally:
        watcher.stop()
        port.stop()
        if keys is not None:
            keys.close()
        if clip is not None:
            clip.close()
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
