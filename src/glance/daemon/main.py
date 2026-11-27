"""glanced: scheduling, Harper, the overlay, hotkeys and the tray, in one GTK main loop.

Everything above `main()` is plain glue with no GTK in it; `main()` builds the real adapters.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from glance.config import ConfigError
from glance.daemon.assemble import attach_features
from glance.daemon.control import CtlRouter, handle_control
from glance.daemon.links import CaptureLink, FieldPanelOverlay, PanelEscape
from glance.daemon.panel_actions import PanelRouter
from glance.daemon.teardown import Teardown
from glance.daemon.wiring import (
    AUTO_CHOOSE_EVERY_MS,
    AUTO_CHOOSE_FIRST_MS,
    SESSION_EXIT,
    AiSetup,
    RunBg,
    check_label,
    configure_api,
    hotkeys_status,
    repeat,
    session_problem,
    set_check_mode,
    set_suggestions,
    thread_runner,
    tone_hotkeys,
)
from glance.feature_switches import menu_rows, set_feature
from glance.log import log_event
from glance.paths import update_log_path
from glance.ui.ports import NotifierPort
from glance.update import CHECK_EVERY_S, CHECK_FIRST_S, UpdateState, launch_update

POLL_CHILDREN_MS = 100  # restart <= 2 s: a death is seen within 100 ms
HARPER_MISSING = ("Glance: the grammar checker is not installed",
                  "Run scripts/fetch-harper.sh in the Glance folder, then start glanced again.")
HARPER_FAILED = ("Glance: the grammar checker keeps stopping",
                 "Glance retries every 30 seconds. Grammar fixes are off until it is back.")
CAPTURE_FAILED = ("Glance cannot read text fields right now",
                  "Its capture helper keeps stopping. Glance retries every 30 seconds.")
_PROBLEMS = {"conflict": "another program uses it", "system": "it is a system shortcut",
             "invalid": "not a valid shortcut"}


# ---------------------------------------------------------------------- single instance


def daemon_answers(path: Path) -> bool:
    """True when a glanced is listening on `path` (a socket file nobody serves is not one)."""
    from glance.ipc import JsonLineClient

    client = JsonLineClient(path)
    try:
        client.connect(timeout_s=0)
    except ConnectionError:
        return False
    try:
        client.request("ping", {}, timeout_s=1.0)
    except ConnectionError:
        return False
    except TimeoutError:
        return True  # it accepted the connection: alive, only busy
    finally:
        client.close()
    return True


# ---------------------------------------------------------------------- AI


class FakeAiBackend:
    """Stands in for every AI backend when the daemon runs with GLANCE_TEST=1 and
    GLANCE_FAKE_BACKEND=1: the desktop tests never make a real call."""

    name = "fake"
    REWRITE = "Thanks so much, will do!"

    def analyze(self, text: str, ctx: Any) -> Any:
        from glance.backends.base import Analysis

        return Analysis("neutral", False, None, ())

    def rewrite(self, text: str, tone: str, ctx: Any, custom: str | None = None) -> Any:
        yield self.REWRITE

    def complete(self, prefix: str, ctx: Any) -> Any:
        yield " tomorrow morning."

    def cancel(self) -> None:
        pass

    def health(self) -> Any:
        from glance.backends.base import Health

        return Health(True, True, "test backend")


def fake_backend_wanted(env: Any = None) -> bool:
    env = os.environ if env is None else env
    return env.get("GLANCE_TEST") == "1" and env.get("GLANCE_FAKE_BACKEND") == "1"


def open_keystore() -> Any:
    """The system keyring for API keys; a store that saves nothing when there is none."""
    from glance.backends.keystore import NoKeyStore, SecretKeyStore

    try:
        return SecretKeyStore()
    except Exception as e:  # noqa: BLE001 - no libsecret: the other backends still work
        log_event("keyring_unavailable", kind=type(e).__name__)
        return NoKeyStore()


def real_backends(cfg: Any, keys: Any = None) -> dict[str, Any]:
    """One instance of every backend. Each call builds new ones, with their own process
    runners, so the supervisor's two lanes never share (and never cancel) each other's.
    `keys` is the one keyring every api instance (and `glance backend set-key`) uses."""
    from glance.backends.api import ApiBackend
    from glance.backends.claude import ClaudeBackend
    from glance.backends.codex import CodexBackend
    from glance.backends.gemini import GeminiBackend
    from glance.backends.opencode import OpencodeBackend
    from glance.backends.proc import ProcRunner

    models = cfg.models
    out: dict[str, Any] = {
        "claude": ClaudeBackend(ProcRunner(), models.get("claude", "haiku")),
        "codex": CodexBackend(ProcRunner(), models.get("codex", "")),
        "gemini": GeminiBackend(ProcRunner(), models.get("gemini", "")),
        "opencode": OpencodeBackend(ProcRunner(), models.get("opencode", "")),
    }
    try:
        out["api"] = ApiBackend(keys if keys is not None else open_keystore(),
                                cfg.api_provider, cfg.api_model, cfg.api_base_url)
    except Exception as e:  # noqa: BLE001 - a bad api setting: the other backends still work
        log_event("api_backend_unavailable", kind=type(e).__name__)
    return out


def build_issue_filter(dictionary_file: Path, ignored_file: Path) -> tuple[Any, Any]:
    """The IssueFilter with Banglish-aware suppression, and the detector behind it."""
    from glance.grammar.filters import IssueFilter, code_like_spans
    from glance.grammar.lang import LanguageDetector
    from glance.grammar.store import IgnoredRules, PersonalDictionary

    dictionary = PersonalDictionary(dictionary_file)
    detector = LanguageDetector(dictionary)
    return (IssueFilter(dictionary, IgnoredRules(ignored_file), [code_like_spans],
                        issue_span_filters=[detector.spans]), detector)


def build_ai(controller: Any, clock: Any, cfg: Callable[[], Any],
             run_bg: Callable[[Callable[[], Any], Callable[[Any], None]], None],
             overlay: Any, detector: Any, backends: dict[str, Any],
             speculative: dict[str, Any], usage_file: Path, overrides: Any,
             on_battery: Callable[[], bool], on_profiles_error: Callable[[ConfigError], None],
             on_status_changed: Callable[[str], None] | None = None) -> tuple[Any, Any]:
    """The supervisor and the attached AiCoordinator. `cfg()` is the whole current Config.

    `run_bg` must run its work off the main loop and never on the pool the grammar checks
    use: a rewrite can take 20 s. The supervisor reports status changes on whatever thread
    saw them; they reach the coordinator (then `on_status_changed`) on the main loop through
    `clock.post`.
    """
    from glance.audience.profiles import BUILTIN, load_profiles
    from glance.audience.resolver import DEFAULT_RULES, AudienceResolver
    from glance.backends.budget import Budget
    from glance.backends.supervisor import BackendSupervisor
    from glance.backends.usage import UsageLedger
    from glance.daemon.ai import AiCoordinator
    from glance.daemon.cache import LruCache
    from glance.daemon.rewrite import is_rich_editor

    try:
        profiles = load_profiles(cfg().raw)
    except ConfigError as e:
        on_profiles_error(e)
        profiles = dict(BUILTIN)
    holder: list[Any] = []

    def status_changed(status: str) -> None:  # main loop
        if holder:
            holder[0].on_ai_status(status)
        if on_status_changed is not None:
            on_status_changed(status)

    def on_status(status: str) -> None:  # any thread
        clock.post(lambda: status_changed(status))

    supervisor = BackendSupervisor(
        backends, lambda: cfg().backend, Budget(clock, lambda: cfg().backend, usage_file),
        clock, on_battery, on_status, speculative=speculative,
        ledger=UsageLedger(usage_file.with_name("usage-ledger.json")),
        cost_limit=lambda: cfg().ai.monthly_cost_limit_usd)
    ai = AiCoordinator(
        controller, supervisor, AudienceResolver(profiles, DEFAULT_RULES, overrides),
        LruCache(), clock, run_bg, language=detector.is_mixed, overlay=overlay,
        rich=lambda s: is_rich_editor(s.wm_class, cfg()),
        writeback=lambda s: cfg().app(s.wm_class).writeback,
        max_words=lambda: cfg().ai.max_words,
        correction=lambda: cfg().general.correction,
        checker=lambda: cfg().general.checker)
    holder.append(ai)
    ai.attach()
    return ai, supervisor


# ---------------------------------------------------------------------- leaving


def hide_everything(overlay: Any, ghost: Any, shell: Any) -> None:
    """Take the strip, panel and ghost down (and with them the Esc and use-key grabs)."""
    hides = [overlay.hide_strip, overlay.hide_panel] + ([ghost.hide] if ghost else [])
    for fn in hides:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - leaving must not fail
            log_event("teardown_hide_failed", kind=type(e).__name__)
    if shell is not None and shell.available:
        # the shell overlay draws through the main loop, which has ended: clear it directly
        from glance.shellbus import ShellUnavailable

        try:
            shell.call("Render", {}, timeout_ms=300)
        except ShellUnavailable:
            pass


def stop_all(stops: Any) -> None:
    """Stop several children at the same time: each waits up to a second for its own."""
    threads = [threading.Thread(target=_guarded, args=(fn,), name="glance-stop", daemon=True)
               for fn in stops]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _guarded(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - leaving must not fail
        log_event("child_stop_failed", kind=type(e).__name__)


def stop_backends(backends: Any) -> None:
    """opencode's private server, claude's idle processes: all at once, none waited on
    for long (their stop escalates to SIGKILL by itself)."""
    stops = []
    for backend in backends:
        stop = getattr(backend, "stop", None) or getattr(backend, "close", None)
        if stop is not None:
            stops.append(stop)
    stop_all(stops)


# ---------------------------------------------------------------------- harper


def _in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="glance-harper-start", daemon=True).start()


class _HarperRun:
    """One life of harper-ls, shaped like a Popen for ChildSupervisor."""

    def __init__(self, owner: SupervisedHarper) -> None:
        self._owner = owner
        self._state = "starting"  # starting | running | dead
        self._cancelled = False
        owner._run_thread(self._start)

    def _start(self) -> None:
        client = self._owner._client
        try:
            client.stop()  # whatever an earlier life left behind
            client.start()
        except Exception as e:  # noqa: BLE001 - a missing binary, a timeout, a broken pipe
            log_event("harper_start_failed", kind=type(e).__name__)
            self._stop_quietly()  # a start that timed out leaves its child running
            self._state = "dead"
            return
        if self._cancelled:
            self._stop_quietly()
            return
        self._state = "running"
        self._owner._set_ready(True)

    def _stop_quietly(self) -> None:
        try:
            self._owner._client.stop()
        except Exception as e:  # noqa: BLE001
            log_event("harper_stop_failed", kind=type(e).__name__)

    def poll(self) -> int | None:
        if self._state == "starting":
            return None
        if self._state == "running" and self._owner._client.alive:
            return None
        self._state = "dead"
        self._owner._set_ready(False)
        return 1

    def terminate(self) -> None:
        self._cancelled = True
        self._state = "dead"
        self._owner._set_ready(False)
        self._stop_quietly()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        pass


class SupervisedHarper:
    """GrammarPort over a HarperClient that a ChildSupervisor restarts through `spawn`.

    `alive` is true only for a client that finished starting and still runs, so the
    controller shows the degraded state for a dead, restarting or half-started harper-ls.
    Starting happens on a thread (it can take seconds); `on_change` is called, possibly on
    that thread, whenever `alive` may have changed.
    """

    def __init__(self, client: Any, on_change: Callable[[], None],
                 run_thread: Callable[[Callable[[], None]], None] = _in_thread) -> None:
        self._client = client
        self._on_change = on_change
        self._run_thread = run_thread
        self._ready = False

    @property
    def alive(self) -> bool:
        return self._ready and bool(self._client.alive)

    def check(self, text: str) -> list[Any]:
        return self._client.check(text) if self.alive else []

    def spawn(self) -> _HarperRun:
        return _HarperRun(self)

    def _set_ready(self, ready: bool) -> None:
        if ready != self._ready:
            self._ready = ready
            self._on_change()


# ---------------------------------------------------------------------- config problems


class ConfigReporter:
    """Tells the user about an invalid config.toml, once per failed load."""

    def __init__(self, notifier: NotifierPort) -> None:
        self._notifier = notifier
        self._bad = False

    def invalid(self, err: ConfigError, startup: bool) -> None:
        self._bad = True
        log_event("config_invalid", startup=startup)
        kept = ("Glance is using the default settings until it is fixed." if startup
                else "Glance keeps the previous settings until it is fixed.")
        self._notifier.notify("Glance: config.toml has an error",
                              f"{err.key}: {err.reason}. {kept}")

    def valid(self) -> None:
        self._bad = False

    def status(self) -> dict[str, Any]:
        return {"config_ok": not self._bad}


# ---------------------------------------------------------------------- the daemon


class _DesktopNotifier:
    """NotifierPort over libnotify. Showing one is a D-Bus round trip, so it happens on a
    thread of its own, never on the main loop; the notifications keep their order."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Notify", "0.7")
        from gi.repository import GLib, Notify

        self._notify, self._error = Notify, GLib.Error
        self._ok = bool(Notify.init("Glance"))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glance-notify")

    def notify(self, summary: str, body: str) -> None:
        log_event("notify")
        if self._ok:
            self._pool.submit(self._show, summary, body)

    def _show(self, summary: str, body: str) -> None:
        try:
            self._notify.Notification.new(summary, body, "tools-check-spelling").show()
        except self._error as e:  # no notification service: nothing to show it on
            log_event("notify_failed", kind=type(e).__name__)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def main(argv: list[str] | None = None) -> int:
    # >>> macOS: glanced runs on AppKit there (src/glance/platform/macos/daemon.py)
    if sys.platform == "darwin":
        from glance.platform.macos.daemon import main as macos_main

        return macos_main(argv)
    # <<< macOS
    argparse.ArgumentParser(
        prog="glanced", description="The Glance daemon. Control it with `glance`.",
    ).parse_args(argv)
    problem = session_problem(os.environ)
    if problem is not None:
        print(problem, file=sys.stderr)
        return SESSION_EXIT

    from glance.paths import (
        capture_sock,
        config_path,
        ctl_sock,
        dictionary_path,
        harper_bin,
        ignored_path,
        overrides_path,
        usage_path,
    )

    if daemon_answers(ctl_sock()):
        print("glanced is already running", file=sys.stderr)
        return 1

    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    from glance.audience.overrides import Overrides
    from glance.clock import GLibClock
    from glance.config import Config, ConfigWatcher, load_config, update_config
    from glance.daemon.backend_ctl import BackendControl
    from glance.daemon.controller import Controller
    from glance.daemon.power_dbus import on_battery
    from glance.daemon.session import SessionEvents
    from glance.daemon.supervisor import ChildSupervisor
    from glance.grammar.harper_native import make_client as make_grammar_client
    from glance.ipc import Conn, JsonLineServer
    from glance.session import GNOME_WAYLAND, session_kind
    from glance.ui.edit_window_gtk import gtk_edit_host
    from glance.ui.hotkeys import bind_hotkeys, system_shortcuts
    from glance.ui.onboarding_gtk import OnboardingWindow
    from glance.ui.tray_gtk import INSTALL_HINT, GtkTray

    wayland = session_kind(os.environ) == GNOME_WAYLAND
    if not Gtk.init_check(None)[0]:
        print("glanced: cannot open the display", file=sys.stderr)
        return 1

    clock = GLibClock()
    notifier = _DesktopNotifier()
    reporter = ConfigReporter(notifier)
    try:
        load_config(config_path())
    except ConfigError as e:  # the watcher starts from the defaults; the user must know why
        reporter.invalid(e, startup=True)

    def on_config(cfg: Config) -> None:
        reporter.valid()
        log_event("config_reloaded")
        if cfg.general.hotkeys != bound_chords:
            rebind()
        ai.refresh_status()  # the chosen backend may have changed
        # models and claude's warm process follow the new config now, not at the next call
        run_ai(ai_supervisor.sync, lambda _done: None)
        if configure_api((backends, speculative), current_config().backend):
            # a new provider/key pairing: health must be asked again, not read from cache
            run_ai(lambda: ai_supervisor.healths(True),
                   lambda _h: (ai.refresh_status(), setup.update_tray()))
        setup.update_tray()
        auto_choose()  # the choice may have been removed, or set to one that is gone
        tray.set_auto_suggest(cfg.ui.suggestions == "auto")
        tray.set_check_mode(cfg.general.check)
        tray.set_features(menu_rows(cfg))
        features.actions_bar.sync()  # [selection] enabled: the watcher starts or stops
        shot_switch.apply(cfg)  # the `shot` feature switch: hotkey and tray item, live
        controller.refresh()

    watcher = ConfigWatcher(config_path(), clock, on_config,
                            lambda e: reporter.invalid(e, startup=False))

    # The callbacks below name parts built further down; they only run once the main loop
    # does, when every part exists.
    shell: Any = None
    if wayland:
        # GNOME on Wayland: the overlay, hotkeys and selection go through Glance's shell
        # extension. Without it (not installed, or enabled only from the next login) they
        # do nothing and grammar + tray keep working; it is picked up when it appears.
        from glance.daemon.shell_link import ShellLink
        from glance.shellbus import GioShellBus
        from glance.ui.hotkeys_shell import ShellHotkeys
        from glance.ui.overlay_shell import ShellOverlay
        from glance.ui.selection_shell import ShellSelection, ShellSelectionWatch
        from glance.ui.theme import install as install_theme

        def extension_changed(available: bool) -> None:
            if available:
                hotkeys.resync()
                selection_watch.resync()
            gtk_overlay.on_extension(available)
            controller.refresh()

        shell = GioShellBus(on_change=lambda up: clock.post(lambda: extension_changed(up)))
        theme = install_theme()
        hotkeys = ShellHotkeys(shell, clock.post)
        gtk_overlay = ShellOverlay(
            shell, clock.post, clock.call_later, lambda: theme.tokens,
            on_chip_click=lambda: chip_clicked(),
            on_panel_action=lambda action, args: panels.action(action, args),
            use_key=lambda: watcher.current.ui.use_key,
            icon_toolbar=lambda on_purpose=False: icon_toolbar(on_purpose))
        theme.on_change(lambda: gtk_overlay.on_extension(True))
        ghost: Any = gtk_overlay.ghost
        selection_port: Any = ShellSelection(shell)
        selection_watch: Any = ShellSelectionWatch(shell)

        def window_changed(state: dict[str, Any]) -> None:
            gtk_overlay.on_window(state)
            selection_watch.on_window(state)

        shell_link = ShellLink({
            "ExtensionHello": lambda _p: extension_changed(True),
            "WindowChanged": window_changed,
            "SelectionSettled": selection_watch.on_settled,
            "SelectionCleared": selection_watch.on_cleared,
            "GeometryChanged": gtk_overlay.on_window,
            "Clicked": lambda p: shot_ui.on_clicked(p) or gtk_overlay.on_clicked(p),
            "Key": lambda p: hotkeys.on_key(str(p.get("name", ""))),
            "Sized": gtk_overlay.on_sized,
            "AskDone": gtk_overlay.on_ask_done,
            "ShotPicked": lambda p: shot_ui.on_picked(p),
        }, shell.connection, lambda: shell.owner)
        shell_link.start()
        # first login after install: GNOME loads the extension only from the next login
        from glance.daemon.wiring import EXTENSION_GRACE_S, ExtensionNotice
        from glance.paths import state_dir

        extension_notice = ExtensionNotice(state_dir() / "extension-notice", notifier.notify)

        def extension_check() -> bool:
            extension_notice.check(shell.available)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add_seconds(EXTENSION_GRACE_S, extension_check)
    else:
        # X11 only: these draw their own GTK windows, which on Wayland (even with DISPLAY
        # set by XWayland) would be ordinary toplevel windows popping up

        from glance.ui.ghost_gtk import GtkGhost
        from glance.ui.hotkeys_xlib import XlibHotkeys
        from glance.ui.overlay_gtk import GtkOverlay
        from glance.ui.selection_xlib import XlibSelection

        hotkeys = XlibHotkeys(clock.post)
        gtk_overlay = GtkOverlay(on_chip_click=lambda: chip_clicked(),
                                 on_panel_action=lambda action, args: panels.action(action, args),
                                 icon_toolbar=lambda on_purpose=False: icon_toolbar(on_purpose),
                                 use_key=lambda: watcher.current.ui.use_key)
        ghost = GtkGhost(watcher.current.ui.ghost_font_factor)
        selection_port = XlibSelection()
        from glance.ui.selection_watch_xlib import XlibSelectionWatch

        selection_watch = XlibSelectionWatch(clock.call_later, clock.now)
    # `overlay` holds the Escape grab while any panel is up; the Controller draws through
    # `field_overlay`, which leaves select-anything's panel alone.
    overlay = PanelEscape(gtk_overlay, hotkeys, lambda: panels.escape(),
                          on_use=lambda: controller.use_strip(),
                          use_key=lambda: watcher.current.ui.use_key)
    field_overlay = FieldPanelOverlay(
        overlay, lambda: features.selection.is_open or features.actions_bar.is_open,
        lambda: (features.selection.close(), features.actions_bar.close()))
    tray = GtkTray(on_pause=lambda: handle_control(controller, "pause", {}),
                   on_resume=lambda: handle_control(controller, "resume", {}),
                   on_quit=Gtk.main_quit, on_choose=lambda name: setup.choose(name),
                   on_setup=lambda: setup.open(),
                   on_auto_suggest=lambda on: set_suggestions(
                       update_config, config_path(), on, watcher.reload),
                   auto_suggest=watcher.current.ui.suggestions == "auto",
                   on_update=lambda: launch_update(notifier.notify, update_log_path(),
                                                    args=update_state.update_args()),
                   on_usage_open=lambda: refresh_usage(),
                   on_usage_details=lambda: show_usage_details(),
                   on_check_mode=lambda mode: set_check_mode(
                       update_config, config_path(), mode, watcher.reload),
                   check_mode=watcher.current.general.check,
                   check_hint=check_label(watcher.current.general.hotkeys.get("check", "")),
                   on_shortcuts=lambda: show_shortcuts(),
                   on_settings=lambda: show_settings(),
                   on_feature=lambda name, on: set_feature(
                       update_config, config_path(), name, on, watcher.reload),
                   features=menu_rows(watcher.current))
    if not tray.available:
        print(f"glanced: no tray icon; to get one run: {INSTALL_HINT}", file=sys.stderr)

    link = CaptureLink(clock.post)
    grammar_client = make_grammar_client(harper_bin(), dictionary_path())
    harper = SupervisedHarper(grammar_client,
                              on_change=lambda: clock.post(lambda: controller.recheck()))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glance-bg")
    # AI calls block for seconds: they get their own threads, so grammar checks never wait
    # behind them. The supervisor's lanes decide what actually runs at the same time.
    ai_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="glance-ai")

    def runner(on: ThreadPoolExecutor) -> RunBg:
        def run(work: Callable[[], Any], done: Callable[[Any], None]) -> None:
            def job() -> None:
                try:
                    result = work()
                except Exception as e:  # noqa: BLE001 - one failed job must not end the pool
                    log_event("bg_error", kind=type(e).__name__)
                    return
                clock.post(lambda: done(result))

            on.submit(job)

        return run

    run_ai = runner(ai_pool)
    issue_filter, detector = build_issue_filter(dictionary_path(), ignored_path())
    controller = Controller(
        clock, lambda: watcher.current, harper, issue_filter,
        field_overlay, link, notifier, tray, runner(pool))
    link.attach(controller)

    overrides = Overrides(overrides_path())
    keys = open_keystore()  # one keyring for both api instances, the cli and the window
    if fake_backend_wanted():
        from dataclasses import replace

        log_event("ai_test_backend")
        backends, speculative = {"fake": FakeAiBackend()}, {"fake": FakeAiBackend()}

        def current_config() -> Config:
            cfg = watcher.current
            return replace(cfg, backend=replace(cfg.backend, choice="fake", enabled=()))
    else:
        backends = real_backends(watcher.current.backend, keys)
        speculative = real_backends(watcher.current.backend, keys)  # the live lane's own

        def current_config() -> Config:
            return watcher.current

    ai, ai_supervisor = build_ai(
        controller, clock, current_config, run_ai, overlay, detector, backends,
        speculative, usage_path(), overrides, on_battery,
        lambda e: reporter.invalid(e, startup=True),
        on_status_changed=lambda _status: setup.update_tray())
    setup = AiSetup(ai_supervisor, current_config, keys, run_ai, clock.post,
                    lambda **kw: OnboardingWindow(**kw), tray, update_config, config_path)
    usage_window: list[Any] = []

    def auto_choose() -> None:
        setup.auto_choose(notifier.notify, ai.refresh_status)

    def refresh_usage() -> None:  # main loop: the tray menu is opening
        from glance.backends.usage import tray_lines

        run_ai(ai_supervisor.usage_report, lambda report: tray.set_usage(tray_lines(report)))

    def show_usage_details() -> None:  # main loop
        from glance.backends.usage import render_report
        from glance.ui.usage_gtk import UsageWindow

        def show(text: str) -> None:
            if not usage_window:
                usage_window.append(UsageWindow())
            usage_window[0].show(text)

        run_ai(lambda: render_report(ai_supervisor.usage_report()), show)

    shortcuts_window: list[Any] = []

    def show_shortcuts() -> None:  # main loop: the tray's "Keyboard shortcuts…"
        from glance.ui.shortcut_table import glance_shortcuts
        from glance.ui.shortcuts_gtk import ShortcutsWindow

        if not shortcuts_window:
            shortcuts_window.append(ShortcutsWindow())
        cfg = watcher.current
        shortcuts_window[0].show(glance_shortcuts(cfg.general.hotkeys, cfg.ui.use_key))

    refresh_usage()
    features = attach_features(
        controller=controller, ai=ai, supervisor=ai_supervisor, cfg=current_config,
        grammar=harper, issue_filter=issue_filter,
        ghost=ghost, hotkeys=hotkeys, clock=clock,
        run_ai=run_ai, language=detector.is_mixed, selection_port=selection_port,
        overlay=overlay, capture=link, notifier=notifier, selection_watch=selection_watch,
        remember=lambda key, value: remember_setting(key, value),
        selection_hint=lambda: check_label(watcher.current.general.hotkeys.get("selection", "")))
    panels = PanelRouter(controller, ai, overrides.set, features.selection, features.guard,
                         on_setup=lambda: setup.open(), bar=features.actions_bar)
    # Glance Shot: the region picker and bar of this desktop, and its controller
    from glance.daemon.shot_wiring import ShotSwitch, build_shot, shot_request
    from glance.ui.settings_model import settings_request

    if wayland:
        from glance.ui.shot_shell import ShellShot

        shot_ui: Any = ShellShot(shell, lambda: theme.tokens, lambda key: shot.action(key))
        theme.on_change(lambda: shot.refresh())
    else:
        from glance.ui.shot_gtk import GtkShot

        shot_ui = GtkShot(hotkeys, lambda key: shot.action(key))
    shot = build_shot(ui=shot_ui, controller=controller, supervisor=ai_supervisor,
                      cfg=current_config, overlay=overlay, notifier=notifier, hotkeys=hotkeys,
                      run_ai=run_ai, grammar=harper,
                      # Extract text opens in the selection actions' result panel
                      text_panel=features.actions_bar.open_text)
    ctl = CtlRouter(
        controller, panels.action,
        backend_control=BackendControl(ai_supervisor, ai, keys, current_config, overrides,
                                       run_ai, config_path()),
        edit_host=gtk_edit_host(harper, issue_filter, ai_supervisor, run_ai),
        extra={"shot": shot_request(shot), "settings_open": settings_request(
            lambda section: show_settings(section))})

    settings_window: list[Any] = []

    def show_settings(section: str | None = None) -> None:  # main loop: "Settings…"
        from glance.ui.settings_gtk import SettingsWindow
        from glance.ui.settings_model import SettingsModel

        if not settings_window:
            model = SettingsModel(config_path(), on_written=watcher.reload, overrides=overrides)
            settings_window.append(SettingsWindow(model, on_usage_details=show_usage_details))
        settings_window[0].show(section)

    def remember_setting(key: str, value: Any) -> None:  # main loop
        try:
            update_config(config_path(), key, value)
        except (ConfigError, OSError) as e:
            log_event("config_write_failed", kind=type(e).__name__)
            return
        watcher.reload()

    def toggle_panel() -> None:
        if overlay.panel_visible:
            if features.actions_bar.is_open:
                features.actions_bar.close()
            elif features.selection.is_open:
                features.selection.close()
            else:
                controller.close_panel()
        else:
            controller.open_panel()

    def icon_toolbar(on_purpose: bool = False) -> Any:
        """The status icon's toolbar now, with the check shortcut as configured
        (`on_purpose`: a right-click asked for it, not a hover)."""
        return controller.icon_toolbar(
            check_label(watcher.current.general.hotkeys.get("check", "")), typing_ok=on_purpose)

    def chip_clicked() -> None:
        """The status icon: opens the strip (or, for a muted state, the line saying why),
        closes it again."""
        controller.toggle_strip()

    actions: dict[str, Callable[[], None]] = {
        "check": controller.check_now,
        "panel": toggle_panel,
        "fix_all": controller.fix_all_safe,
        "revert": controller.revert_last,
        "pause": controller.toggle_pause,
        **tone_hotkeys(ai),
        **features.actions,
    }
    bound_chords: dict[str, str] = {}
    hotkey_problems: dict[str, str] = {}
    shot_switch = ShotSwitch(shot, actions, tray, lambda: rebind(), hotkeys)
    shot_switch.apply(watcher.current, rebind=False)

    def rebind() -> None:
        """Grab the configured chords. GNOME's own shortcuts are read with gsettings, which
        can take seconds: that happens on a worker, the grabs back on the main loop."""
        nonlocal bound_chords
        chords = bound_chords = dict(watcher.current.general.hotkeys)
        run_ai(system_shortcuts, lambda system: bind(chords, system))

    def bind(chords: dict[str, str], system: Any) -> None:
        nonlocal hotkey_problems
        if chords != bound_chords:
            return  # the config changed again meanwhile: that rebind wins
        problems = bind_hotkeys(hotkeys, chords, actions, system)
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
        """The socket closing is the first sign of a dead capture: do not wait for the
        next 500 ms poll to start its restart."""

        def on_capture_lost(self) -> None:
            clock.call_later(50, capture_sup.poll)

    controller.add_listener(_CaptureWatch())

    class _ResumeReset:
        """What a wake from suspend means beyond the controller: in-flight AI calls are
        dead, the X grabs may be gone, tokens may have expired, warm processes are stale."""

        def on_reset(self) -> None:
            ai_supervisor.cancel_all()
            rebind()

            def refresh() -> list[Any]:
                for backend in (*backends.values(), *speculative.values()):
                    if hasattr(backend, "set_warm"):
                        backend.set_warm(False)  # kills the idle processes
                ai_supervisor.sync()  # ...and the chosen claude warms a fresh one
                return list(ai_supervisor.healths(True))

            run_ai(refresh, lambda _h: (ai.refresh_status(), setup.update_tray()))

    controller.add_listener(_ResumeReset())
    session_events = SessionEvents(
        on_sleep=lambda: log_event("suspending"),
        on_resume=controller.handle_resume,
        on_lock=controller.handle_lock,
        on_monitors_changed=controller.handle_monitors_changed)
    controller.extra_status.append(lambda: {
        "capture_child": capture_sup.state,
        "harper_child": harper_sup.state,
        "grammar_engine": harper_engine,
        **hotkeys_status(hotkeys.grabbed(), bound_chords),
        "hotkeys_unavailable": ",".join(sorted(hotkey_problems)) or None,
        "hotkeys_problems": ",".join(f"{n}:{w}" for n, w in sorted(hotkey_problems.items()))
        or None,
        "tray": tray.available,
        **({"shell_extension": "running" if shell.available else "missing"}
           if shell is not None else {}),
        **reporter.status(),
    })

    def test_monitors(payload: dict[str, Any]) -> dict[str, Any]:
        """GLANCE_TEST only: pretend the monitors are `payload["monitors"]` ([x, y, w, h]
        each; none: the real ones again), through the path a real hotplug takes."""
        from glance.protocol import Rect

        rects = payload.get("monitors")
        if rects:
            fixed = [Rect(*map(int, r)) for r in rects]
            gtk_overlay.monitors = lambda: list(fixed)  # type: ignore[method-assign]
        else:
            gtk_overlay.__dict__.pop("monitors", None)
        gtk_overlay._layout()
        session_events.monitors_changed()
        return {"ok": True}

    def ctl_handle(msg_type: str, payload: dict[str, Any], send: Any) -> None:
        from glance.daemon.test_overlay import handle_test_overlay

        if msg_type == "test_monitors" and os.environ.get("GLANCE_TEST") == "1":
            send({**test_monitors(payload), **({"id": payload["id"]} if "id" in payload else {})})
            return

        shown = handle_test_overlay(msg_type, payload, overlay, controller)
        if shown is None:
            ctl.handle(msg_type, payload, send)
        else:
            send({**shown, **({"id": payload["id"]} if "id" in payload else {})})

    def on_ctl(conn: Conn, msg_type: str, payload: dict[str, Any]) -> None:  # reader thread
        clock.post(lambda: ctl_handle(msg_type, payload,
                                      lambda reply: conn.send(msg_type, reply)))

    capture_server = JsonLineServer(capture_sock(), link.on_message, link.on_disconnect)
    ctl_server = JsonLineServer(ctl_sock(), on_ctl)

    def poll_children() -> bool:
        capture_sup.poll()
        harper_sup.poll()
        return GLib.SOURCE_CONTINUE

    def quit_loop() -> bool:
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, quit_loop)

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

    def update_check() -> bool:  # main loop; the lookup runs on its own thread
        if watcher.current.general.check_updates:
            run_update_check(update_state.refresh,
                             lambda _s: tray.set_update_available(update_state.status == "available",
                                                                  update_state.result.label))
        return GLib.SOURCE_CONTINUE

    def first_update_check() -> bool:
        update_check()
        GLib.timeout_add_seconds(CHECK_EVERY_S, update_check)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add_seconds(CHECK_FIRST_S, first_update_check)
    controller.extra_status.append(lambda: {"update": update_state.status,
                                            "version": update_state.result.installed})
    rebind()
    session_events.start()
    watcher.start()
    harper_sup.start()
    capture_sup.start()
    GLib.timeout_add(POLL_CHILDREN_MS, poll_children)
    controller.refresh()  # the tray shows a state from the first moment
    setup.update_tray()
    # No AI chosen (or the chosen one gone) but one is connected: use it, and say so.
    repeat(clock, AUTO_CHOOSE_FIRST_MS, AUTO_CHOOSE_EVERY_MS, auto_choose)
    log_event("glanced_started", tray=tray.available)
    try:
        Gtk.main()
    finally:
        log_event("glanced_stopping")
        # Nothing on screen and no key grabbed first, so a slow step below can never leave
        # the desktop's Esc/Tab/Enter captured; then the children, the AI work and the
        # pools, all within teardown.DEADLINE_S (or the process ends regardless).
        teardown = Teardown()
        teardown.run([
            ("overlay", lambda: hide_everything(overlay, ghost, shell)),
            ("selection", features.actions_bar.stop),
            ("shot", shot.close),
            ("hotkeys", hotkeys.close),
            ("ai", ai_supervisor.cancel_all),
            ("pools", lambda: (pool.shutdown(wait=False, cancel_futures=True),
                               ai_pool.shutdown(wait=False, cancel_futures=True))),
            ("watcher", watcher.stop),
            ("sockets", lambda: (ctl_server.stop(), capture_server.stop())),
            ("children", lambda: stop_all((capture_sup.stop, harper_sup.stop))),
            ("shell", lambda: shell is not None and (shell_link.stop(), shell.close())),
            ("backends", lambda: stop_backends((*backends.values(), *speculative.values()))),
            ("notifier", notifier.close),
        ])
        teardown.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
