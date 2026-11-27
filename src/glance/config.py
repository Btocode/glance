"""Configuration: frozen dataclasses, TOML loader, polling hot reload."""
from __future__ import annotations

import dataclasses
import os
import re
import tempfile
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli
import tomlkit
from tomlkit.exceptions import TOMLKitError

from glance.clock import Clock, Handle
from glance.log import log_event
from glance.wordlimit import DEFAULT_MAX_WORDS, MAX_MAX_WORDS, MIN_MAX_WORDS

SUGGESTIONS_MODES = ("on_click", "auto")  # `[ui] suggestions`
# `[general] check`: "shortcut" checks only when asked (the check hotkey, a click on the
# status icon); "typing" checks as the user types.
CHECK_MODES = ("shortcut", "typing")
# `[general] correction`: "minimal" corrects mistakes only, in the writer's words; "fluent"
# may also reword a sentence a little to make it grammatical (never its meaning, names or code).
CORRECTION_MODES = ("minimal", "fluent")
# `[general] checker`: "ai" (default) — with an AI connected, the check is the AI's; the local
# checker's findings only show when no AI answers. "local": the local checker's findings show
# at once and the AI's are added (the behaviour before 0.5).
CHECKERS = ("ai", "local")

POLL_MS = 1000


class ConfigError(Exception):
    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


def _default_hotkeys() -> dict[str, str]:
    """The Linux defaults; every one was checked against the desktops' own shortcuts
    (docs/shortcuts.md, `glance.ui.shortcut_table`). macOS maps them in `hotkeys_mac`."""
    return {
        "check": "Super+Alt+g",
        "panel": "Super+Alt+space",
        "fix_all": "Super+Alt+f",
        "revert": "Super+Alt+z",
        "pause": "Super+Alt+p",
        "tone1": "Super+Alt+1",
        "tone2": "Super+Alt+2",
        "tone3": "Super+Alt+3",
        "tone4": "Super+Alt+4",
        "complete": "Super+Alt+Return",
        # Not Super+Alt+S: GNOME toggles the screen reader there (spike S5.3).
        "selection": "Super+Alt+a",
        # Glance Shot (capture a region). Free on GNOME 46, KDE and Xfce (docs/shortcuts.md).
        "shot": "Super+Alt+c",
    }


@dataclass(frozen=True)
class GeneralCfg:
    chip_idle_ms: int = 400
    analyze_idle_ms: int = 1200
    min_chars_chip: int = 15
    min_chars_analyze: int = 25
    max_field_chars: int = 20000
    autocomplete_idle_ms: int = 600  # pause before a live completion is asked for
    check_updates: bool = True  # once a day: ask github.com for the latest version
    # "shortcut" (default): nothing is checked while typing; the check hotkey (or a click on
    # the status icon) checks the field and opens the strip. "typing": check as you type.
    check: str = "shortcut"
    correction: str = "minimal"  # CORRECTION_MODES
    checker: str = "ai"  # CHECKERS
    hotkeys: dict[str, str] = field(default_factory=_default_hotkeys)


@dataclass(frozen=True)
class PrivacyCfg:
    app_denylist: tuple[str, ...] = (
        "anydesk", "remmina", "seahorse", "keepassxc", "keepass2", "bitwarden", "1password",
        "enpass", "lastpass", "virt-manager", "virtualbox", "vmware", "gnome-boxes",
        "vncviewer", "xfreerdp", "rustdesk", "teamviewer",
    )
    url_denylist: tuple[str, ...] = (
        r"(^|\.)accounts\.google\.com",
        r"login\.microsoftonline\.com",
        r"(^|\.)okta\.com",
        r"(^|\.)auth0\.com",
        r"(^|\.)paypal\.com",
        r"bank",
        r"/(login|signin|sign-in|sso|oauth|checkout|password)(/|\?|$)",
    )
    # S2.7: Chrome " (Incognito)", Brave " (Private)", Firefox " Private Browsing".
    incognito_title_patterns: tuple[str, ...] = (
        "(Incognito)", "Incognito", "Private Browsing", "Private window", "InPrivate",
        "(Private)",
    )
    browser_wm_classes: tuple[str, ...] = (
        "google-chrome", "brave-browser", "brave-browser-nightly", "firefox", "chromium",
    )
    terminal_wm_classes: tuple[str, ...] = (
        "com.mitchellh.ghostty", "ghostty", "kitty", "gnome-terminal-server", "xterm",
        "alacritty", "org.wezfurlong.wezterm", "konsole", "tilix",
        # Wayland app ids (GNOME on Wayland reports these as the window class)
        "org.gnome.terminal", "org.gnome.ptyxis", "org.gnome.console", "kgx",
    )
    terminal_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppCfg:
    capture: bool = True
    writeback: str = "auto"
    select: str = "auto"
    undo_steps: int = 1
    type_max_chars: int = 80
    type_delay_ms: int = 2
    # Mentions, emoji and links are objects here, so a whole-field rewrite is copy-only when
    # the text has any. None: Slack, Discord and browsers are rich, everything else is not.
    rich: bool | None = None


# Built-in per-app settings, keyed by casefolded window class; an `[apps.<class>]` table
# overrides them field by field. A GtkSourceView (gedit) undoes a typed replace in two steps
# and a pasted one in one (findings S4.4), so its edits are pasted: one apply, one Ctrl+Z.
APP_DEFAULTS: dict[str, AppCfg] = {
    "gedit": AppCfg(writeback="paste"),
    "org.gnome.gedit": AppCfg(writeback="paste"),
}


@dataclass(frozen=True)
class UiCfg:
    chip_px: int = 22
    chip_offset_px: int = 4
    ghost_font_factor: float = 0.81  # ghost text size as a share of the caret height (S5.2)
    # Applies the suggestion strip, grabbed only while the strip offers a fix. Never plain
    # Return: that is "send" in chat apps and a new line everywhere else.
    use_key: str = "Alt+Return"
    # When the suggestion strip shows: "on_click" (default) only after a click on the status
    # icon (or the panel hotkey); "auto" by itself whenever it has something to say.
    suggestions: str = "on_click"


# The ONE table of default models. Glance only does short, basic text work (spell/grammar
# findings, tone rewrites, a few words of completion), so every backend defaults to the
# fastest and cheapest model its tool offers. The user changes one with
# `glance backend model <backend> <model>` (or `[backend.models]`); `--default` drops the
# override. A backend missing here runs the CLI's own default (opencode: its model list is
# whatever providers the user connected, so no model is assumed).
#   claude   "haiku"          alias for the newest Haiku (verified: claude --model docs).
#   codex    "gpt-6-luna"     ~/.codex/models_cache.json: "Fast and affordable model for
#                             easier tasks" (no mini/nano tier exists in that list).
#   gemini   "flash-lite"     gemini-cli alias for the Flash-Lite tier. UNVERIFIED offline
#                             (gemini was never signed in here): a rejected name makes the
#                             backend retry once without -m (see BaseBackend).
DEFAULT_MODELS: dict[str, str] = {"claude": "haiku", "codex": "gpt-6-luna",
                                  "gemini": "flash-lite"}
# The api backend (anthropic provider) when `backend.api_model` is empty. The openai-compatible
# provider has no default: the user must set `backend.api_model`.
CLI_MODEL_BACKENDS = ("claude", "codex", "gemini", "opencode")  # the ones `models` applies to
API_DEFAULT_MODEL = "claude-haiku-5-5"


# A model name goes to a CLI argument: letters, digits and . _ : / @ + - only, never a flag.
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")


def _default_models() -> dict[str, str]:
    return dict(DEFAULT_MODELS)


@dataclass(frozen=True)
class BackendCfg:
    choice: str | None = None
    enabled: tuple[str, ...] = ()  # fallbacks, in order; nothing else is ever tried
    models: dict[str, str] = field(default_factory=_default_models)
    per_minute: int = 10
    per_day: int = 300
    speculate: bool | None = None  # None: on only for the api backend
    live_autocomplete: bool | None = None  # None: on only for the api backend
    api_provider: str = "anthropic"
    api_model: str = ""
    api_base_url: str = ""
    # Backends allowed to analyze although their class says `analyze_default = False`
    # (codex is rewrite-only by default: findings S6.3, analyze takes 8.6-13.6 s).
    analyze_with: tuple[str, ...] = ()
    # Keep a claude process started and idle, so a call skips its ~0.9 s start-up (spike
    # S6.2) for ~195 MB of resident memory. None: on only while claude is the chosen backend;
    # false: never; true: on for claude when it is chosen. Never on for any other backend.
    warm: bool | None = None

    def warm_wanted(self) -> bool:
        return self.choice == "claude" and self.warm is not False


@dataclass(frozen=True)
class AiCfg:
    # Stop new AI calls once the cost the backends reported this calendar month reaches this
    # many US dollars (local grammar keeps working). None: no limit. Only a cost a backend
    # reported counts (claude does; the others report none, so they are never stopped by it).
    monthly_cost_limit_usd: float | None = None
    # The most words of one text the AI is sent (analysis, rewrites, select-anything): a
    # longer text is analysed in a window of this many words around the caret. 50-5000.
    max_words: int = DEFAULT_MAX_WORDS


MIN_WORDS_RANGE = (1, 50)  # `[selection] min_words`


@dataclass(frozen=True)
class SelectionCfg:
    """Selection actions: the small Glance mark beside text the user selected anywhere.
    `enabled` is the floating mark only: the selection hotkey works even when it is off."""

    enabled: bool = True
    min_words: int = 2
    translate_to: str = "English"  # "Translate" in the action bar; "Translate to…" sets it
    terminals: bool = False  # show the mark in terminals too
SHOT_OCR = ("auto", "ocrs", "tesseract", "off")  # `[shot] ocr`
SHOT_AI_IMAGES = ("ask", "allow", "never")  # `[shot] ai_images`


@dataclass(frozen=True)
class ShotCfg:
    """`[shot]`: Glance Shot, capture a screen region and act on it."""

    enabled: bool = True  # the hotkey and the tray's "Take a Glance Shot"
    save_dir: str = "~/Pictures/Glance"  # where Save puts a PNG (nothing else ever does)
    # Extract text: "auto" is Vision on macOS, else ocrs (glance_core) then tesseract.
    ocr: str = "auto"
    # Explain / Ask send the image to the AI: "ask" (a one-time notice per backend),
    # "allow" (no notice), "never" (those two actions are off).
    ai_images: str = "ask"


@dataclass(frozen=True)
class Config:
    general: GeneralCfg = field(default_factory=GeneralCfg)
    privacy: PrivacyCfg = field(default_factory=PrivacyCfg)
    apps: dict[str, AppCfg] = field(default_factory=dict)
    ui: UiCfg = field(default_factory=UiCfg)
    backend: BackendCfg = field(default_factory=BackendCfg)
    ai: AiCfg = field(default_factory=AiCfg)
    selection: SelectionCfg = field(default_factory=SelectionCfg)
    shot: ShotCfg = field(default_factory=ShotCfg)
    raw: dict[str, Any] = field(default_factory=dict)

    def app(self, wm_class: str) -> AppCfg:
        key = wm_class.casefold()
        return self.apps.get(key) or APP_DEFAULTS.get(key, AppCfg())


_TABLE_DEFAULTS: dict[str, Callable[[], dict[str, str]]] = {
    "hotkeys": _default_hotkeys,
    "models": _default_models,
}


def _check(path: str, value: Any, tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):  # `X | None`: TOML has no null, so check X
        tp = next(a for a in typing.get_args(tp) if a is not type(None))
        origin = typing.get_origin(tp)
    if origin is tuple:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(path, "expected a list of strings")
        return tuple(value)
    if origin is dict:
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ConfigError(path, "expected a table of strings")
        return dict(value)
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(path, "expected true or false")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(path, "expected an integer")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(path, "expected a number")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(path, "expected a string")
        return value
    raise ConfigError(path, "unsupported type")  # pragma: no cover


def _build(cls: type, table: Any, prefix: str, base: Any = None) -> Any:
    if not isinstance(table, dict):
        raise ConfigError(prefix, "expected a table")
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in table:
            continue
        value = _check(f"{prefix}.{f.name}", table[f.name], hints[f.name])
        if f.name in _TABLE_DEFAULTS:
            value = {**_TABLE_DEFAULTS[f.name](), **value}
        kwargs[f.name] = value
    return cls(**kwargs) if base is None else dataclasses.replace(base, **kwargs)


def _parse(data: dict[str, Any]) -> Config:
    apps_raw = data.get("apps", {})
    if not isinstance(apps_raw, dict):
        raise ConfigError("apps", "expected a table")
    apps = {k.casefold(): _build(AppCfg, v, f"apps.{k}", APP_DEFAULTS.get(k.casefold()))
            for k, v in apps_raw.items()}
    known = {"general", "privacy", "apps", "ui", "ai", "selection", "shot"}  # `raw` keeps [backend] too (Plan 1 contract)
    backend = _build(BackendCfg, data.get("backend", {}), "backend")
    for model_name, model in backend.models.items():
        if not MODEL_NAME.fullmatch(model):
            raise ConfigError(f"backend.models.{model_name}", "not a model name")
    for name in ("per_minute", "per_day"):
        if getattr(backend, name) < 0:
            raise ConfigError(f"backend.{name}", "expected zero or more")
    ai_raw = data.get("ai", {})
    if isinstance(ai_raw, dict) and "max_words" in ai_raw:
        n = ai_raw["max_words"]
        if isinstance(n, bool) or not isinstance(n, int) or not \
                MIN_MAX_WORDS <= n <= MAX_MAX_WORDS:
            # Out of range or not a number: the default, and a line naming the key only.
            log_event("config_value_ignored", key="ai.max_words")
            ai_raw = {k: v for k, v in ai_raw.items() if k != "max_words"}
    ai = _build(AiCfg, ai_raw, "ai")
    if ai.monthly_cost_limit_usd is not None and ai.monthly_cost_limit_usd < 0:
        raise ConfigError("ai.monthly_cost_limit_usd", "expected zero or more")
    ui = _build(UiCfg, data.get("ui", {}), "ui")
    if ui.suggestions not in SUGGESTIONS_MODES:
        # A typo here must not stop Glance: the default, and a line saying which key (the
        # value itself is not logged).
        log_event("config_value_ignored", key="ui.suggestions")
        ui = dataclasses.replace(ui, suggestions=UiCfg.suggestions)
    general = _build(GeneralCfg, data.get("general", {}), "general")
    if general.check not in CHECK_MODES:
        log_event("config_value_ignored", key="general.check")
        general = dataclasses.replace(general, check=GeneralCfg.check)
    if general.checker not in CHECKERS:
        log_event("config_value_ignored", key="general.checker")
        general = dataclasses.replace(general, checker=GeneralCfg.checker)
    if general.correction not in CORRECTION_MODES:
        log_event("config_value_ignored", key="general.correction")
        general = dataclasses.replace(general, correction=GeneralCfg.correction)
    selection = _build(SelectionCfg, data.get("selection", {}), "selection")
    lo, hi = MIN_WORDS_RANGE
    if not lo <= selection.min_words <= hi:
        log_event("config_value_ignored", key="selection.min_words")
        selection = dataclasses.replace(selection, min_words=SelectionCfg.min_words)
    if not selection.translate_to.strip() or len(selection.translate_to) > 40:
        log_event("config_value_ignored", key="selection.translate_to")
        selection = dataclasses.replace(selection, translate_to=SelectionCfg.translate_to)
    shot = _build(ShotCfg, data.get("shot", {}), "shot")
    for key, allowed in (("ocr", SHOT_OCR), ("ai_images", SHOT_AI_IMAGES)):
        if getattr(shot, key) not in allowed:
            log_event("config_value_ignored", key=f"shot.{key}")
            shot = dataclasses.replace(shot, **{key: getattr(ShotCfg, key)})
    return Config(
        general=general,
        privacy=_build(PrivacyCfg, data.get("privacy", {}), "privacy"),
        apps=apps,
        ui=ui,
        backend=backend,
        ai=ai,
        selection=selection,
        shot=shot,
        raw={k: v for k, v in data.items() if k not in known},
    )


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(str(path), f"unreadable: {e.__class__.__name__}") from e
    try:
        data = tomli.loads(text)
    except tomli.TOMLDecodeError as e:
        raise ConfigError(str(path), f"invalid TOML: {e}") from e
    return _parse(data)


def validate_text(text: str) -> Config:
    """The config `text` would load as, with load_config's checks; ConfigError otherwise."""
    try:
        data = tomli.loads(text)
    except tomli.TOMLDecodeError as e:
        raise ConfigError("config.toml", f"invalid TOML: {e}") from e
    return _parse(data)


def update_config(path: Path, dotted_key: str | tuple[str, ...], value: Any,
                  check: Callable[[str], Any] | None = None) -> None:
    """Set one key (e.g. `backend.choice`) keeping the user's comments and layout.

    `None` removes the key. The file and its directory are created when missing; the write is
    an atomic replace. A file that is not valid TOML is left untouched (ConfigError).
    `dotted_key` may be the parts themselves (a window class such as `org.gnome.gedit` has
    dots of its own). `check(new_text)` runs before anything is written: when it raises
    (e.g. `validate_text`'s ConfigError) the file stays as it was.
    """
    target = path.resolve() if path.is_symlink() else path  # keep a symlinked dotfile a symlink
    try:
        doc = tomlkit.parse(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        doc = tomlkit.document()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(str(path), f"unreadable: {e.__class__.__name__}") from e
    except TOMLKitError as e:
        raise ConfigError(str(path), f"invalid TOML: {e}") from e
    parts = tuple(dotted_key.split(".")) if isinstance(dotted_key, str) else tuple(dotted_key)
    *parents, leaf = parts
    table: Any = doc
    for i, part in enumerate(parents):
        if part not in table:
            if value is None:
                return
            table[part] = tomlkit.table()
        table = table[part]
        if not isinstance(table, dict):
            raise ConfigError(".".join(parents[: i + 1]), "expected a table")
    if value is None:
        if leaf not in table:
            return
        del table[leaf]
    else:
        table[leaf] = list(value) if isinstance(value, tuple) else value
    text = tomlkit.dumps(doc)
    if check is not None:
        check(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if target.exists():
            os.chmod(tmp, target.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConfigWatcher:
    """Polls the file's mtime; keeps the last good config when a reload fails."""

    def __init__(
        self,
        path: Path,
        clock: Clock,
        on_change: Callable[[Config], None],
        on_error: Callable[[ConfigError], None],
    ) -> None:
        self._path = path
        self._clock = clock
        self._on_change = on_change
        self._on_error = on_error
        self._stamp: float | None = None
        self._timer: Handle | None = None
        try:
            self.current: Config = load_config(path)
        except ConfigError:
            self.current = Config()
        self._stamp = self._mtime()

    def _mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def start(self) -> None:
        self._stamp = self._mtime()
        self._schedule()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule(self) -> None:
        self._timer = self._clock.call_later(POLL_MS, self._poll)

    def reload(self) -> None:
        """Check the file now (after Glance itself wrote it), not at the next poll."""
        if self._timer is not None:
            self._timer.cancel()
        self._poll()

    def _poll(self) -> None:
        try:
            stamp = self._mtime()
            if stamp != self._stamp:
                self._stamp = stamp  # remember even on failure: report once per save
                try:
                    cfg = load_config(self._path)
                except ConfigError as e:
                    self._on_error(e)
                else:
                    self.current = cfg
                    self._on_change(cfg)
        finally:
            self._schedule()
