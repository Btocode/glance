"""The hard privacy gate: decides from field metadata alone whether text may leave capture.

`evaluate` never sees the text. Every check fails closed: matching is case-insensitive and by
substring where a window class or title is involved, so a variant spelling is blocked rather
than let through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from glance.config import Config


@dataclass(frozen=True)
class FieldMeta:
    """What capture knows about the focused field before reading its text.

    `title` must be the AT-SPI frame name: Chrome and Brave put their private-window marker
    there and not in `_NET_WM_NAME` (findings S2.7). `url` is the document's `URI` (Chromium)
    or `DocURL` (Firefox) attribute (S2.4). `terminal_fg` is the terminal's foreground
    process name, when known.
    """

    app: str
    wm_class: str
    title: str
    url: str | None
    role: str
    states: frozenset[str]
    pid: int
    text_len: int
    terminal_fg: str | None = None


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str | None


_ALLOWED = GateDecision(True, None)


def _contains_any(patterns: tuple[str, ...], *values: str) -> bool:
    """True if any non-empty pattern is a case-insensitive substring of any value."""
    folded = [v.casefold() for v in values]
    return any(p and p in v for p in (p.casefold() for p in patterns) for v in folded)


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        # A broken user pattern must still block something, not silently vanish.
        return re.compile(re.escape(pattern), re.IGNORECASE)


def _url_parts(url: str) -> tuple[str, ...]:
    """The strings a denylist pattern is searched in: host, path, and the URL sans fragment.

    Host and path are separate so that anchored patterns (`(^|\\.)okta\\.com`,
    `/login(/|\\?|$)`) work whatever surrounds them in the full URL.
    """
    whole = url.split("#", 1)[0]
    try:
        parts = urlsplit(url)
        return (parts.hostname or "", parts.path, whole)
    except ValueError:
        return (whole,)


class PrivacyGate:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._url_denylist = [_compile(p) for p in cfg.privacy.url_denylist if p]

    def evaluate(self, meta: FieldMeta) -> GateDecision:
        reason = self._reason(meta)
        return _ALLOWED if reason is None else GateDecision(False, reason)

    def _reason(self, meta: FieldMeta) -> str | None:
        privacy = self._cfg.privacy
        url = (meta.url or "").strip()
        # S2.6: Chromium and Firefox both report role `password text`.
        if "password" in meta.role.casefold() or "protected" in meta.states:
            return "password"
        if _contains_any(privacy.app_denylist, meta.wm_class, meta.app):
            return "app_denylist"
        if _contains_any(privacy.incognito_title_patterns, meta.title):
            return "incognito"
        if url and any(rx.search(part) for rx in self._url_denylist for part in _url_parts(url)):
            return "url_denylist"
        if not url and _contains_any(privacy.browser_wm_classes, meta.wm_class):
            return "url_unknown"
        if _contains_any(privacy.terminal_wm_classes, meta.wm_class) and (
            not meta.terminal_fg or meta.terminal_fg not in privacy.terminal_allowlist
        ):
            return "terminal"
        if not self._cfg.app(meta.wm_class).capture:
            return "app_disabled"
        if meta.text_len > self._cfg.general.max_field_chars:
            return "too_long"
        if "editable" not in meta.states:
            return "not_editable"
        return None
