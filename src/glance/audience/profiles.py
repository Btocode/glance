"""Built-in audience profiles and the `[[profile]]` config overlay."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from glance.backends.prompts import TONES
from glance.config import ConfigError


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    target_tone: str
    chips: tuple[str, ...]
    strictness: str
    check: bool = True
    ignore_rules: tuple[str, ...] = ()
    guard: bool = False


# Harper rule names: SentenceCapitalization (lowercase sentence start) and
# CapitalizePersonalPronouns (lowercase "i"). Harper has no missing-final-period rule
# that fires on chat text, so none is listed.
_CASUAL_IGNORED = ("SentenceCapitalization", "CapitalizePersonalPronouns")

BUILTIN: dict[str, Profile] = {p.name: p for p in (
    Profile("ai_prompt", "AI prompt", "clear, specific, complete",
            ("sharpen_prompt", "add_structure", "shorter"), "relaxed",
            ignore_rules=("SentenceCapitalization",)),
    Profile("team_channel", "Team channel", "friendly, clear, professional",
            ("friendlier", "more_direct", "shorter", "professional"), "normal", guard=True),
    Profile("dm", "Direct message", "casual, warm",
            ("friendlier", "more_direct", "shorter"), "relaxed",
            ignore_rules=_CASUAL_IGNORED, guard=True),
    Profile("email", "Email", "professional, courteous",
            ("professional", "friendlier", "shorter", "more_direct"), "strict"),
    Profile("public_post", "Public post", "clear, engaging",
            ("shorter", "friendlier", "professional", "more_direct"), "normal"),
    Profile("code_review", "Code review", "constructive, specific",
            ("friendlier", "more_direct", "shorter"), "normal"),
    Profile("default", "Default", "clear, neutral",
            ("friendlier", "more_direct", "shorter", "professional"), "normal"),
)}

_FIELDS = {f.name for f in dataclasses.fields(Profile)}


def load_profiles(raw: dict[str, Any]) -> dict[str, Profile]:
    """Built-ins overlaid with `[[profile]]` tables (override or add by `name`)."""
    out = dict(BUILTIN)
    tables = raw.get("profile", [])
    if not isinstance(tables, list):
        raise ConfigError("profile", "expected an array of tables")
    for n, t in enumerate(tables):
        where = f"profile[{n}]"
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"]:
            raise ConfigError(where, "needs a name")
        for k in t:
            if k not in _FIELDS:
                raise ConfigError(f"{where}.{k}", "unknown key")
        base = out.get(t["name"]) or Profile(
            t["name"], t["name"], "clear, neutral", BUILTIN["default"].chips, "normal")
        kw = dict(t)
        for k in ("chips", "ignore_rules"):
            if k in kw:
                if not isinstance(kw[k], list) or not all(isinstance(x, str) for x in kw[k]):
                    raise ConfigError(f"{where}.{k}", "expected a list of strings")
                kw[k] = tuple(kw[k])
        for chip in kw.get("chips", ()):
            if chip not in TONES or chip == "custom":
                raise ConfigError(f"{where}.chips", f"unknown chip {chip!r}")
        if kw.get("strictness", base.strictness) not in ("relaxed", "normal", "strict"):
            raise ConfigError(f"{where}.strictness", "expected relaxed, normal or strict")
        try:
            out[t["name"]] = dataclasses.replace(base, **kw)
        except TypeError as e:  # pragma: no cover
            raise ConfigError(where, str(e)) from e
    return out
