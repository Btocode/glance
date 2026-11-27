"""The Settings window's model: its sections, fields, current values, validation and writes.
Pure (no GTK, no AppKit); the GTK window (`settings_gtk`) and the macOS one
(`platform.macos.settings`) only draw what this says and hand changes back.

Every write goes through `config.update_config` (tomlkit: the user's comments and layout stay)
with `config.validate_text` as its check, so a value load_config would refuse never reaches
the file; then `on_written()` lets a running glanced apply it now (`ConfigWatcher.reload`; a
glanced in another process sees the file within a second anyway). A refused value comes back
as one line of text for the field, and the file is left as it was.

Nothing here talks to glanced: what needs it (backends, usage, the live hotkey grabs, the API
key) goes through `SettingsServices`, which the windows call on a worker thread.
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli

from glance.config import (
    MODEL_NAME,
    SHOT_AI_IMAGES,
    SHOT_OCR,
    Config,
    ConfigError,
    PrivacyCfg,
    _default_hotkeys,
    load_config,
    update_config,
    validate_text,
)
from glance.feature_switches import FEATURES, Feature
from glance.ui.hotkeys import parse_chord
from glance.ui.shortcut_table import GLANCE_ACTIONS, LINUX_RESERVED, MAC_RESERVED
from glance.wordlimit import MAX_MAX_WORDS, MIN_MAX_WORDS

Key = tuple[str, ...]

SECTIONS: tuple[tuple[str, str], ...] = (
    ("general", "General"), ("features", "Features"), ("ai", "AI"),
    ("shortcuts", "Shortcuts"), ("apps", "Apps"), ("privacy", "Privacy"), ("about", "About"),
)
TITLE = "Glance Settings"
GITHUB_URL = "https://github.com/Btocode/glance"
ISSUES_URL = GITHUB_URL + "/issues"


def settings_request(open_settings: Callable[[str | None], None]
                     ) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """glanced's `settings_open {section?}` (what `glance settings` sends): open the window."""
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        section = payload.get("section")
        if section is not None and section not in dict(SECTIONS):
            return {"ok": False, "error": "unknown section"}
        open_settings(section)
        return {"ok": True}

    return handle


def glance_version() -> str:
    try:
        from importlib.metadata import version

        return version("glance")
    except Exception:  # noqa: BLE001 - a checkout run without the package metadata
        return "dev"


# ---------------------------------------------------------------------- fields


@dataclass(frozen=True)
class Field:
    """One setting. `kind`: switch | choice | int | money | text | folder | model.
    The UI hands `parse` what its control holds (a bool, a choice's value, or text)."""

    key: Key
    label: str
    kind: str
    help: str = ""
    choices: tuple[tuple[Any, str], ...] = ()
    lo: int = 0
    hi: int = 0
    max_chars: int = 200

    @property
    def id(self) -> str:
        return ".".join(self.key)


GENERAL: tuple[Field, ...] = (
    Field(("general", "check"), "Check my writing", "choice",
          "On the shortcut checks only when you ask; while typing checks as you go.",
          choices=(("shortcut", "On shortcut"), ("typing", "While typing"))),
    Field(("general", "checker"), "Checked by", "choice",
          "AI checks your text with a fast model; the local checker is used only when no AI "
          "answers. Local shows the quick local fixes at once and adds the AI's.",
          choices=(("ai", "AI (fast model)"), ("local", "Local + AI"))),
    Field(("general", "correction"), "Correction strength", "choice",
          "Minimal fixes mistakes in your own words; fluent may also rephrase a little "
          "to fix awkward grammar.",
          choices=(("minimal", "Minimal"), ("fluent", "Fluent"))),
    Field(("ui", "suggestions"), "Show suggestions", "choice",
          "On click: when you click the Glance icon beside the field.",
          choices=(("on_click", "On click"), ("auto", "Automatically"))),
    Field(("ai", "max_words"), "AI word limit", "int",
          f"The most words of one text the AI is sent ({MIN_MAX_WORDS}–{MAX_MAX_WORDS}). "
          "A longer text is checked around the caret.", lo=MIN_MAX_WORDS, hi=MAX_MAX_WORDS),
    Field(("general", "check_updates"), "Check for updates", "switch",
          "Once a day Glance asks github.com for the latest version; nothing else is sent."),
)

# The settings under a feature's switch (shown while it is on).
FEATURE_FIELDS: dict[str, tuple[Field, ...]] = {
    "selection": (
        Field(("selection", "translate_to"), "Translate to", "text",
              "The language the Translate action writes.", max_chars=40),
        Field(("selection", "min_words"), "Smallest selection", "int",
              "Words a selection needs before the Glance mark shows (1–50).", lo=1, hi=50),
        Field(("selection", "terminals"), "In terminals too", "switch",
              "Show the mark beside text selected in a terminal."),
    ),
    "shot": (
        Field(("shot", "save_dir"), "Save shots to", "folder",
              "Where Save puts a PNG. Nothing else is ever written."),
        Field(("shot", "ocr"), "Read text with", "choice",
              "Extract text runs on this computer.", choices=tuple(
            (v, {"auto": "Automatic", "ocrs": "ocrs", "tesseract": "Tesseract",
                 "off": "Off"}[v]) for v in SHOT_OCR)),
        Field(("shot", "ai_images"), "Send images to the AI", "choice",
              "Explain and Ask send the picture itself to the AI you connected.",
              choices=tuple((v, {"ask": "Ask first", "allow": "Allow",
                                 "never": "Never"}[v]) for v in SHOT_AI_IMAGES)),
    ),
}

COST_LIMIT = Field(("ai", "monthly_cost_limit_usd"), "Monthly cost limit (US$)", "money",
                   "AI calls stop for the month once the cost the AI reported reaches this. "
                   "Empty: no limit. Local grammar keeps working.")
API_PROVIDER = Field(("backend", "api_provider"), "API provider", "choice",
                     choices=(("anthropic", "Anthropic"), ("openai", "OpenAI-compatible")))
API_MODEL = Field(("backend", "api_model"), "API model", "model",
                  "Empty: Glance's default for the provider.")
MODEL_BACKENDS = ("claude", "codex", "gemini", "opencode")


def model_field(backend: str) -> Field:
    return Field(("backend", "models", backend), "Model", "model",
                 "Empty: Glance's default (the fastest, cheapest).")


def parse(f: Field, raw: Any) -> tuple[Any, str | None]:
    """(value to write, None) or (None, the reason it is refused). `None` as the value
    means "remove the key" (back to the default)."""
    if f.kind == "switch":
        return bool(raw), None
    if f.kind == "choice":
        allowed = [v for v, _ in f.choices]
        return (raw, None) if raw in allowed else (None, "Pick one of the choices")
    text = str(raw if raw is not None else "").strip()
    if f.kind == "int":
        if not re.fullmatch(r"[0-9]+", text):
            return None, "Enter a whole number"
        n = int(text)
        if not f.lo <= n <= f.hi:
            return None, f"Enter a number from {f.lo} to {f.hi}"
        return n, None
    if f.kind == "money":
        if not text:
            return None, None
        try:
            v = float(text.lstrip("$"))
        except ValueError:
            return None, "Enter an amount such as 5 or 12.50, or leave it empty"
        if not 0 <= v <= 1_000_000:  # (NaN fails this too)
            return None, "Enter zero or more"
        return v, None
    if f.kind == "model":
        if not text:
            return None, None
        if not MODEL_NAME.fullmatch(text):
            return None, "Letters, digits and . _ : / @ + - only"
        return text, None
    if f.kind in ("text", "folder"):
        if not text:
            return None, "This cannot be empty"
        if len(text) > f.max_chars:
            return None, f"At most {f.max_chars} characters"
        return text, None
    return None, "Unsupported setting"  # pragma: no cover


def display(f: Field, value: Any) -> Any:
    """What the control shows for `value`: a bool, a choice's value, or text."""
    if f.kind in ("switch", "choice"):
        return value
    if value is None:
        return ""
    if f.kind == "money":
        return f"{value:g}"
    return str(value)


# ---------------------------------------------------------------------- shortcuts

HOTKEY_LABELS: dict[str, str] = {
    "use": "Apply the suggestion",
    "tone1": "Tone 1", "tone2": "Tone 2", "tone3": "Tone 3", "tone4": "Tone 4",
    **{name: what for name, what in GLANCE_ACTIONS if name not in ("tones", "use")},
}
LIVE_PROBLEMS = {"conflict": "Another program already uses it",
                 "system": "It is one of the desktop's own shortcuts",
                 "invalid": "Not a valid shortcut",
                 "overlay": "On GNOME, Super plus one key also opens the Activities overview: "
                            "add Alt (Super+Alt+…)"}
_MODS_ORDER = ("Ctrl", "Super", "Shift", "Alt")
_MOD_KEYS = {"shift_l", "shift_r", "control_l", "control_r", "alt_l", "alt_r", "super_l",
             "super_r", "meta_l", "meta_r", "hyper_l", "hyper_r", "iso_level3_shift",
             "caps_lock", "num_lock"}


@dataclass(frozen=True)
class ShortcutRow:
    name: str          # `[general.hotkeys] <name>`, or "use" (`[ui] use_key`)
    label: str
    chord: str         # as configured
    default: str
    problem: str | None  # why it may not work, or None

    @property
    def key(self) -> Key:
        return ("ui", "use_key") if self.name == "use" else ("general", "hotkeys", self.name)

    @property
    def is_default(self) -> bool:
        return _norm(self.chord) == _norm(self.default)


def _norm(chord: str) -> tuple[frozenset[str], str] | None:
    try:
        mods, key = parse_chord(chord)
    except ValueError:
        return None
    return mods, key.casefold()


def chord_from_keys(mods: Sequence[str], keyname: str) -> str | None:
    """A chord in Glance's spelling from a key press: `mods` among ctrl/shift/alt/super,
    `keyname` the keysym name (GTK's `Gdk.keyval_name`). None for a lone modifier key."""
    if not keyname or keyname.casefold() in _MOD_KEYS:
        return None
    want = {m.casefold() for m in mods}
    names = [m for m in _MODS_ORDER if m.casefold() in want]
    key = keyname.lower() if len(keyname) == 1 else keyname
    return "+".join([*names, key])


def chord_problem(name: str, chord: str, chords: Mapping[str, str], mac: bool = False,
                  live: Mapping[str, str] | None = None, live_chord: str | None = None
                  ) -> str | None:
    """Why `chord` for Glance's `name` will not work, or None. `chords`: all of Glance's,
    name -> chord (the use key under "use"); `live`: glanced's grab result for the chords
    it holds now (`hotkeys_problems`), which counts only while `chord` is `live_chord`."""
    n = _norm(chord)
    if n is None:
        return "Not a valid shortcut"
    if not n[0]:
        return "Add Ctrl, Alt or Super: a lone key would be taken from your typing"
    for other, theirs in chords.items():
        if other != name and _norm(theirs) == n:
            return f"Glance already uses it for “{HOTKEY_LABELS.get(other, other)}”"
    for reserved, owner in (MAC_RESERVED if mac else LINUX_RESERVED):
        if _norm(reserved) == n:
            return f"Used by {owner}"
    if live and name in live and (live_chord is None or _norm(live_chord) == n):
        return LIVE_PROBLEMS.get(live[name], "Not available")
    return None


def parse_live_problems(value: Any) -> dict[str, str]:
    """`hotkeys_problems` from `glance status` ("check:system,shot:conflict")."""
    out: dict[str, str] = {}
    for part in str(value or "").split(","):
        name, _, why = part.partition(":")
        if name and why:
            out[name] = why
    return out


# ---------------------------------------------------------------------- lists


@dataclass(frozen=True)
class ListItem:
    value: str
    builtin: bool  # a built-in protection: shown, never removable here


def _builtin(name: str) -> tuple[str, ...]:
    return tuple(getattr(PrivacyCfg(), name))


@dataclass(frozen=True)
class AppRow:
    wm_class: str
    capture: bool       # `[apps.<class>] capture`
    profile: str | None  # the remembered audience profile, None: automatic


# ---------------------------------------------------------------------- the model


class SettingsModel:
    """Reads the config file fresh on `refresh()`; writes one key at a time."""

    def __init__(self, path: Path, on_written: Callable[[], None] = lambda: None,
                 mac: bool | None = None, overrides: Any = None) -> None:
        self.path = path
        self._on_written = on_written
        self.mac = sys.platform == "darwin" if mac is None else mac
        self._overrides = overrides  # audience.Overrides, or None (no per-app profile)
        self.cfg: Config = Config()
        self.load_error: str | None = None
        self._raw: dict[str, Any] = {}
        self.refresh()

    # -- reading ---------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.cfg = load_config(self.path)
            self.load_error = None
        except ConfigError as e:
            self.cfg = Config()
            self.load_error = f"config.toml cannot be read ({e}); fix it by hand first."
        try:
            self._raw = tomli.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._raw = {}

    def is_set(self, key: Key) -> bool:
        """True when the file itself sets `key` (not just the default)."""
        node: Any = self._raw
        for part in key:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    def value(self, key: Key) -> Any:
        """The value in effect (the file's, else the default)."""
        if key[0] == "apps" and len(key) == 3:
            return getattr(self.cfg.app(key[1]), key[2])
        node: Any = self.cfg
        for part in key:
            node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
        return node

    # -- writing ---------------------------------------------------------------------
    def write(self, key: Key, value: Any) -> str | None:
        """Write `value` (None removes the key) after load_config's own checks. Returns
        None, or the one line saying why nothing was written."""
        try:
            update_config(self.path, key, value, check=validate_text)
        except ConfigError as e:
            return _reason(e)
        except OSError as e:
            return f"Could not write config.toml ({e.__class__.__name__})"
        self.refresh()
        self._on_written()
        return None

    def set(self, f: Field, raw: Any) -> str | None:
        """The UI's value for field `f`: checked, then written. None, or the error line."""
        value, error = parse(f, raw)
        if error is not None:
            return error
        if value is None and f.kind in ("text", "folder"):
            return "This cannot be empty"  # pragma: no cover - parse already says so
        if value == self.value(f.key) and (value is not None or not self.is_set(f.key)):
            return None  # nothing changed: the file is not touched
        return self.write(f.key, value)

    def reset(self, key: Key) -> str | None:
        return self.write(key, None)

    # -- features --------------------------------------------------------------------
    def features(self) -> tuple[tuple[Feature, bool, tuple[Field, ...]], ...]:
        """(feature, on, its settings) for each switch; the ones the tray shows elsewhere
        (`menu=False`: check while typing is General's "Check my writing") are left out."""
        return tuple((f, f.is_on(self.cfg), FEATURE_FIELDS.get(f.name, ()))
                     for f in FEATURES if f.menu)

    def set_feature(self, name: str, on: bool) -> str | None:
        f = next(x for x in FEATURES if x.name == name)
        return self.write(tuple(f.key.split(".")), f.on if on else f.off)

    # -- shortcuts -------------------------------------------------------------------
    def chords(self) -> dict[str, str]:
        """Every chord as it is bound on this platform, the use key under "use"."""
        hk = dict(self.cfg.general.hotkeys)
        if self.mac:
            from glance.platform.macos.hotkeys_mac import mac_hotkeys

            hk = mac_hotkeys(hk)
        return {"use": self.cfg.ui.use_key, **hk}

    def defaults(self) -> dict[str, str]:
        hk = _default_hotkeys()
        if self.mac:
            from glance.platform.macos.hotkeys_mac import mac_hotkeys

            hk = mac_hotkeys(hk)
        from glance.config import UiCfg

        return {"use": UiCfg.use_key, **hk}

    def shortcuts(self, live: Mapping[str, str] | None = None) -> tuple[ShortcutRow, ...]:
        chords, defaults = self.chords(), self.defaults()
        order = ["use", *(n for n in _default_hotkeys())]
        order += [n for n in chords if n not in order]
        return tuple(ShortcutRow(n, HOTKEY_LABELS.get(n, n), chords[n], defaults.get(n, ""),
                                 chord_problem(n, chords[n], chords, self.mac, live,
                                               chords[n]))
                     for n in order if n in chords)

    def set_shortcut(self, name: str, chord: str) -> str | None:
        """Bind `chord` to `name` unless something already owns it."""
        chords = self.chords()
        problem = chord_problem(name, chord, chords, self.mac)
        if problem is not None:
            return problem
        row = ShortcutRow(name, "", chord, self.defaults().get(name, ""), None)
        if row.is_default:
            return self.write(row.key, None)  # the default: drop the line
        return self.write(row.key, chord)

    def reset_shortcut(self, name: str) -> str | None:
        return self.write(ShortcutRow(name, "", "", "", None).key, None)

    # -- lists (privacy) ------------------------------------------------------------
    def list_items(self, name: str) -> tuple[ListItem, ...]:
        """`[privacy] <name>`: built-in entries first (locked), then the user's."""
        current = tuple(getattr(self.cfg.privacy, name))
        builtin = _builtin(name)
        return tuple(ListItem(v, v in builtin) for v in current)

    def add_to_list(self, name: str, entry: str) -> str | None:
        entry = entry.strip()
        if not entry:
            return "Type a name first"
        if len(entry) > 200:
            return "At most 200 characters"
        if name == "url_denylist":
            try:
                re.compile(entry)
            except re.error:
                return "Not a valid pattern (a regular expression)"
        current = list(getattr(self.cfg.privacy, name))
        if entry.casefold() in (v.casefold() for v in current):
            return "Already in the list"
        if not self.is_set(("privacy", name)):
            current = list(_builtin(name)) + [v for v in current if v not in _builtin(name)]
        return self.write(("privacy", name), current + [entry])

    def remove_from_list(self, name: str, entry: str) -> str | None:
        """Remove a user's entry; a built-in protection is never removed here."""
        if entry in _builtin(name) and name != "terminal_allowlist":
            return "A built-in protection cannot be removed"
        current = [v for v in getattr(self.cfg.privacy, name) if v != entry]
        return self.write(("privacy", name), current)

    # -- apps ------------------------------------------------------------------------
    def apps(self) -> tuple[AppRow, ...]:
        """The apps with settings of their own (`[apps.<class>]`) or a remembered profile."""
        names: dict[str, None] = {}
        raw_apps = self._raw.get("apps")
        if isinstance(raw_apps, dict):
            names.update((k.casefold(), None) for k in raw_apps)
        profiles = self._profile_map()
        names.update((k, None) for k in profiles if _app_key(k))
        return tuple(AppRow(n, self.cfg.app(n).capture, profiles.get(n))
                     for n in sorted(names))

    def add_app(self, wm_class: str) -> str | None:
        wm = wm_class.strip().casefold()
        if not wm or not _app_key(wm):
            return "Type the app's window class, e.g. slack or org.gnome.texteditor"
        if any(a.wm_class == wm for a in self.apps()):
            return "Already in the list"
        return self.write(("apps", wm, "capture"), True)

    def set_app_capture(self, wm_class: str, on: bool) -> str | None:
        return self.write(("apps", wm_class, "capture"), on)

    def remove_app(self, wm_class: str) -> str | None:
        """Forget the app's own settings (capture and profile): back to the defaults."""
        if self._overrides is not None:
            self._overrides.clear(wm_class)
        if not self.is_set(("apps", wm_class)):
            return None
        return self.write(("apps", wm_class), None)

    def profiles(self) -> tuple[tuple[str, str], ...]:
        """(name, label) of every audience profile, built-ins first."""
        from glance.audience.profiles import BUILTIN, load_profiles

        try:
            found = load_profiles(self.cfg.raw)
        except ConfigError:
            found = dict(BUILTIN)
        return tuple((p.name, p.label) for p in found.values())

    def set_app_profile(self, wm_class: str, profile: str | None) -> str | None:
        if self._overrides is None:
            return "Not available"
        if profile is None:
            self._overrides.clear(wm_class)
        elif profile not in dict(self.profiles()):
            return "Unknown profile"
        else:
            self._overrides.set(wm_class, profile)
        return None

    def _profile_map(self) -> dict[str, str]:
        if self._overrides is None:
            return {}
        try:
            data = self._overrides._load()
        except Exception:  # noqa: BLE001
            return {}
        return dict(data)


_APP_KEY = re.compile(r"[a-z0-9][a-z0-9._+-]{0,99}")


def _app_key(key: str) -> bool:
    """A window class as `[apps.<class>]` and the audience overrides key it (a browser or
    chat context such as `slack|general` or `github.com/…` is not an app)."""
    return bool(_APP_KEY.fullmatch(key))


def _reason(e: ConfigError) -> str:
    """A refused write as one line: the file's own problem names the file."""
    if e.key.endswith("config.toml") or "/" in e.key:
        return f"config.toml: {e.reason}"
    return e.reason[:1].upper() + e.reason[1:]


# ---------------------------------------------------------------------- start at login

Run = Callable[[list[str]], tuple[int, str]]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return p.returncode, p.stdout + p.stderr


class StartAtLogin:
    """Whether glanced starts at login: the systemd user unit (Linux) or the LaunchAgent
    (macOS). Switching never stops or starts the running glanced. Blocking: worker only."""

    def __init__(self, mac: bool | None = None, run: Run = _run, uid: int | None = None
                 ) -> None:
        self.mac = sys.platform == "darwin" if mac is None else mac
        self._run = run
        if uid is None:
            import os

            uid = os.getuid()
        self._uid = uid

    def _target(self) -> str:
        from glance.platform.macos.launchd import LABEL, domain

        return f"{domain(self._uid)}/{LABEL}"

    def get(self) -> bool | None:
        """True / False, or None when it cannot be told (no systemd, no agent)."""
        if self.mac:
            from glance.platform.macos.launchd import LABEL, domain

            rc, out = self._run(["launchctl", "print-disabled", domain(self._uid)])
            if rc != 0:
                return None
            for line in out.splitlines():
                if f'"{LABEL}"' in line:
                    return not ("disabled" in line.split("=>")[-1]
                                or "true" in line.split("=>")[-1])
            return True  # not listed: launchd loads it
        rc, out = self._run(["systemctl", "--user", "is-enabled", "glance.service"])
        state = out.strip().splitlines()[0] if out.strip() else ""
        if state in ("enabled", "enabled-runtime", "static", "alias"):
            return True
        if state in ("disabled", "masked", "indirect"):
            return False
        return None

    def set(self, on: bool) -> bool:
        if self.mac:
            argv = ["launchctl", "enable" if on else "disable", self._target()]
        else:
            argv = ["systemctl", "--user", "enable" if on else "disable", "glance.service"]
        return self._run(argv)[0] == 0


# ---------------------------------------------------------------------- the rest


# The README's privacy list, in the same words.
PRIVACY_TEXT: tuple[str, ...] = (
    "Grammar is checked on this computer. Nothing is sent to an AI until you connect one.",
    "Text sent to an AI is redacted first (keys, tokens, card numbers, private keys).",
    ("Password fields, private browser windows, password managers, banking and sign-in pages "
    "are never read, nor are terminals (except the programs listed below)."),
    "API keys live in the system keyring only, never in config.toml or a log.",
    "Glance's status output and its logs never contain your text.",
    ("A Glance Shot stays in memory: it is saved only when you click Save, and sent to an AI "
    "only by Explain or Ask."),
    ("Once a day Glance asks github.com for the latest version (General ▸ Check for "
    "updates); nothing else is sent."),
)


@dataclass(frozen=True)
class UsageSummary:
    today: str
    month: str
    limit: str | None  # the limit line, or None when there is none


def usage_summary(report: Mapping[str, Any] | None) -> UsageSummary:
    from glance.backends.usage import MONTH_LIMIT_MESSAGE, summary_line

    if not report:
        return UsageSummary("Today: no AI calls", "This month: no AI calls", None)
    p = report.get("periods") or {}

    def line(label: str, key: str) -> str:
        total = (p.get(key) or {}).get("total")
        if not total or not total.get("calls"):
            return f"{label}: no AI calls"
        try:
            return summary_line(label, total)
        except (KeyError, TypeError):
            return f"{label}: {total.get('calls')} calls"

    limit = report.get("monthly_cost_limit_usd")
    note = None
    if report.get("limit_reason") == "month":
        note = MONTH_LIMIT_MESSAGE
    elif isinstance(limit, (int, float)):
        note = f"Limit: ${limit:g} a month"
    return UsageSummary(line("Today", "today"), line("This month", "this_month"), note)


def doctor_summary(results: Sequence[Any]) -> str:
    """One line over `glance doctor`'s results."""
    fails = sum(1 for r in results if r.status == "fail")
    warns = sum(1 for r in results if r.status == "warn")
    if not fails and not warns:
        return f"All {len(results)} checks passed"
    parts = [f"{n} {w}" for n, w in ((fails, "failed"), (warns, "to look at")) if n]
    return ", ".join(parts) + f" of {len(results)} checks"


def backend_status(info: Mapping[str, Any] | None) -> str:
    """A backend's state as `backend_list` reports it, in a few words."""
    if not info:
        return "Not checked yet"
    if not info.get("installed"):
        return "Not installed"
    if info.get("signed_in") is True:
        return "Signed in"
    if info.get("signed_in") is False:
        return "Signed out"
    return "Installed"


class SettingsServices:
    """What the window asks glanced (over ctl.sock). Every method blocks: worker only.
    Each returns the reply dict, or None when glanced is not running."""

    def __init__(self, request: Callable[..., dict[str, Any] | None] | None = None) -> None:
        if request is None:
            from glance.cli import _request

            request = _request
        self._request = request

    def backends(self, refresh: bool = False) -> dict[str, Any] | None:
        return self._request("backend_list", {"refresh": refresh}, 60.0 if refresh else 15.0)

    def use_backend(self, name: str) -> dict[str, Any] | None:
        return self._request("backend_use", {"name": name}, 15.0)

    def keys(self) -> dict[str, Any] | None:
        return self._request("backend_keys", {}, 10.0)

    def set_key(self, provider: str, key: str) -> dict[str, Any] | None:
        return self._request("backend_key", {"provider": provider, "key": key}, 30.0)

    def remove_key(self, provider: str) -> dict[str, Any] | None:
        return self._request("backend_key_delete", {"provider": provider}, 30.0)

    def usage(self) -> dict[str, Any] | None:
        reply = self._request("usage", {}, 5.0)
        if reply is None:
            try:
                from glance.cli import _usage_offline

                return _usage_offline(None)
            except Exception:  # noqa: BLE001
                return None
        return reply

    def status(self) -> dict[str, Any] | None:
        return self._request("status", {}, 5.0)

    def doctor(self) -> list[Any]:
        from glance.doctor.runner import default_env, run_checks

        if sys.platform == "darwin":
            from glance.platform.macos.doctor import CHECKS, mac_env

            return run_checks(mac_env(), CHECKS)
        from glance.doctor.checks import CHECKS as LINUX_CHECKS

        return run_checks(default_env(), LINUX_CHECKS)

