"""The one rewrite path: shield, ask the backend, clean, unshield, decide whether it may be
typed back. Used by the panel's tone chips and by every other feature that rewrites text."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from glance.backends.base import BackendError, Ctx
from glance.backends.prompts import clean_rewrite
from glance.backends.supervisor import AiUnavailable, BackendSupervisor
from glance.config import Config
from glance.privacy.redact import Shielded, can_apply_rewrite, find_secrets, shield, unshield

# Editors where mentions, emoji and links are objects, not characters (findings S2.5).
RICH_WM_CLASSES = frozenset({"slack", "discord"})
CUSTOM_MAX_CHARS = 300
_PLACEHOLDER = re.compile(r"\[\[GLANCE_[SE]\d+\]\]")
# The start of a placeholder that has not fully arrived yet, at the end of a stream.
_UNFINISHED = re.compile(r"\[(?:\[(?:G(?:L(?:A(?:N(?:C(?:E(?:_(?:[SE]\d*\]?)?)?)?)?)?)?)?)?)?\Z")


@dataclass(frozen=True)
class RewriteOutcome:
    """`text` is the rewrite with the user's own content restored, whenever there is one: it
    can be offered as "Copy rewrite" even when `applicable` is false.

    `reason` says why it is not applicable: no_change, placeholders (the model lost or
    invented one), objects (the field holds embedded objects), rich (typing it back would
    flatten a mention, emoji, link or code span), limit, none, battery, failed or cancelled.
    """

    text: str | None
    applicable: bool
    reason: str | None


def is_rich_editor(wm_class: str, cfg: Config) -> bool:
    """True where protected entities are rich objects that typed text cannot recreate.

    `[apps.X] rich = true|false` decides for app X; otherwise Slack, Discord and every
    browser count as rich.
    """
    name = wm_class.casefold()
    choice = cfg.app(wm_class).rich
    if choice is not None:
        return choice
    return name in RICH_WM_CLASSES or name in {b.casefold()
                                               for b in cfg.privacy.browser_wm_classes}


def _restore_known(text: str, shielded: Shielded) -> str:
    originals = dict(shielded.mapping)
    return _PLACEHOLDER.sub(lambda m: originals.get(m.group(), m.group()), text)


def partial_text(shielded: Shielded, raw: str) -> str:
    """A rewrite that is still streaming, as the user may see it: known placeholders
    restored, a placeholder that is still arriving left out."""
    return _restore_known(_UNFINISHED.sub("", raw), shielded)


def finish_rewrite(original: str, shielded: Shielded, raw: str,
                   rich: bool = False) -> RewriteOutcome:
    """Turn a backend's raw answer for `shielded` into what may be done with it."""
    if not raw.strip():
        return RewriteOutcome(None, False, "failed")
    # The model saw the shielded text: that is what "unchanged" and "quoted" are judged on.
    cleaned = clean_rewrite(raw, shielded.text)
    if cleaned is None:
        return RewriteOutcome(None, False, "no_change")
    text = unshield(cleaned, shielded)
    if text is None:
        return RewriteOutcome(_restore_known(cleaned, shielded), False, "placeholders")
    if text.strip() == original.strip():
        return RewriteOutcome(None, False, "no_change")
    if shielded.has_objects:
        return RewriteOutcome(text, False, "objects")
    if not can_apply_rewrite(shielded, rich):
        return RewriteOutcome(text, False, "rich")
    return RewriteOutcome(text, True, None)


def _safe_instruction(custom: str | None) -> str | None:
    """The user's own instruction for a custom rewrite, without anything secret in it."""
    if custom is None:
        return None
    out, pos = [], 0
    for span in find_secrets(custom):
        out += [custom[pos:span.start], "[secret]"]
        pos = span.end
    out.append(custom[pos:])
    return "".join(out)[:CUSTOM_MAX_CHARS]


def rewrite_text(supervisor: BackendSupervisor, text: str, tone: str, ctx: Ctx,
                 custom: str | None = None,
                 on_delta: Callable[[str], None] | None = None, *,
                 rich: bool = False, speculative: bool = False) -> RewriteOutcome:
    """Rewrite `text` in `tone`. Blocks until the backend is done: call it off the UI thread.

    The backend only ever sees the shielded text. `on_delta` gets the raw deltas as they
    arrive (they still carry placeholders: see `partial_text`). `rich` says the text will be
    typed back into a rich editor; `speculative` uses the supervisor's second lane.
    """
    shielded = shield(text)
    parts: list[str] = []
    kwargs = {"speculative": True} if speculative else {}
    try:
        for delta in supervisor.rewrite(shielded.text, tone, ctx, _safe_instruction(custom),
                                        **kwargs):
            parts.append(delta)
            if on_delta is not None:
                on_delta(delta)
    except AiUnavailable as e:
        return RewriteOutcome(None, False, e.reason)
    except BackendError as e:
        return RewriteOutcome(None, False, "cancelled" if e.kind == "cancelled" else "failed")
    return finish_rewrite(text, shielded, "".join(parts), rich)
