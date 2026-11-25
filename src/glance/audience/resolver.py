"""Pick the audience profile for a field: remembered override, first matching rule, default."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from glance.audience.profiles import Profile
from glance.backends.base import Ctx
from glance.protocol import FieldSnapshot


@dataclass(frozen=True)
class Rule:
    profile: str
    wm_class: str | None = None
    title: str | None = None
    url: str | None = None

    def matches(self, s: FieldSnapshot) -> bool:
        for pattern, value in ((self.wm_class, s.wm_class), (self.title, s.title),
                               (self.url, s.url)):
            if pattern is None:
                continue
            if value is None or not re.search(pattern, value, re.IGNORECASE):
                return False
        return True


_CHAT = r"^(slack|discord)$"
_SLACK_DM = r"\(DM\)"  # spike S2.4: "<name> (DM) - <Workspace> - Slack"
_DM_URL = r"(app\.slack\.com|discord\.com/channels/@me)"

DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("ai_prompt", url=r"(claude\.ai|chatgpt\.com|chat\.openai\.com|gemini\.google\.com"
                          r"|perplexity\.ai|copilot\.microsoft\.com)"),
    Rule("email", url=r"(mail\.google\.com|outlook\.(live|office)\.com|mail\.proton\.me)"),
    Rule("code_review", url=r"(github\.com/.+/pull/|gitlab\.com/.+/merge_requests/)"),
    Rule("public_post", url=r"(x\.com|twitter\.com|linkedin\.com|reddit\.com"
                            r"|news\.ycombinator\.com|facebook\.com)"),
    Rule("dm", wm_class=_CHAT, title=_SLACK_DM),
    Rule("dm", wm_class=_CHAT, url=r"discord\.com/channels/@me"),
    Rule("team_channel", wm_class=_CHAT),
    Rule("dm", url=_DM_URL, title=_SLACK_DM),
    Rule("dm", url=r"discord\.com/channels/@me"),
    Rule("team_channel", url=r"(app\.slack\.com|discord\.com/channels/)"),
)

_SLACK_TITLE = re.compile(r"^(.*?) \((?:Channel|DM)\) - ")
_DISCORD_TITLE = re.compile(r"^#?(.*?) \| ")


def context_key(s: FieldSnapshot) -> str:
    wm = s.wm_class.casefold()
    if wm in ("slack", "discord"):
        m = (_SLACK_TITLE if wm == "slack" else _DISCORD_TITLE).match(s.title)
        return f"{wm}|{m.group(1)}" if m and m.group(1) else wm
    if s.url:
        parts = urlsplit(s.url)
        if parts.hostname:
            first = next((p for p in parts.path.split("/") if p), "")
            return f"{parts.hostname}/{first}"
    return wm


@dataclass(frozen=True)
class Resolved:
    profile: Profile
    key: str
    source: str  # override | rule | default


class OverrideStore(Protocol):
    def get(self, key: str) -> str | None: ...


class AudienceResolver:
    def __init__(self, profiles: dict[str, Profile], rules: tuple[Rule, ...],
                 overrides: OverrideStore) -> None:
        self._profiles = profiles
        self._rules = rules
        self._overrides = overrides

    def profile(self, name: str) -> Profile | None:
        return self._profiles.get(name)

    def profiles(self) -> tuple[Profile, ...]:
        """Every profile the user can choose, built-ins first."""
        return tuple(self._profiles.values())

    def resolve(self, s: FieldSnapshot) -> Resolved:
        key = context_key(s)
        name = self._overrides.get(key)
        if name in self._profiles:
            return Resolved(self._profiles[name], key, "override")
        for rule in self._rules:
            if rule.profile in self._profiles and rule.matches(s):
                return Resolved(self._profiles[rule.profile], key, "rule")
        return Resolved(self._profiles["default"], key, "default")

    def ctx(self, s: FieldSnapshot, r: Resolved, mixed_language: bool = False) -> Ctx:
        return Ctx(profile=r.profile.name, target_tone=r.profile.target_tone,
                   strictness=r.profile.strictness, app_label=s.wm_class,
                   mixed_language=mixed_language)
