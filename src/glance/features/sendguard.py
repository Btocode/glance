"""Send-guard: an amber chip when a chat message looks risky to send. Never blocks.

Enter is not handled here or anywhere else: the guard only contributes a chip label (and a
panel banner, drawn by the UI) for the text currently in the field.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from glance.audience.profiles import Profile
from glance.backends.base import Analysis
from glance.daemon.ai import AiCoordinator
from glance.daemon.controller import Controller
from glance.grammar.store import IgnoredRules
from glance.privacy.entities import OBJECT, find_protected
from glance.protocol import FieldSnapshot

GUARD_LABELS = {
    "hostile": "Sounds hostile",
    "missing_attachment": "Attachment mentioned — attached?",
    "missing_link": "Link mentioned — none here",
}
# Priority: the worst thing to send first.
_ORDER = ("hostile", "missing_attachment", "missing_link")

_LINK_WORDS = re.compile(r"\b(here'?s|see|check|at) (the|this|my) link\b|\blink (below|attached|here)\b",
                         re.IGNORECASE)
_ATTACH_WORDS = re.compile(
    r"\b(attached|attaching|attachment|see the (screenshot|file|doc|pdf)|i'?ve attached)\b",
    re.IGNORECASE)


def _local_reasons(text: str) -> set[str]:
    # An attachment outside the text box cannot be seen, so the label is only a question and
    # the user can silence it per app.
    if OBJECT in text or any(s.kind == "url" for s in find_protected(text)):
        return set()
    found = set()
    if _ATTACH_WORDS.search(text):
        found.add("missing_attachment")
    if _LINK_WORDS.search(text):
        found.add("missing_link")
    return found


def local_guard(text: str) -> str | None:
    """The first local reason (attachment before link), or None."""
    found = _local_reasons(text)
    return next((r for r in _ORDER if r in found), None)


def guard_reason(text: str, analysis: Analysis | None, profile: Profile,
                 ignored: IgnoredRules, wm_class: str) -> str | None:
    """Why this text deserves a warning here; None for no warning.

    Each reason is satisfied by a local match or an AI flag; silenced ones are skipped.
    """
    if not profile.guard:
        return None
    found = _local_reasons(text)
    if analysis is not None:
        found |= {f for f in analysis.flags if f in GUARD_LABELS}
    for reason in _ORDER:
        if reason in found and not ignored.is_ignored(f"guard:{reason}", wm_class or None):
            return reason
    return None


class SendGuard:
    """Adds the amber guard label to the chip inputs. Appends hooks; grabs nothing."""

    def __init__(self, controller: Controller, ai: AiCoordinator,
                 ignored: IgnoredRules) -> None:
        self._c = controller
        self._ai = ai
        self._ignored = ignored

    def attach(self) -> None:
        self._c.extra_chip_inputs.append(self._chip_inputs)
        self._c.extra_strip.append(lambda _s, view: replace(view, guard=self.label()))

    def reason(self) -> str | None:
        """The guard reason for the text now in the focused field (for the panel banner)."""
        s = self._c.current
        if s is None or s.gated:
            return None
        r = self._ai.resolved()
        if r is None:
            return None
        return guard_reason(s.text, self._ai.analysis(), r.profile, self._ignored, s.wm_class)

    def label(self) -> str | None:
        reason = self.reason()
        return None if reason is None else GUARD_LABELS[reason]

    def silence(self, scope: str) -> None:
        """Stop warning about the current reason: scope "app" (this app) or "global"."""
        if scope not in ("app", "global"):
            raise ValueError(f"unknown scope: {scope}")
        s, reason = self._c.current, self.reason()
        if s is None or reason is None:
            return
        self._ignored.ignore(f"guard:{reason}", (s.wm_class or None) if scope == "app" else None)
        self._c.refresh()

    def _chip_inputs(self, _s: FieldSnapshot) -> dict[str, Any]:
        label = self.label()
        return {"guard": label} if label else {}
