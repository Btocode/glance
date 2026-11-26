"""glance-capture on macOS: the AX port, the unchanged FocusTracker and WriteBackEngine, on
the main run loop (no NSApplication). `glance.capture.main.main` hands off to `main` here.

Same command line and behaviour as on Linux (`--print` prints snapshot metadata, never text).
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from typing import Any

from glance.log import log_event


def _screens() -> list[Any]:
    from AppKit import NSScreen

    from glance.platform.macos.geometry import from_cocoa

    screens = list(NSScreen.screens() or ())
    if not screens:
        return []
    primary_h = screens[0].frame().size.height
    out = []
    for s in screens:
        f = s.frame()
        out.append(from_cocoa(f.origin.x, f.origin.y, f.size.width, f.size.height, primary_h))
    return out


def ax_trusted(prompt: bool = False) -> bool:
    """Whether this process may use the Accessibility API (and, with `prompt`, ask macOS to
    show its dialog naming this executable)."""
    import ApplicationServices as AX

    if prompt:
        return bool(AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True}))
    return bool(AX.AXIsProcessTrusted())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="glance-capture")
    ap.add_argument("--print", dest="print_mode", action="store_true",
                    help="print snapshot metadata to stdout instead of connecting to glanced")
    ap.add_argument("--with-text-for-tests", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    with_text = args.with_text_for_tests and os.environ.get("GLANCE_TEST") == "1"

    from glance.capture.main import (
        RECONNECT_MS,
        Serialized,
        handle_command,
        printable,
        watch_snapshot,
    )
    from glance.capture.tracker import FocusTracker
    from glance.config import Config, ConfigError, ConfigWatcher
    from glance.ipc import JsonLineClient
    from glance.paths import capture_sock, config_path
    from glance.platform.macos.ax_port import AxPort
    from glance.platform.macos.ax_roles import mac_config
    from glance.platform.macos.runloop import ConsoleLoop, MacClock
    from glance.privacy.gate import PrivacyGate
    from glance.protocol import HELLO, RESET
    from glance.writeback.activity import TypingWatch
    from glance.writeback.engine import WriteBackEngine, normalize

    if not ax_trusted():
        # Nothing can be read; say so once and keep running so glanced does not restart us
        # in a loop. The doctor names the binary that needs the permission.
        log_event("ax_not_trusted", exe=os.path.basename(sys.executable))

    clock = MacClock()
    loop = ConsoleLoop()
    client: JsonLineClient | None = None
    connected = False
    watch = TypingWatch(fold=normalize)

    def emit(msg_type: str, payload: dict[str, Any]) -> None:
        watch_snapshot(watch, msg_type, payload)
        if args.print_mode:
            sys.stdout.write(printable(msg_type, payload, with_text) + "\n")
            sys.stdout.flush()
        elif connected and client is not None:
            try:
                client.send(msg_type, payload)
            except OSError:
                pass

    def on_config(cfg: Config) -> None:
        nonlocal gate
        gate = PrivacyGate(mac_config(cfg))
        tracker.set_gate(gate)
        log_event("capture_config_reloaded")

    def on_config_error(_err: ConfigError) -> None:
        log_event("capture_config_invalid")

    watcher = ConfigWatcher(config_path(), clock, on_config, on_config_error)
    port = AxPort(clock.every, _screens, clock.post)
    gate = PrivacyGate(mac_config(watcher.current))
    tracker = FocusTracker(port, gate, emit, clock)

    engine = keys = clip = None
    if not args.print_mode:
        from glance.platform.macos.clipboard import MacClipboard
        from glance.platform.macos.keys import MacKeys

        try:
            keys, clip = MacKeys(), MacClipboard()
        except Exception as e:  # noqa: BLE001 - capture is still useful read-only
            log_event("writeback_unavailable", kind=type(e).__name__)
        else:
            def focused_now() -> Any:
                # As on Linux: let focus changes that arrived during the apply reach the
                # tracker before it is trusted.
                ConsoleLoop.dispatch_pending()
                port._check_focus()  # the poll may not have run since the last key
                return tracker.current_field()

            engine = WriteBackEngine(
                focused_now, keys, clip,
                app_cfg=lambda bundle: watcher.current.app(bundle),
                sleep=ConsoleLoop.pumping_sleep,
                allowed=lambda meta: gate.evaluate(meta).allowed,
                watch=watch,
            )

    def is_terminal(bundle: str) -> bool:
        folded = bundle.casefold()
        return any(t and t.casefold() in folded
                   for t in mac_config(watcher.current).privacy.terminal_wm_classes)

    def answer(msg_type: str, payload: dict[str, Any]) -> None:
        if msg_type == RESET:
            try:
                port.reregister()
            except Exception as e:  # noqa: BLE001
                log_event("capture_reset_failed", kind=type(e).__name__)
            tracker.resync_all()
            return
        reply = handle_command(msg_type, payload, engine, is_terminal)
        if reply is not None:
            emit(*reply)

    on_message = Serialized(answer)

    def on_disconnect() -> None:
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
        emit(HELLO, {"role": "capture", "pid": os.getpid(), "ax_trusted": ax_trusted()})
        tracker.resync()

    if not args.print_mode:
        client = JsonLineClient(
            capture_sock(),
            on_message=lambda t, p: clock.post(lambda: on_message(t, p)),
            on_disconnect=lambda: clock.post(on_disconnect),
        )
        clock.post(try_connect)
        parent = os.getppid()

        def parent_alive() -> None:  # no PR_SET_PDEATHSIG on macOS
            if os.getppid() != parent:
                loop.stop()

        clock.every(1000, parent_alive)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: loop.stop())
    watcher.start()
    tracker.start()
    log_event("capture_started", mode="print" if args.print_mode else "socket")
    try:
        loop.run()
    finally:
        watcher.stop()
        port.stop()
        if client is not None:
            client.close()
    return 0
