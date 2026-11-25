"""What the "Connect an AI" window, the tray menu and `glance backend list` show."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from glance.backends.base import CONNECT, Health
from glance.config import API_DEFAULT_MODEL, DEFAULT_MODELS, BackendCfg

ORDER = ("claude", "codex", "gemini", "opencode", "api")
TITLES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "opencode": "OpenCode",
          "api": "API key"}
STATE_TEXT = {"ready": "Ready", "signed_out": "Signed out", "not_installed": "Not installed",
              "unknown": "Not checked yet"}
_TERMS = " The provider processes your text under its own terms."
STORAGE_NOTES: dict[str, str] = {
    "claude": "Glance runs claude with --no-session-persistence, so nothing is saved locally."
              + _TERMS,
    "codex": "Glance runs codex with --ephemeral, so no session file is kept." + _TERMS,
    "gemini": "Glance deletes the Gemini session after each call if one was written." + _TERMS,
    "opencode": "Glance deletes the OpenCode session after each call." + _TERMS,
    "api": "Nothing is stored locally; the key lives in the system keyring." + _TERMS,
}


@dataclass(frozen=True)
class BackendRow:
    name: str
    title: str
    state: str  # ready | signed_out | not_installed | unknown
    state_text: str
    command: str
    storage_note: str
    selectable: bool
    selected: bool
    model: str = ""  # what runs; "" = the tool's own default (or, for api, not set yet)


def _state(h: Health | None) -> str:
    if h is None:
        return "unknown"
    if not h.installed:
        return "not_installed"
    if h.signed_in is True:
        return "ready"
    if h.signed_in is False:
        return "signed_out"
    return "unknown"


def effective_model(name: str, cfg: BackendCfg) -> str:
    """The model `name` runs: the configured one, else the table default ("" = CLI default)."""
    if name == "api":
        return cfg.api_model or (API_DEFAULT_MODEL if cfg.api_provider == "anthropic" else "")
    return cfg.models.get(name, DEFAULT_MODELS.get(name, ""))


def model_text(model: str, name: str) -> str:
    """How a row shows `model`."""
    if model:
        return model
    return "not set: glance backend api --model ..." if name == "api" else "tool default"


def build_rows(healths: Mapping[str, Health], choice: str | None,
               models: Mapping[str, str] | None = None) -> tuple[BackendRow, ...]:
    """One row per backend in fixed order. `unknown` (installed, sign-in not detectable, e.g.
    gemini) can be chosen: the Test button then finds out."""
    rows = []
    for name in ORDER:
        state = _state(healths.get(name))
        rows.append(BackendRow(
            name=name, title=TITLES[name], state=state, state_text=STATE_TEXT[state],
            command=CONNECT[name], storage_note=STORAGE_NOTES[name],
            selectable=state in ("ready", "unknown"), selected=name == choice,
            model=(models or {}).get(name, "")))
    return tuple(rows)


def headline(rows: tuple[BackendRow, ...]) -> str:
    if not any(r.state == "ready" for r in rows):
        return "Connect an AI"
    chosen = next((r for r in rows if r.selected and r.state == "ready"), None)
    return "Choose your AI" if chosen is None else f"AI: {chosen.title}"
