"""The Ctrl+G window's logic: one prompt, its issues, tone-chip previews and a one-step undo.

The prompt lives only in the `EditSession` (and the window showing it); `EditSessionHost`
drops it the moment the shim has its answer. Every backend call goes through
`rewrite_text`, which shields the text first.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from glance.audience.profiles import BUILTIN
from glance.backends.base import Ctx
from glance.backends.prompts import TONES
from glance.daemon.rewrite import RewriteOutcome, partial_text, rewrite_text
from glance.grammar.issues import Issue, apply_edit, issue_edit
from glance.privacy.redact import shield
from glance.protocol import Rect

PROFILE = "ai_prompt"
APP_LABEL = "claude-code"
# What IgnoredRules sees as the "app" for per-app ignores made in this window.
WM_CLASS = "claude-code"

RunBg = Callable[[Callable[[], Any], Callable[[Any], None]], None]


def prompt_ctx() -> Ctx:
    p = BUILTIN[PROFILE]
    return Ctx(p.name, p.target_tone, p.strictness, APP_LABEL)


@dataclass(frozen=True)
class Preview:
    """What a running or finished preview shows. `outcome` is None while it streams."""

    tone: str
    partial: str
    outcome: RewriteOutcome | None


class EditSession:
    """`run_bg(work, done)` runs `work` off the UI thread and `done(result)` back on it.
    `on_update` passed to `preview` is called on the UI thread for the final result and on
    the worker thread for streamed partials (the window marshals those itself)."""

    def __init__(self, text: str, grammar: Any, issue_filter: Any, supervisor: Any, ctx: Ctx,
                 run_bg: RunBg) -> None:
        self._text = text
        self._grammar, self._filter, self._supervisor = grammar, issue_filter, supervisor
        self._ctx, self._run_bg = ctx, run_bg
        profile = BUILTIN.get(ctx.profile)
        self._ignore = frozenset(profile.ignore_rules) if profile else frozenset()
        self._issues: list[Issue] | None = None
        self._undo: str | None = None
        self._gen = 0  # bumps on every text change and every new preview
        self.preview_tone: str | None = None
        self.outcome: RewriteOutcome | None = None

    # -- text ------------------------------------------------------------------
    @property
    def text(self) -> str:
        return self._text

    @property
    def can_undo(self) -> bool:
        return self._undo is not None

    def _replace(self, text: str, undoable: bool) -> None:
        if text == self._text:
            return
        self._undo = self._text if undoable else None
        self._text = text
        self._issues = None
        self._gen += 1
        self.outcome = None
        self.preview_tone = None

    def set_text(self, t: str) -> None:
        """The user typed: issues and any preview are stale, and so is the undo step."""
        self._replace(t, undoable=False)

    def undo(self) -> None:
        if self._undo is not None:
            before = self._undo
            self._replace(before, undoable=False)

    # -- issues ----------------------------------------------------------------
    def issues(self) -> list[Issue]:
        if self._issues is None:
            try:
                found = self._grammar.check(self._text)
            except Exception:  # noqa: BLE001 - no Harper: the window still works without it
                found = []
            self._issues = sorted(
                self._filter.apply(self._text, found, WM_CLASS, extra_ignored=self._ignore),
                key=lambda i: (i.start, i.end))
        return list(self._issues)

    def accept(self, issue_id: str, choice: int = 0) -> None:
        issue = next((i for i in self.issues() if i.id == issue_id), None)
        if issue is None or not 0 <= choice < len(issue.replacements):
            return
        self._replace(apply_edit(self._text, issue_edit(issue, choice)), undoable=True)

    def fix_all_safe(self) -> None:
        edits = []
        for i in self.issues():  # sorted; an edit overlapping the previous one waits
            if i.safe and i.replacements and (not edits or i.start >= edits[-1].end):
                edits.append(issue_edit(i))
        text = self._text
        for e in reversed(edits):
            text = apply_edit(text, e)
        self._replace(text, undoable=True)

    # -- tone chips ------------------------------------------------------------
    @property
    def chips(self) -> list[tuple[str, str]]:
        """(key, label): the ai_prompt chips, Sharpen prompt first, then custom."""
        keys = [*BUILTIN[PROFILE].chips, "custom"]
        return [(k, TONES[k].label) for k in keys]

    def preview(self, tone: str, custom: str | None = None,
                on_update: Callable[[Preview], None] | None = None) -> None:
        self._gen += 1
        gen, text = self._gen, self._text
        self.preview_tone, self.outcome = tone, None
        shown = shield(text)  # only to show partials the way the user will see them
        raw: list[str] = []

        def delta(d: str) -> None:  # worker thread
            raw.append(d)
            if on_update is not None and self._gen == gen:
                on_update(Preview(tone, partial_text(shown, "".join(raw)), None))

        def work() -> RewriteOutcome:
            return rewrite_text(self._supervisor, text, tone, self._ctx, custom, delta)

        def done(outcome: RewriteOutcome) -> None:
            if self._gen != gen or self._text != text:
                return  # the text changed, or a newer preview started: this one is stale
            self.outcome = outcome
            if on_update is not None:
                on_update(Preview(tone, outcome.text or "", outcome))

        self._run_bg(work, done)

    def apply_preview(self) -> bool:
        o = self.outcome
        if o is None or not o.applicable or o.text is None:
            return False
        self._replace(o.text, undoable=True)
        return True


# ---------------------------------------------------------------- the daemon side

OpenWindow = Callable[[EditSession, "Rect | None", Callable[[str, str], None]], None]


class EditSessionHost:
    """Answers `edit_session {text, window}` on ctl.sock. Main loop only.

    `reply(dict)` sends the answer to the shim, later, when the window is done; it may raise
    ConnectionError if the shim is gone. One window at a time: a second request while one
    is open brings the open one to the front and is itself answered `cancel`.
    Pause does not block it: the user asked for it explicitly.
    """

    def __init__(self, grammar: Any, issue_filter: Any, supervisor: Any, run_bg: RunBg, *,
                 open_window: OpenWindow, present_window: Callable[[], None],
                 window_rect: Callable[[int], Rect | None]) -> None:
        self._grammar, self._filter, self._supervisor = grammar, issue_filter, supervisor
        self._run_bg = run_bg
        self._open, self._present, self._rect = open_window, present_window, window_rect
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def handle(self, payload: dict[str, Any], reply: Callable[[dict[str, Any]], None]) -> None:
        text, window = payload.get("text"), payload.get("window", 0)
        if not isinstance(text, str) or isinstance(window, bool) or not isinstance(window, int):
            _send(reply, {"ok": False, "error": "bad request"})
            return
        if self._busy:
            self._present()
            _send(reply, {"action": "cancel"})
            return
        session = EditSession(text, self._grammar, self._filter, self._supervisor,
                              prompt_ctx(), self._run_bg)
        answered = False

        def on_done(action: str, new_text: str) -> None:
            nonlocal answered, session
            if answered:
                return
            answered = True
            self._busy = False
            session = None  # type: ignore[assignment]  # the prompt is dropped here
            if action in ("apply", "editor"):
                _send(reply, {"action": action, "text": new_text})
            else:
                _send(reply, {"action": "cancel"})

        self._busy = True
        try:
            over = self._rect(window)
        except Exception:  # noqa: BLE001 - no rectangle: the window is centred on screen
            over = None
        try:
            self._open(session, over, on_done)
        except Exception:  # noqa: BLE001 - the shim must hear back: it opens the real editor
            self._busy = False
            answered = True
            _send(reply, {"ok": False, "error": "window failed"})


def _send(reply: Callable[[dict[str, Any]], None], msg: dict[str, Any]) -> None:
    try:
        reply(msg)
    except ConnectionError:
        pass  # the shim left (Claude Code quit): nothing to tell
