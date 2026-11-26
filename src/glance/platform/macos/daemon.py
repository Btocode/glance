"""glanced on macOS: the same controller, AI, features and sockets as on Linux, on the main
NSRunLoop of an accessory NSApplication, with the AppKit / Carbon / AX adapters.

`glance.daemon.main.main` hands off to `main` here. Every part that decides anything is the
shared one; this module only assembles (compare `daemon/main.py:main`).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from glance.log import log_event


class CachedMacConfig:
    """`mac_config(watcher.current)`, rebuilt only when the watcher has a new Config."""

    def __init__(self, current: Callable[[], Any]) -> None:
        self._current = current
        self._src: Any = None
        self._out: Any = None

    def __call__(self) -> Any:
        from glance.platform.macos.ax_roles import mac_config

        cfg = self._current()
        if cfg is not self._src:
            self._src, self._out = cfg, mac_config(cfg)
        return self._out


def open_keychain() -> Any:
    from glance.backends.keystore import NoKeyStore

    try:
        from glance.platform.macos.keychain import KeychainKeyStore

        return KeychainKeyStore()
    except Exception as e:  # noqa: BLE001 - no Security bindings: the CLI backends still work
        log_event("keyring_unavailable", kind=type(e).__name__)
        return NoKeyStore()


def capture_hello_filter(on_message: Callable[[Any, str, dict], None],
                         seen: dict[str, Any]) -> Callable[[Any, str, dict], None]:
    """Remember what capture says about its Accessibility permission in its HELLO."""
    def handle(conn: Any, msg_type: str, payload: dict[str, Any]) -> None:
        if msg_type == "hello" and isinstance(payload.get("ax_trusted"), bool):
            seen["capture_ax_trusted"] = payload["ax_trusted"]
        on_message(conn, msg_type, payload)

    return handle


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="glanced", description="The Glance daemon. Control it with `glance`.",
    ).parse_args(argv)

    from glance.daemon.main import (
        _PROBLEMS,
        CAPTURE_FAILED,
        HARPER_FAILED,
        HARPER_MISSING,
        POLL_CHILDREN_MS,
        ConfigReporter,
        FakeAiBackend,
        SupervisedHarper,
        build_ai,
        build_issue_filter,
        daemon_answers,
        fake_backend_wanted,
        real_backends,
    )
    from glance.paths import (
        capture_sock,
        config_path,
        ctl_sock,
        dictionary_path,
        harper_bin,
        ignored_path,
        overrides_path,
        update_log_path,
        usage_path,
    )

    if daemon_answers(ctl_sock()):
        print("glanced is already running", file=sys.stderr)
        return 1

    import ApplicationServices as AX
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSEvent

    from glance.audience.overrides import Overrides
    from glance.config import Config, ConfigError, ConfigWatcher, load_config, update_config
    from glance.daemon.assemble import attach_features
    from glance.daemon.backend_ctl import BackendControl
    from glance.daemon.control import CtlRouter, handle_control
    from glance.daemon.controller import Controller
    from glance.daemon.links import CaptureLink, FieldPanelOverlay, PanelEscape
    from glance.daemon.panel_actions import PanelRouter
    from glance.daemon.supervisor import ChildSupervisor
    from glance.daemon.wiring import (
        AiSetup,
        RunBg,
        configure_api,
        hotkeys_status,
        set_suggestions,
        thread_runner,
        tone_hotkeys,
    )
    from glance.grammar.harper_native import make_client as make_grammar_client
    from glance.ipc import Conn, JsonLineServer
    from glance.platform.macos.carbon_hotkeys import CarbonHotkeys
    from glance.platform.macos.hotkeys_mac import mac_hotkeys, mac_system_shortcuts
    from glance.platform.macos.notify import MacNotifier
    from glance.platform.macos.overlay import MacGhost, MacOverlay
    from glance.platform.macos.power import on_battery
    from glance.platform.macos.runloop import MacClock
    from glance.platform.macos.selection import MacSelection
    from glance.platform.macos.session import MacSessionEvents, screen_locked
    from glance.platform.macos.tray import MacTray
    from glance.platform.macos.windows import SetupNotice, UsageWindow
    from glance.ui.hotkeys import bind_hotkeys
    from glance.update import CHECK_EVERY_S, CHECK_FIRST_S, UpdateState, launch_update

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    clock = MacClock()
    notifier = MacNotifier()
    reporter = ConfigReporter(notifier)
    try:
        load_config(config_path())
    except ConfigError as e:
        reporter.invalid(e, startup=True)

    def quit_app() -> None:
        app.stop_(None)
        # stop: takes effect after the next event; post one so it is now.
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            15, (0, 0), 0, 0, 0, None, 0, 0, 0)
        app.postEvent_atStart_(ev, True)

    def on_config(cfg: Config) -> None:
        reporter.valid()
        log_event("config_reloaded")
        if mac_hotkeys(cfg.general.hotkeys) != bound_chords:
            rebind()
        ai.refresh_status()
        run_ai(ai_supervisor.sync, lambda _done: None)
        if configure_api((backends, speculative), current_config().backend):
            run_ai(lambda: ai_supervisor.healths(True),
                   lambda _h: (ai.refresh_status(), setup.update_tray()))
        setup.update_tray()
        tray.set_auto_suggest(cfg.ui.suggestions == "auto")
        controller.refresh()

    watcher = ConfigWatcher(config_path(), clock, on_config,
                            lambda e: reporter.invalid(e, startup=False))
    mac_cfg = CachedMacConfig(lambda: watcher.current)

    hotkeys = CarbonHotkeys(clock.post)
    mac_overlay = MacOverlay(on_chip_click=lambda: controller.toggle_strip(),
                             on_panel_action=lambda action, args: panels.action(action, args),
                             clock=clock, use_key=lambda: watcher.current.ui.use_key)
    overlay = PanelEscape(mac_overlay, hotkeys, lambda: panels.escape(),
                          on_use=lambda: controller.use_strip(),
                          use_key=lambda: watcher.current.ui.use_key)
    field_overlay = FieldPanelOverlay(overlay, lambda: features.selection.is_open,
                                      lambda: features.selection.close())
    tray = MacTray(on_pause=lambda: handle_control(controller, "pause", {}),
                   on_resume=lambda: handle_control(controller, "resume", {}),
                   on_quit=quit_app, on_choose=lambda name: setup.choose(name),
                   on_setup=lambda: setup.open(),
                   on_auto_suggest=lambda on: set_suggestions(
                       update_config, config_path(), on, watcher.reload),
                   auto_suggest=watcher.current.ui.suggestions == "auto",
                   on_update=lambda: launch_update(notifier.notify, update_log_path()),
                   on_usage_open=lambda: refresh_usage(),
                   on_usage_details=lambda: show_usage_details())

    link = CaptureLink(clock.post)
    grammar_client = make_grammar_client(harper_bin(), dictionary_path())
    harper = SupervisedHarper(grammar_client,
                              on_change=lambda: clock.post(lambda: controller.recheck()))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glance-bg")
    ai_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="glance-ai")

    def runner(on: ThreadPoolExecutor) -> RunBg:
        def run(work: Callable[[], Any], done: Callable[[Any], None]) -> None:
            def job() -> None:
                try:
                    result = work()
                except Exception as e:  # noqa: BLE001
                    log_event("bg_error", kind=type(e).__name__)
                    return
                clock.post(lambda: done(result))

            on.submit(job)

        return run

    run_ai = runner(ai_pool)
    issue_filter, detector = build_issue_filter(dictionary_path(), ignored_path())
    controller = Controller(clock, mac_cfg, harper, issue_filter, field_overlay, link, notifier,
                            tray, runner(pool))
    link.attach(controller)

    overrides = Overrides(overrides_path())
    keys = open_keychain()
    if fake_backend_wanted():
        log_event("ai_test_backend")
        backends, speculative = {"fake": FakeAiBackend()}, {"fake": FakeAiBackend()}

        def current_config() -> Config:
            cfg = mac_cfg()
            return replace(cfg, backend=replace(cfg.backend, choice="fake", enabled=()))
    else:
        backends = real_backends(watcher.current.backend, keys)
        speculative = real_backends(watcher.current.backend, keys)

        def current_config() -> Config:
            return mac_cfg()

    ai, ai_supervisor = build_ai(
        controller, clock, current_config, run_ai, overlay, detector, backends,
        speculative, usage_path(), overrides, on_battery,
        lambda e: reporter.invalid(e, startup=True),
        on_status_changed=lambda _status: setup.update_tray())
    setup = AiSetup(ai_supervisor, current_config, keys, run_ai, clock.post,
                    lambda **kw: SetupNotice(notifier, **kw), tray, update_config, config_path)
    usage_window: list[Any] = []

    def refresh_usage() -> None:
        from glance.backends.usage import tray_lines

        run_ai(ai_supervisor.usage_report, lambda report: tray.set_usage(tray_lines(report)))

    def show_usage_details() -> None:
        from glance.backends.usage import render_report

        def show(text: str) -> None:
            if not usage_window:
                usage_window.append(UsageWindow())
            usage_window[0].show(text)

        run_ai(lambda: render_report(ai_supervisor.usage_report()), show)

    refresh_usage()
    features = attach_features(
        controller=controller, ai=ai, supervisor=ai_supervisor, cfg=current_config,
        grammar=harper, issue_filter=issue_filter,
        ghost=MacGhost(watcher.current.ui.ghost_font_factor), hotkeys=hotkeys, clock=clock,
        run_ai=run_ai, language=detector.is_mixed, selection_port=MacSelection(),
        overlay=overlay, capture=link, notifier=notifier)
    panels = PanelRouter(controller, ai, overrides.set, features.selection, features.guard)
    ctl = CtlRouter(
        controller, panels.action,
        backend_control=BackendControl(ai_supervisor, ai, keys, current_config, overrides,
                                       run_ai, config_path()))

    def toggle_panel() -> None:
        if overlay.panel_visible:
            if features.selection.is_open:
                features.selection.close()
            else:
                controller.close_panel()
        else:
            controller.open_panel()

    actions: dict[str, Callable[[], None]] = {
        "panel": toggle_panel,
        "fix_all": controller.fix_all_safe,
        "revert": controller.revert_last,
        "pause": controller.toggle_pause,
        **tone_hotkeys(ai),
        **features.actions,
    }
    bound_chords: dict[str, str] = {}
    hotkey_problems: dict[str, str] = {}

    def rebind() -> None:
        nonlocal bound_chords
        chords = bound_chords = mac_hotkeys(watcher.current.general.hotkeys)
        run_ai(mac_system_shortcuts, lambda system: bind(chords, system))

    def bind(chords: dict[str, str], system: Any) -> None:
        nonlocal hotkey_problems
        if chords != bound_chords:
            return
        problems = bind_hotkeys(hotkeys, chords, actions, system or ())
        if problems and problems != hotkey_problems:
            lines = [f"{chords[name]} ({name}): {_PROBLEMS[why]}"
                     for name, why in problems.items()]
            notifier.notify("Glance: some shortcuts are not available",
                            "\n".join(lines) + "\nChange them under [general.hotkeys] in "
                            "config.toml.")
        hotkey_problems = problems
        log_event("hotkeys_bound", grabbed=len(hotkeys.grabbed()))

    failed_told: set[str] = set()

    def on_child_state(name: str, message: tuple[str, str], state: str) -> None:
        if state == "failed" and name not in failed_told:
            failed_told.add(name)
            notifier.notify(*message)
        elif state == "up":
            failed_told.discard(name)
        controller.refresh()

    capture_sup = ChildSupervisor(
        "capture", lambda: subprocess.Popen([sys.executable, "-m", "glance.capture.main"]),
        clock, lambda state: on_child_state("capture", CAPTURE_FAILED, state))
    harper_engine = getattr(grammar_client, "engine", "harper-ls")
    harper_missing = harper_engine == "harper-ls" and not harper_bin().exists()
    harper_sup = ChildSupervisor(
        "harper", harper.spawn, clock,
        lambda state: on_child_state(
            "harper", HARPER_MISSING if harper_missing else HARPER_FAILED, state))

    class _CaptureWatch:
        def on_capture_lost(self) -> None:
            clock.call_later(50, capture_sup.poll)

    controller.add_listener(_CaptureWatch())

    class _ResumeReset:
        def on_reset(self) -> None:
            ai_supervisor.cancel_all()
            rebind()

            def refresh() -> list[Any]:
                for backend in (*backends.values(), *speculative.values()):
                    if hasattr(backend, "set_warm"):
                        backend.set_warm(False)
                ai_supervisor.sync()
                return list(ai_supervisor.healths(True))

            run_ai(refresh, lambda _h: (ai.refresh_status(), setup.update_tray()))

    controller.add_listener(_ResumeReset())
    session_events = MacSessionEvents(
        on_sleep=lambda: log_event("suspending"),
        on_resume=controller.handle_resume,
        on_lock=controller.handle_lock,
        on_monitors_changed=controller.handle_monitors_changed)
    seen: dict[str, Any] = {}
    controller.extra_status.append(lambda: {
        "platform": "macos",
        "ax_trusted": bool(AX.AXIsProcessTrusted()),
        "capture_ax_trusted": seen.get("capture_ax_trusted"),
        "glanced_exe": os.path.realpath(sys.executable),
        "capture_child": capture_sup.state,
        "harper_child": harper_sup.state,
        "grammar_engine": harper_engine,
        **hotkeys_status(hotkeys.grabbed(), bound_chords),
        "hotkeys_unavailable": ",".join(sorted(hotkey_problems)) or None,
        "tray": tray.available,
        **reporter.status(),
    })

    def test_message(msg_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """`ax_prompt`, and (GLANCE_TEST only) what the macOS tests ask besides `test_overlay`."""
        if msg_type == "ax_prompt":  # `glance doctor --fix`: the dialog names glanced's binary
            return {"ok": True, "trusted": bool(AX.AXIsProcessTrustedWithOptions(
                {AX.kAXTrustedCheckOptionPrompt: bool(payload.get("prompt"))}))}
        if os.environ.get("GLANCE_TEST") != "1":
            return None
        if msg_type == "test_windows":
            return {"ok": True, **mac_overlay.window_numbers()}
        return None

    def ctl_handle(msg_type: str, payload: dict[str, Any], send: Any) -> None:
        from glance.daemon.test_overlay import handle_test_overlay

        reply_id = {"id": payload["id"]} if "id" in payload else {}
        extra = test_message(msg_type, payload)
        if extra is not None:
            send({**extra, **reply_id})
            return
        shown = handle_test_overlay(msg_type, payload, overlay, controller)
        if shown is None:
            ctl.handle(msg_type, payload, send)
        else:
            send({**shown, **reply_id})

    def on_ctl(conn: Conn, msg_type: str, payload: dict[str, Any]) -> None:
        clock.post(lambda: ctl_handle(msg_type, payload,
                                      lambda reply: conn.send(msg_type, reply)))

    capture_server = JsonLineServer(capture_sock(), capture_hello_filter(link.on_message, seen),
                                    link.on_disconnect)
    ctl_server = JsonLineServer(ctl_sock(), on_ctl)

    def poll_children() -> None:
        capture_sup.poll()
        harper_sup.poll()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: clock.post(quit_app))
    clock.every(250, lambda: None)  # back in Python often enough for signal handlers

    try:
        capture_server.start()
        ctl_server.start()
    except OSError as e:
        print(f"glanced: cannot create its sockets in {ctl_sock().parent}: {e}",
              file=sys.stderr)
        capture_server.stop()
        hotkeys.close()
        return 1
    update_state = UpdateState()
    run_update_check = thread_runner(clock.post, "glance-update-check")

    def update_check() -> None:
        if watcher.current.general.check_updates:
            run_update_check(update_state.refresh,
                             lambda _s: tray.set_update_available(update_state.status == "available"))

    def first_update_check() -> None:
        update_check()
        clock.every(CHECK_EVERY_S * 1000, update_check)

    clock.call_later(CHECK_FIRST_S * 1000, first_update_check)
    controller.extra_status.append(lambda: {"update": update_state.status})
    rebind()
    session_events.start()
    if screen_locked():
        controller.handle_lock(True)
    watcher.start()
    harper_sup.start()
    capture_sup.start()
    clock.every(POLL_CHILDREN_MS, poll_children)
    controller.refresh()
    setup.update_tray()
    if not AX.AXIsProcessTrusted():
        log_event("ax_not_trusted", exe=os.path.basename(sys.executable))
    log_event("glanced_started", tray=tray.available, platform="macos")
    try:
        app.run()
    finally:
        log_event("glanced_stopping")
        watcher.stop()
        capture_sup.stop()
        harper_sup.stop()
        ctl_server.stop()
        capture_server.stop()
        hotkeys.close()
        ai_supervisor.cancel_all()
        for backend in (*backends.values(), *speculative.values()):
            stop = getattr(backend, "stop", None) or getattr(backend, "close", None)
            if stop is not None:
                try:
                    stop()
                except Exception as e:  # noqa: BLE001
                    log_event("backend_stop_failed", kind=type(e).__name__)
        pool.shutdown(wait=False, cancel_futures=True)
        ai_pool.shutdown(wait=False, cancel_futures=True)
        notifier.close()
    return 0
