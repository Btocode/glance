"""Small pieces of glue between glanced's parts. No GTK; each is tested with fakes."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from glance.log import log_event

SESSION_EXIT = 78  # packaging/glance.service: RestartPreventExitStatus=78
NEEDS_X11 = ("glance: needs an X11 session or GNOME on Wayland; not starting "
             "(KDE, Sway and other Wayland desktops are not supported yet)")

RunBg = Callable[[Callable[[], Any], Callable[[Any], None]], None]


def thread_runner(post: Callable[[Callable[[], None]], None], name: str) -> RunBg:
    """`run_bg` with a thread per job: for short blocking reads that must not queue behind
    the AI pool's seconds-long calls. `done` runs on the main loop through `post`; a job
    that raises hands `done` None."""
    import threading

    def run(work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        def job() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001 - the caller must always hear back
                log_event("bg_error", kind=type(e).__name__)
                result = None
            post(lambda: done(result))

        threading.Thread(target=job, name=name, daemon=True).start()

    return run


EXTENSION_NOTICE = ("Log out and back in once to finish setting up Glance",
                    ("Grammar checking and the tray icon already work. The suggestions next to "
                    "what you type come from Glance's GNOME Shell extension, which GNOME "
                    "starts at login."))
EXTENSION_GRACE_S = 15  # the extension says hello within a second or two when it runs
EXTENSION_NOTICE_EVERY_S = 24 * 3600


class ExtensionNotice:
    """GNOME on Wayland without the shell extension yet (enabled from the next login): one
    desktop notification, at most once per run and once a day across restarts. Never a
    window: glanced draws nothing of its own on Wayland."""

    def __init__(self, stamp: Path, notify: Callable[[str, str], None],
                 now: Callable[[], float] | None = None) -> None:
        import time

        self._stamp, self._notify = stamp, notify
        self._now = now or time.time
        self._done = False

    def check(self, available: bool) -> bool:
        """Called once the grace period is over; True when the notification was sent."""
        if available or self._done:
            return False
        self._done = True
        now = self._now()
        try:
            last = float(self._stamp.read_text().strip())
        except (OSError, ValueError):
            last = None
        if last is not None and 0 <= now - last < EXTENSION_NOTICE_EVERY_S:
            return False
        try:
            self._stamp.parent.mkdir(parents=True, exist_ok=True)
            self._stamp.write_text(f"{now:.0f}\n")
        except OSError:
            pass
        log_event("extension_notice")
        self._notify(*EXTENSION_NOTICE)
        return True


def session_problem(env: Mapping[str, str]) -> str | None:
    """Why glanced must not start in this session (a Wayland desktop other than GNOME, no
    display); None when it can."""
    from glance.session import session_kind

    if session_kind(env) is None:
        return NEEDS_X11
    return None


def tone_hotkeys(ai: Any) -> dict[str, Callable[[], None]]:
    """`tone1`..`tone4`: apply the Nth tone chip of the focused field's audience."""
    return {f"tone{n}": (lambda n=n: ai.apply_chip(n)) for n in range(1, 5)}


def hotkeys_status(grabbed: Iterable[str], configured: Iterable[str]) -> dict[str, str]:
    """`hotkeys_grabbed` for `glance status`: the configured hotkey names glanced holds now
    (never the transient Tab / Escape grabs), comma separated, at most 200 characters."""
    wanted = set(configured)
    return {"hotkeys_grabbed": ",".join(sorted(n for n in grabbed if n in wanted))[:200]}


def configure_api(pools: Iterable[Mapping[str, Any]], backend_cfg: Any) -> bool:
    """Hand the current `backend.api_*` settings to every live api instance (main and
    speculative); True when they changed, so the caller re-checks health. A bad provider is
    logged and the old settings stay."""
    changed = False
    for pool in pools:
        api = pool.get("api")
        if api is None or not hasattr(api, "configure"):
            continue
        try:
            changed |= api.configure(backend_cfg.api_provider, backend_cfg.api_model,
                                     backend_cfg.api_base_url)
        except ValueError:
            log_event("api_config_invalid")
    return changed


def set_suggestions(update_config: Callable[[Path, str, Any], None], path: Path,
                    auto: bool, reload: Callable[[], None]) -> None:
    """The tray's "Show suggestions automatically": write `[ui] suggestions` (keeping the
    file's own layout) and load it now, so it applies without waiting for the next poll.
    A config that cannot be written is logged; the setting stays as it was."""
    from glance.config import ConfigError

    try:
        update_config(path, "ui.suggestions", "auto" if auto else "on_click")
    except (ConfigError, OSError) as e:
        log_event("config_write_failed", kind=type(e).__name__)
        return
    reload()


def guard_panel(guard: Any) -> Callable[[Any, Any], Any]:
    """The `extra_panel` transform that puts the send-guard's warning on the panel."""
    return lambda _s, panel: replace(panel, guard=guard.label())


class AiSetup:
    """The "Connect an AI" window and the tray's backend menu. Main loop only.

    The window runs `rows`, `use`, `test`, `set_key` and `api_settings_changed` on its own
    worker threads; whatever they change in the daemon reaches it through `post`. The rows
    for the tray are built with `run_bg` (health checks can take seconds).
    """

    def __init__(self, supervisor: Any, cfg: Callable[[], Any], keystore: Any, run_bg: RunBg,
                 post: Callable[[Callable[[], None]], None],
                 make_window: Callable[..., Any], tray: Any,
                 update_config: Callable[[Path, str, Any], None],
                 config_path: Callable[[], Path]) -> None:
        self._sup, self._cfg, self._keys = supervisor, cfg, keystore
        self._run_bg, self._post = run_bg, post
        self._make_window, self._tray = make_window, tray
        self._update_config, self._config_path = update_config, config_path
        self._window: Any = None

    # main loop

    def open(self) -> None:
        """Only ever on the user's click (tray "Connect an AI…" / "Set up…"): glanced never
        opens a window by itself at start-up, "No AI connected" is the tray icon's state."""
        if self._window is None:
            self._window = self._make_window(
                rows_provider=lambda: self.rows(refresh=True), on_use=self.use, on_test=self.test,
                on_set_key=self.set_key, on_api_settings=self.api_settings_changed,
                api_settings=self.api_settings)
        self._window.show()

    def update_tray(self) -> None:
        """Rows from the cached health (fresh after a key was saved or a Test ran)."""
        self._run_bg(self.rows, self._tray.set_backends)

    def choose(self, name: str) -> None:
        """The tray's radio item: use `name` (the config watcher then refreshes the status)."""
        def work() -> bool:
            try:
                self.use(name)
            except ValueError:
                return False
            return True

        self._run_bg(work, lambda _ok: self.update_tray())

    # worker threads

    def rows(self, refresh: bool = False) -> tuple[Any, ...]:
        from glance.ui.onboarding_model import build_rows

        return build_rows(self._sup.healths(refresh), self._cfg().backend.choice)

    def use(self, name: str) -> None:
        self._sup.use(name)  # writes backend.choice; the watcher reloads on the main loop
        self._post(self.update_tray)

    def test(self, name: str, done: Callable[[bool, str], None]) -> None:
        done(*self._sup.test(name))
        self._sup.healths(True)
        self._post(self.update_tray)

    def set_key(self, provider: str, key: str) -> None:
        self._keys.set(provider, key)
        self._sup.healths(True)
        self._post(self.update_tray)

    def api_settings(self) -> tuple[str, str, str]:
        b = self._cfg().backend
        return (b.api_provider, b.api_model or "", b.api_base_url or "")

    def api_settings_changed(self, provider: str, model: str, base_url: str) -> None:
        path = self._config_path()
        self._update_config(path, "backend.api_provider", provider)
        self._update_config(path, "backend.api_model", model.strip() or None)
        self._update_config(path, "backend.api_base_url", base_url.strip() or None)
