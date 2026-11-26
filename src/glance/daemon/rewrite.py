"""The one rewrite path: shield, ask the backend, clean, unshield, decide whether it may be
typed back. Used by the panel's tone chips and by every other feature that rewrites text."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from glance.backends.base import BackendError, Ctx
from glance.backends.prompts import clean_rewrite
from glance.backends.supervisor import AiUnavailable, BackendSupervisor
from glance.config import Config
from glance.privacy.redact import Shielded, can_apply_rewrite, find_secrets, shield, unshield
from glance.protocol import Edit
from glance.wordlimit import count_words, limit_notice

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
    # Only this [start, end) of a text longer than `[ai] max_words` was rewritten (`text` is
    # still the whole text, the rest unchanged), and (words rewritten, words in all).
    window: tuple[int, int] | None = None
    words: tuple[int, int] | None = None

    def notice(self, where: str = "around your cursor") -> str | None:
        """"Long text: AI rewrote 600 of 1,450 words around your cursor", or None."""
        return None if self.words is None else limit_notice(*self.words, verb="rewrote",
                                                            where=where)

    def edit(self, original: str) -> Edit | None:
        """What applying it types: the rewritten window only (what lies outside it, with any
        mention or link there, is never retyped), else the whole text."""
        if self.text is None:
            return None
        if self.window is None:
            return Edit(0, len(original), self.text)
        w0, w1 = self.window
        return Edit(w0, w1, self.text[w0:len(self.text) - (len(original) - w1)])


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
                 rich: bool = False, speculative: bool = False,
                 window: tuple[int, int] | None = None) -> RewriteOutcome:
    """Rewrite `text` in `tone`. Blocks until the backend is done: call it off the UI thread.

    The backend only ever sees the shielded text. `on_delta` gets the raw deltas as they
    arrive (they still carry placeholders: see `partial_text`). `rich` says the text will be
    typed back into a rich editor; `speculative` uses the supervisor's second lane.
    `window` (see `wordlimit.word_window`): only that part of `text` is sent and rewritten;
    the outcome is still the whole text, with its `window` and `words` set.
    """
    if window is not None and window != (0, len(text)):
        w0, w1 = window
        part = text[w0:w1]
        o = rewrite_text(supervisor, part, tone, ctx, custom, on_delta, rich=rich,
                         speculative=speculative)
        whole = None if o.text is None else text[:w0] + o.text + text[w1:]
        return replace(o, text=whole, window=(w0, w1),
                       words=(count_words(part), count_words(text)))
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
