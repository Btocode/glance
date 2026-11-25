"""Select-anything: rewrite the PRIMARY selection of any app on an explicit hotkey.

The universal fallback for apps whose fields Glance cannot capture. What it does:

  * refuses unless the selection belongs to the window that is active now (a selection left
    over in a password manager must never be read while the user is in Slack), unless the
    privacy gate allows that window (denylisted apps, private windows, sensitive pages), and
    while the field capture reports focused in that window is a password field (the window
    gate cannot see the field: a login dialog of an allowed app is still a password);
  * where the daemon also has a live snapshot of the field and the selection is in it exactly
    once, the rewrite goes through the normal verified write-back path;
  * otherwise it shows a panel at the pointer: the local grammar issues (read-only), tone
    chips, a rewrite preview, and two actions. "Copy rewrite" is the default, because what a
    paste does cannot be checked. "Paste over selection" is secondary, absent for terminals,
    and its result is only ever `unverified`.

The selection text lives in the session object for as long as the panel is open.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from glance.audience.resolver import Resolved
from glance.backends.prompts import TONES
from glance.config import Config
from glance.daemon.ai import ASK_PROMPT, BANNERS, CUSTOM
from glance.daemon.controller import Controller
from glance.daemon.rewrite import RewriteOutcome, is_rich_editor, rewrite_text
from glance.diffing import word_diff
from glance.grammar.filters import IssueFilter
from glance.log import log_event
from glance.privacy.gate import FieldMeta, PrivacyGate, _compile
from glance.protocol import ApplyResult, Edit, FieldSnapshot, text_sha256
from glance.ui.panel_model import IssueRow, PanelView, ToneChip, build_panel
from glance.ui.ports import (
    CapturePort,
    GrammarPort,
    NotifierPort,
    OverlayPort,
    SelectionPort,
    WindowInfo,
)
from glance.writeback.engine import PASTE_ID

T = TypeVar("T")

NO_SELECTION = "Select some text first"
OTHER_WINDOW = "Select text in this window first"
TOO_LONG = "That selection is too long"
PRIVATE = "Glance does not read this window"
PASTED_HINT = "Pasted — Ctrl+Z in the app undoes it"
PASTE_FAILED = "Glance could not paste. Use Copy rewrite instead"
BACK_TO_WINDOW = "Go back to the window with the selection, or use Copy rewrite."
NOT_PASTABLE = "This rewrite cannot be pasted safely. Use Copy rewrite instead."
TERMINAL_COPY_ONLY = "Terminals get Copy only."
BUSY = "Glance is busy with another change. Try again."

# A gate verdict that does not apply to an explicit action on a selection: terminals are
# allowed (the spec lets the user ask for a rewrite there) and "capture off" is about what
# Glance reads on its own.
_NOT_BLOCKING = frozenset({"terminal", "app_disabled"})
# What a page lookup answers: the document URL and the frame title, either may be None.
PageInfo = tuple[str | None, str | None]


@dataclass
class _Rewrite:
    tone: str
    custom: str | None
    state: str = "loading"          # loading | ready | failed
    outcome: RewriteOutcome | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.tone, self.custom)


@dataclass
class _Session:
    text: str
    window: WindowInfo
    resolved: Resolved
    terminal: bool
    rich: bool
    anchor: tuple[str, str] | None   # (field id, text hash) of the field in view at trigger
    xy: tuple[int, int]
    issues: tuple[IssueRow, ...] = ()
    rw: _Rewrite | None = None
    pending: str | None = None       # copy | paste: do it as soon as the rewrite is ready
    banner: str | None = None
    pasting: bool = False


def _blank_view(w: WindowInfo) -> FieldSnapshot:
    """What the audience resolver gets: the window's class and title, never its text."""
    return FieldSnapshot(
        app=w.wm_class, wm_class=w.wm_class, title=w.title, url=None, role="selection",
        text="", caret=0, caret_rect=None, prev_char_rect=None, field_rect=None,
        window_rect=None, field_id="selection", capture_confidence=1.0, seq=0, pid=w.pid,
        gated=None)


def _contains(patterns: tuple[str, ...], *values: str) -> bool:
    folded = [v.casefold() for v in values]
    return any(p and p.casefold() in v for p in patterns for v in folded)


class SelectAnything:
    """See the module docstring. Every method runs on the main loop."""

    def __init__(
        self,
        controller: Controller,
        selection: SelectionPort,
        gate: Callable[[], PrivacyGate],
        cfg: Callable[[], Config],
        grammar: GrammarPort,
        issue_filter: IssueFilter,
        supervisor: Any,
        resolver: Any,
        overlay: OverlayPort,
        capture: CapturePort,
        notifier: NotifierPort,
        run_bg: Callable[[Callable[[], T], Callable[[T], None]], None],
        page_lookup: Callable[[Callable[[PageInfo | None], None]], None] | None = None,
        run_read: Callable[[Callable[[], T], Callable[[T], None]], None] | None = None,
    ) -> None:
        """`page_lookup(done)` asks glance-capture for the active browser tab's (url, title)
        and calls `done` with it, or with None when it cannot say. `run_read(work, done)`
        reads the selection off the main loop (an owner that never answers costs a second)
        and calls `done` back on it; default `run_bg`."""
        self._c = controller
        self._selection = selection
        self._gate = gate
        self._cfg = cfg
        self._grammar = grammar
        self._filter = issue_filter
        self._supervisor = supervisor
        self._resolver = resolver
        self._overlay = overlay
        self._capture = capture
        self._notifier = notifier
        self._run_bg = run_bg
        self._page_lookup = page_lookup
        self._run_read = run_read or run_bg
        self._sess: _Session | None = None
        self._looking = 0  # bumped by every trigger: a late page lookup is dropped

    @property
    def is_open(self) -> bool:
        return self._sess is not None

    # ------------------------------------------------------------------ the hotkey

    def trigger(self) -> None:
        self.close()
        self._looking += 1
        if self._c.status()["paused"]:
            return
        cfg = self._cfg()
        # Who owns the selection, and may Glance look at that window at all, are settled
        # before a single byte of the selection is read.
        owner, active = self._selection.owner(), self._selection.active()
        if owner is None or active is None or owner.xid != active.xid:
            self._say(OTHER_WINDOW)
            return
        if self._in_password_field(active):
            log_event("select_anything_refused", reason="password")
            self._say(PRIVATE)
            return
        if not _contains(cfg.privacy.browser_wm_classes, active.wm_class):
            self._open(active, None, (active.title,))
            return
        # A browser: the page decides (a bank page's text must not be sent), and only the
        # browser's own tab knows its URL.
        ticket = self._looking
        if self._page_lookup is None:
            self._browser(active, None, ticket)
        else:
            self._page_lookup(lambda info: self._browser(active, info, ticket))

    def _in_password_field(self, active: WindowInfo) -> bool:
        """True if capture's focused field is a password field of the active window's
        process (role `password text` or state `protected`: the gate's `password`)."""
        cur = self._c.current
        return cur is not None and cur.gated == "password" and cur.pid == active.pid

    def _browser(self, active: WindowInfo, info: PageInfo | None, ticket: int) -> None:
        now = self._selection.active()
        if ticket != self._looking or now is None or now.xid != active.xid:
            return  # the user moved on while the page was being looked up
        url, title = info if info is not None else (None, None)
        titles = (active.title, title) if title else (active.title,)
        if url:
            self._open(active, url, titles)
            return
        # No URL: the title is all there is. A bank, SSO or checkout page is refused by it;
        # anything else goes ahead: the hotkey itself is the user's permission. (The gate's
        # compiler: a broken user pattern still blocks.)
        patterns = [_compile(p) for p in self._cfg().privacy.url_denylist if p]
        if any(rx.search(t) for rx in patterns for t in titles):
            self._say(PRIVATE)
            return
        self._open(active, None, titles)

    def _open(self, active: WindowInfo, url: str | None, titles: tuple[str, ...]) -> None:
        gate = self._gate()
        for title in titles:
            meta = FieldMeta(
                app=active.wm_class, wm_class=active.wm_class, title=title,
                url=url or "selection:", role="selection", states=frozenset({"editable"}),
                pid=active.pid, text_len=0)
            decision = gate.evaluate(meta)
            if not decision.allowed and decision.reason not in _NOT_BLOCKING:
                log_event("select_anything_refused", reason=decision.reason)
                self._say(PRIVATE)
                return
        limit = self._cfg().general.max_field_chars
        ticket = self._looking
        # What is in view now, before the hotkey's own blur reaches the controller.
        cur = self._c.current
        anchor = None if cur is None or cur.gated else (cur.field_id, text_sha256(cur.text))
        xy = self._selection.pointer()
        self._run_read(lambda: self._selection.primary_text(limit * 4),
                       lambda text: self._read(active, text, ticket, anchor, xy))

    def _read(self, active: WindowInfo, text: str | None, ticket: int,
              anchor: tuple[str, str] | None, xy: tuple[int, int]) -> None:
        if ticket != self._looking:
            return  # another trigger or a close came first: this read is history
        cfg = self._cfg()
        limit = cfg.general.max_field_chars
        if text is None or not text.strip():
            self._say(NO_SELECTION)
            return
        if len(text) > limit:
            self._say(TOO_LONG)
            return
        sess = _Session(
            text=text, window=active, resolved=self._resolver.resolve(_blank_view(active)),
            terminal=_contains(cfg.privacy.terminal_wm_classes, active.wm_class),
            rich=is_rich_editor(active.wm_class, cfg), anchor=anchor,
            xy=xy)
        self._sess = sess
        self._render()
        self._run_bg(lambda: self._check(text), lambda found: self._issues_ready(sess, found))

    # ------------------------------------------------------------------ actions

    def preview(self, tone: str, custom: str | None = None) -> None:
        """Rewrite the selection in `tone` and show the result in the panel."""
        self._preview(tone, custom, None)

    def _preview(self, tone: str, custom: str | None, then: str | None) -> None:
        """`then` (copy | paste) is done as soon as the rewrite is ready."""
        s = self._sess
        if s is None:
            return
        if tone == CUSTOM and custom is None:
            self._overlay.ask_text(ASK_PROMPT, lambda text, _back: self._preview(CUSTOM, text, then))
            return
        if tone == CUSTOM:
            custom = (custom or "").strip()
            if not custom:
                return
        elif tone not in TONES:
            return
        else:
            custom = None
        if s.rw is not None and s.rw.key == (tone, custom) and s.rw.state != "failed":
            s.pending = then or s.pending
            return
        self._cancel_rewrite(s)
        s.pending = then
        s.banner = None
        s.rw = rw = _Rewrite(tone, custom)
        self._render()
        ctx = self._resolver.ctx(_blank_view(s.window), s.resolved)
        text, rich = s.text, s.rich

        def work() -> RewriteOutcome:
            try:
                return rewrite_text(self._supervisor, text, tone, ctx, custom, rich=rich)
            except Exception as e:  # noqa: BLE001 - the panel must always leave "loading"
                log_event("rewrite_error", kind=type(e).__name__)
                return RewriteOutcome(None, False, "failed")

        self._run_bg(work, lambda outcome: self._rewrite_done(s, rw, outcome))

    def copy(self) -> None:
        """Default action: the rewrite goes to the clipboard."""
        self._act("copy")

    def paste_over(self) -> None:
        """Secondary action: replace the selection with the rewrite. Never for terminals."""
        s = self._sess
        if s is not None and s.terminal:
            return
        self._act("paste")

    def close(self) -> None:
        self._looking += 1  # a selection read still on its way is dropped
        s, self._sess = self._sess, None
        if s is not None:
            self._cancel_rewrite(s)
            self._overlay.hide_panel()

    # ------------------------------------------------------------------ controller hooks

    def on_snapshot(self, snap: FieldSnapshot) -> None:
        s = self._sess
        if s is None:
            return
        if s.anchor != (snap.field_id, text_sha256(snap.text)):
            self.close()  # another field, or the text moved on: the selection is history

    def on_pause_changed(self, paused: bool) -> None:
        if paused:
            self.close()

    def on_capture_lost(self) -> None:
        self.close()

    def on_apply_result(self, r: ApplyResult) -> None:
        s = self._sess
        if s is None or not s.pasting or r.apply_id != PASTE_ID:
            return
        s.pasting = False
        if r.status == "unverified":
            self._say(PASTED_HINT)
            self.close()
        else:
            self._say(PASTE_FAILED, r.detail)
            s.banner = PASTE_FAILED
            self._render()

    # ------------------------------------------------------------------ internals

    def _say(self, summary: str, body: str = "") -> None:
        self._notifier.notify(summary, body)

    def _cancel_rewrite(self, s: _Session) -> None:
        if s.rw is not None and s.rw.state == "loading":
            self._supervisor.cancel_all()
        s.rw = None
        s.pending = None

    def _check(self, text: str) -> list[Any]:
        if not self._grammar.alive:
            return []
        try:
            return list(self._grammar.check(text))
        except Exception as e:  # noqa: BLE001 - the issue list is a nicety
            log_event("select_check_error", kind=type(e).__name__)
            return []

    def _issues_ready(self, s: _Session, found: list[Any]) -> None:
        if self._sess is not s:
            return
        kept = self._filter.apply(s.text, found, s.window.wm_class)
        rows = build_panel(s.text, sorted(kept, key=lambda i: (i.start, i.end))).issues
        # A list, not a fixer: the replacements are dropped.
        s.issues = tuple(IssueRow(r.issue_id, r.excerpt, r.message, (), r.rule) for r in rows)
        self._render()

    def _rewrite_done(self, s: _Session, rw: _Rewrite, outcome: RewriteOutcome) -> None:
        if self._sess is not s or s.rw is not rw:
            return
        rw.outcome = outcome
        rw.state = "ready" if outcome.text is not None or outcome.reason == "no_change" \
            else "failed"
        s.banner = BANNERS.get(outcome.reason or "", BANNERS["failed"]) if outcome.reason \
            else None
        pending, s.pending = s.pending, None
        self._render()
        if pending is not None:
            self._act(pending)

    def _act(self, kind: str) -> None:
        s = self._sess
        if s is None:
            return
        rw = s.rw
        if rw is None or rw.state == "failed":
            chip = self._chips(s)[0] if self._chips(s) else None
            if chip is None:
                return
            self._preview(chip.key, None, kind)
            return
        if rw.state == "loading":
            s.pending = kind
            return
        outcome = rw.outcome
        if outcome is None or outcome.text is None:
            return
        if kind == "copy":
            self._overlay.copy_to_clipboard(outcome.text)
            self.close()
        else:
            self._paste(s, outcome)

    def _paste(self, s: _Session, outcome: RewriteOutcome) -> None:
        assert outcome.text is not None
        if not outcome.applicable:
            s.banner = NOT_PASTABLE
            self._render()
            return
        now = self._selection.active()
        if now is None or now.xid != s.window.xid:
            s.banner = BACK_TO_WINDOW
            self._render()
            return
        edit = self._verified_edit(s, outcome.text)
        if edit is not None:
            if self._c.request_apply(edit):
                self.close()
            else:
                s.banner = BUSY
                self._render()
            return
        s.pasting = True
        self._capture.send_paste(outcome.text, s.window.xid, s.window.wm_class)

    def _verified_edit(self, s: _Session, rewritten: str) -> Edit | None:
        """The edit for the verified path, when the daemon sees this very field and the
        selection occurs in it exactly once."""
        cur = self._c.current
        if (cur is None or cur.gated or cur.pid != s.window.pid
                or cur.wm_class.casefold() != s.window.wm_class.casefold()
                or cur.text.count(s.text) != 1):
            return None
        start = cur.text.index(s.text)
        return Edit(start, start + len(s.text), rewritten)

    # ------------------------------------------------------------------ the panel

    def _chips(self, s: _Session) -> tuple[ToneChip, ...]:
        chips = [ToneChip(key, TONES[key].label, None)
                 for key in s.resolved.profile.chips if key in TONES]
        return (*chips, ToneChip(CUSTOM, TONES[CUSTOM].label, None))

    def _render(self) -> None:
        s = self._sess
        if s is None:
            return
        rw = s.rw
        preview = None
        if rw is not None and rw.outcome is not None and rw.outcome.text is not None:
            preview = word_diff(s.text, rw.outcome.text)
        view = PanelView(
            mode="selection", tone_chips=self._chips(s),
            issues=s.issues, banner=s.banner,
            preview=preview, preview_state=None if rw is None else rw.state,
            profile_label=s.resolved.profile.label, can_paste=not s.terminal)
        self._overlay.show_panel(view, *s.xy)

