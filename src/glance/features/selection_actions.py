"""Selection actions: a small Glance mark beside text the user selected anywhere, its action
bar, and the result panel.

Privacy, in order:

  * The mark is driven by "a selection settled" events (`SelectionWatchPort`: XFixes owner
    changes and raw button state on X11, the GNOME Shell extension on Wayland, AX
    notifications on macOS). They carry a point and nothing else: no text is read to show it.
  * Before the mark shows, select-anything's gate decides on the window alone: the selection
    must belong to the active window, which may not be a password field, a denylisted app,
    a private window or a sensitive page, nor a terminal unless `[selection] terminals`.
  * Only a click on an action reads the selection (on a worker thread, as select-anything
    does), and only that action's result is shown. Anything sent to an AI is shielded
    (`privacy.redact.shield`) on the way, as every rewrite is.

The mark never takes the focus, never shows while the user is typing in the field (a field
text change within `TYPING_QUIET_MS`, or a selection made with the keyboard), and hides on a
key, a scroll, a click elsewhere, a focus change, the selection going away, or `IDLE_MS`.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from glance.config import Config
from glance.features.select_anything import NO_SELECTION, TOO_LONG, SelectAnything
from glance.log import log_event
from glance.protocol import FieldSnapshot, Rect, text_sha256
from glance.ui import selection_bar as sb
from glance.ui.ports import SelectionPort, WindowInfo

T = TypeVar("T")


class SelectionWatchPort(Protocol):
    """Selection-settled events, never the selection's text."""

    def start(self, on_settled: Callable[[int, int], None], on_cleared: Callable[[], None],
              on_input: Callable[[str, int, int], None]) -> None: ...

    def stop(self) -> None: ...

    def arm(self, on: bool) -> None:
        """While on: report keys, scrolling, clicks and focus changes as `on_input(kind, x,
        y)` (kind only, never which key)."""
        ...


class NoWatch:
    """For a platform (or a session) without a selection source: only the hotkey works."""

    def start(self, *_a: Any) -> None:
        pass

    def stop(self) -> None:
        pass

    def arm(self, on: bool) -> None:
        pass


@dataclass
class _Mark:
    window: WindowInfo
    point: tuple[int, int]
    xy: tuple[int, int]
    view: sb.SelectionBarView
    hovered: bool = False
    idle: Any = None
    opener: Any = None


@dataclass
class _Result:
    """The result panel's session: the selection read for it lives here while it is open."""

    text: str
    window: WindowInfo
    action: str
    title: str
    xy: tuple[int, int]
    answer: str | None = None
    state: str = "loading"            # loading | ready | failed
    note: str | None = None
    applicable: bool = False          # the answer may replace the selection
    history: list[str] = field(default_factory=list)
    # Text read from a Glance Shot (`open_text`): the panel offers more to do with it, and a
    # private region's text never goes to an AI.
    from_shot: bool = False
    private: bool = False


class SelectionActions:
    """See the module docstring. Every method runs on the main loop."""

    def __init__(self, *, controller: Any, select_anything: SelectAnything,
                 selection: SelectionPort, watch: SelectionWatchPort,
                 cfg: Callable[[], Config], overlay: Any, clock: Any, notifier: Any,
                 run_bg: Callable[[Callable[[], T], Callable[[T], None]], None],
                 run_read: Callable[[Callable[[], T], Callable[[T], None]], None] | None = None,
                 supervisor: Any = None, resolver: Any = None, grammar: Any = None,
                 issue_filter: Any = None,
                 remember: Callable[[str, Any], None] = lambda key, value: None,
                 hint: Callable[[], str | None] = lambda: None) -> None:
        """`remember(dotted key, value)` writes a setting ("Translate to…" keeps its
        language); `hint()` is the selection hotkey as shown to the user."""
        self._c = controller
        self._sa = select_anything
        self._selection = selection
        self._watch = watch
        self._cfg = cfg
        self._overlay = overlay
        self._clock = clock
        self._notifier = notifier
        self._run_bg = run_bg
        self._run_read = run_read or run_bg
        self._supervisor = supervisor
        self._resolver = resolver
        self._grammar = grammar
        self._filter = issue_filter
        self._remember = remember
        self._hint = hint
        self._watching = False
        self._ticket = 0               # bumped by every new selection: late answers are dropped
        self._mark: _Mark | None = None
        self._more = False
        self._typed_at: float | None = None
        self._last_text: tuple[str, str] | None = None  # (field id, text hash) last seen
        self._res: _Result | None = None

    # ------------------------------------------------------------------ switch

    @property
    def watching(self) -> bool:
        return self._watching

    def sync(self) -> None:
        """Follow `[selection] enabled` (at start and after every config change): off means
        no listener at all, and no mark."""
        on = self._cfg().selection.enabled
        if on and not self._watching:
            self._watching = True
            self._watch.start(self.on_settled, self.on_cleared, self.on_input)
        elif not on and self._watching:
            self._watching = False
            self.hide()
            self._watch.stop()

    def stop(self) -> None:
        self.hide()
        self.close()
        if self._watching:
            self._watching = False
            self._watch.stop()

    # ------------------------------------------------------------------ detection

    def on_settled(self, x: int, y: int, length: int | None = None) -> None:
        """A mouse selection settled with the pointer at (x, y). `length` (characters) when
        the platform says it without the text."""
        self.hide()
        self._ticket += 1
        cfg = self._cfg()
        if not cfg.selection.enabled or self._c.status()["paused"]:
            log_event("selection_skipped", reason="off_or_paused")
            return
        if self._sa.is_open or self._res is not None:
            log_event("selection_skipped", reason="panel_open")
            return  # a panel is up: one thing at a time
        if self._typing():
            log_event("selection_skipped", reason="typing")
            return
        if length is not None and length < 2 * cfg.selection.min_words - 1:
            log_event("selection_skipped", reason="short")
            return
        ticket = self._ticket
        self._sa.gated(lambda active: self._show_mark(active, (x, y), ticket),
                       quiet=True, terminals=cfg.selection.terminals,
                       current=lambda: ticket == self._ticket)

    def on_cleared(self) -> None:
        self._ticket += 1
        self.hide()

    def on_input(self, kind: str, x: int = 0, y: int = 0) -> None:
        """A key, scroll, click or focus change while the mark (or bar) is up."""
        if self._mark is None:
            return
        if kind == "click":
            hit = getattr(self._overlay, "selmark_hit", None)
            if hit is not None and hit(x, y):
                return  # on the mark or the bar itself: its own click handler does the rest
        self.hide()

    def _typing(self) -> bool:
        t = self._typed_at
        return t is not None and (self._clock.now() - t) * 1000 < sb.TYPING_QUIET_MS

    # ------------------------------------------------------------------ the mark and the bar

    def _show_mark(self, active: WindowInfo, point: tuple[int, int], ticket: int) -> None:
        if ticket != self._ticket or self._res is not None or self._sa.is_open:
            return
        if active.pid == os.getpid():
            return  # text selected in one of Glance's own windows (a follow-up question)
        xy = sb.place_mark(*point, self._overlay.monitors())
        self._mark = _Mark(active, point, xy, sb.mark_view())
        self._more = False
        log_event("selection_mark_shown")
        self._draw()
        self._watch.arm(True)
        self._restart_idle()

    def hotkey(self) -> None:
        """Super+Alt+A (⌃⌥A): the action bar on the current selection, without the mark.
        Works with the feature switched off."""
        self.hide()
        self.close()
        self._sa.close()
        self._c.close_panel()
        self._ticket += 1
        ticket = self._ticket
        point = self._selection.pointer()
        self._sa.gated(lambda active: self._show_bar(active, point, ticket),
                       current=lambda: ticket == self._ticket)

    def _show_bar(self, active: WindowInfo, point: tuple[int, int], ticket: int) -> None:
        if ticket != self._ticket:
            return
        xy = sb.place_mark(*point, self._overlay.monitors())
        self._mark = _Mark(active, point, xy, self._bar())
        self._draw()
        self._watch.arm(True)
        self._restart_idle()

    def open_bar(self) -> None:
        """The mark was clicked, or the pointer rested on it."""
        m = self._mark
        if m is None or m.view.state == "bar":
            return
        m.view = self._bar()
        self._draw()

    def hover(self, inside: bool) -> None:
        """The pointer entered (or left) the mark or the bar: no idle hiding meanwhile; on
        the bare mark, resting there `OPEN_MS` opens the bar."""
        m = self._mark
        if m is None:
            return
        m.hovered = inside
        if inside:
            self._cancel(m, "idle")
            if m.view.state == "mark" and m.opener is None:
                m.opener = self._clock.call_later(sb.OPEN_MS, self._open_due)
        else:
            self._cancel(m, "opener")
            self._restart_idle()

    def toggle_more(self) -> None:
        m = self._mark
        if m is None:
            return
        self._more = not self._more
        m.view = self._bar()
        self._draw()

    def hide(self) -> None:
        m, self._mark = self._mark, None
        if m is None:
            return
        self._cancel(m, "idle")
        self._cancel(m, "opener")
        self._watch.arm(False)
        self._overlay.hide_selmark()

    @property
    def mark_state(self) -> str | None:
        return None if self._mark is None else self._mark.view.state

    def _bar(self) -> sb.SelectionBarView:
        tones: tuple[tuple[str, str], ...] = ()
        if self._more:
            tones = self._tones()
        return sb.bar_view(self._cfg().selection.translate_to, tones, self._more, self._hint())

    def _tones(self) -> tuple[tuple[str, str], ...]:
        from glance.backends.prompts import TONES

        m = self._mark
        if self._resolver is None or m is None:
            return ()
        try:
            resolved = self._resolver.resolve(_blank_view(m.window))
        except Exception:  # noqa: BLE001 - the tones are a nicety
            return ()
        return tuple((k, TONES[k].label) for k in resolved.profile.chips
                     if k in TONES and k != "custom")

    def _draw(self) -> None:
        m = self._mark
        if m is not None:
            self._overlay.show_selmark(m.view, *m.xy)

    def _open_due(self) -> None:
        m = self._mark
        if m is None:
            return
        m.opener = None
        if m.hovered:
            self.open_bar()

    def _restart_idle(self) -> None:
        m = self._mark
        if m is None:
            return
        self._cancel(m, "idle")
        if not m.hovered:
            m.idle = self._clock.call_later(sb.IDLE_MS, self._idle_due)

    def _idle_due(self) -> None:
        m = self._mark
        if m is not None and not m.hovered:
            m.idle = None
            self.hide()

    @staticmethod
    def _cancel(m: _Mark, attr: str) -> None:
        h = getattr(m, attr)
        if h is not None:
            h.cancel()
            setattr(m, attr, None)

    # ------------------------------------------------------------------ controller hooks

    def on_snapshot(self, snap: FieldSnapshot) -> None:
        key = (snap.field_id, text_sha256(snap.text))
        last, self._last_text = self._last_text, key
        if last is not None and last[0] == key[0] and last[1] != key[1]:
            self._typed_at = self._clock.now()  # the user is typing in that field
            self.hide()

    def on_pause_changed(self, paused: bool) -> None:
        if paused:
            self.hide()
            self.close()

    def on_capture_lost(self) -> None:
        pass

    def on_apply_result(self, r: Any) -> None:
        pass

    def on_window(self) -> None:
        """The focused window changed (Wayland's extension says so): the mark goes."""
        self.hide()

    # ------------------------------------------------------------------ actions

    def act(self, key: str) -> None:
        """A click on an action in the bar: only now is the selection read."""
        m = self._mark
        if m is None:
            return
        window, point = m.window, m.point
        self.hide()
        self._ticket += 1
        ticket = self._ticket

        def allowed(active: WindowInfo) -> None:
            if active.xid != window.xid:
                return  # another window by now: not what the user pointed at
            self._read(key, active, point, ticket)

        # Gated again: the click allows reading this selection, in this window, now.
        self._sa.gated(allowed, current=lambda: ticket == self._ticket)

    def _read(self, key: str, active: WindowInfo, point: tuple[int, int], ticket: int) -> None:
        limit = self._cfg().general.max_field_chars
        log_event("selection_action", action=key.split(":")[0])
        self._run_read(lambda: self._selection.primary_text(limit * 4),
                       lambda text: self._got(key, active, point, text, ticket))

    def _got(self, key: str, active: WindowInfo, point: tuple[int, int], text: str | None,
             ticket: int) -> None:
        if ticket != self._ticket:
            return
        if text is None or not text.strip():
            self._notifier.notify(NO_SELECTION, "")
            return
        if len(text) > self._cfg().general.max_field_chars:
            self._notifier.notify(TOO_LONG, "")
            return
        if key == "copy":
            self._overlay.copy_to_clipboard(text)
        elif key == "rewrite":
            self._sa.open_text(active, text, point)
        elif key == "translate_to":  # the languages to pick from, in the answer panel
            res = self._open_result(active, text, point, "choose", TRANSLATE_TITLE)
            res.state = "ready"
            self._render()
        elif key == "reply":
            self._overlay.ask_text(REPLY_PROMPT, lambda what, _back: self._start(
                active, text, point, "reply", what.strip() or None))
        elif key == "fix":
            self._fix(active, text, point)
        elif key.startswith("tone:"):
            self._start(active, text, point, "tone", tone=key.split(":", 1)[1])
        elif key in ("explain", "summarize", "translate"):
            target = self._cfg().selection.translate_to
            self._start(active, text, point, key,
                        f"Target language: {target}" if key == "translate" else None)
        else:
            log_event("selection_action_unknown")

    def _translate_to(self, active: WindowInfo, text: str, point: tuple[int, int],
                      lang: str) -> None:
        lang = " ".join(lang.split())[:40]
        if not lang:
            return
        if lang != self._cfg().selection.translate_to:
            self._remember("selection.translate_to", lang)  # "Translate" uses it from now on
        self._start(active, text, point, "translate", f"Target language: {lang}",
                    target=lang)

    def _fix(self, active: WindowInfo, text: str, point: tuple[int, int], *,
             private: bool = False, from_shot: bool = False) -> None:
        """AI first, as every check the user asks for is (the shielded rewrite path, minimal
        edits); the local checker's fixes when no AI answers (none connected, paused, failed,
        timed out). "No change" from the AI is trusted: no issues."""
        res = self._open_result(active, text, point, "fix", FIX_TITLE, from_shot=from_shot,
                                private=private)
        if private:  # never to an AI: the local checker alone
            from glance.daemon.rewrite import RewriteOutcome

            self._fix_locally(res, text, RewriteOutcome(None, False, "none"))
            return
        self._ask_ai(res, "fix_grammar", None, local_clean=True,
                     fallback=lambda outcome: self._fix_locally(res, text, outcome))

    def _fix_locally(self, res: _Result, text: str, outcome: Any) -> None:
        res.state, res.answer, res.note = "loading", None, None
        self._render()

        def done(fixed: str | None) -> None:
            if self._res is not res:
                return
            if fixed is not None and fixed != text:
                res.answer, res.state, res.applicable = fixed, "ready", True
                res.note = (None if outcome.reason in ("none", "limit", "battery", "private")
                            else LOCAL_ONLY)
                self._render()
                return
            self._answered(res, outcome, local_clean=fixed is not None)

        self._run_bg(lambda: self._local_fix(text), done)

    def _local_fix(self, text: str) -> str | None:
        """Every local finding's first replacement, applied (non-overlapping, left to
        right); None when the checker is not running."""
        g = self._grammar
        if g is None or not getattr(g, "alive", False):
            return None
        try:
            found = list(g.check(text))
        except Exception as e:  # noqa: BLE001 - then the AI may do it
            log_event("select_check_error", kind=type(e).__name__)
            return None
        if self._filter is not None:
            found = self._filter.apply(text, found, "")
        out, pos = [], 0
        for i in sorted(found, key=lambda i: (i.start, i.end)):
            if i.start < pos or not i.replacements:
                continue
            out += [text[pos:i.start], i.replacements[0]]
            pos = i.end
        out.append(text[pos:])
        return "".join(out)

    def _start(self, active: WindowInfo, text: str, point: tuple[int, int], action: str,
               instruction: str | None = None, *, tone: str | None = None,
               target: str | None = None, from_shot: bool = False) -> None:
        title = _title(action, tone, target or self._cfg().selection.translate_to)
        res = self._open_result(active, text, point, action, title, from_shot=from_shot)
        if tone is not None:
            res.history.append(tone)
        self._ask_ai(res, action, instruction, tone=tone)

    def _open_result(self, active: WindowInfo, text: str, point: tuple[int, int], action: str,
                     title: str, *, from_shot: bool = False, private: bool = False) -> _Result:
        prev = self._res
        if prev is not None:
            # A new answer in the same panel (a follow-up, Translate on the shot's text…):
            # it stays where it is, it does not close and open again somewhere else.
            point = prev.xy
            if prev.state == "loading" and self._supervisor is not None:
                self._supervisor.cancel_all()
            self._res = None
        else:
            self.close()
        self._sa.close()
        self._res = res = _Result(text, active, action, title, point, from_shot=from_shot,
                                  private=private)
        self._render()
        return res

    def _ask_ai(self, res: _Result, action: str, instruction: str | None, *,
                tone: str | None = None, local_clean: bool = False,
                fallback: Callable[[Any], None] | None = None) -> None:
        from glance.daemon.rewrite import RewriteOutcome, answer_text, rewrite_text
        from glance.wordlimit import word_window

        res.state, res.answer, res.note = "loading", None, None
        self._render()
        sup = self._supervisor

        def finish(outcome: Any) -> None:
            if (fallback is not None and self._res is res and outcome.text is None
                    and outcome.reason not in ("no_change", "cancelled")):
                fallback(outcome)
                return
            self._answered(res, outcome, local_clean)

        if sup is None or res.private:  # a private shot's text never goes to an AI
            finish(RewriteOutcome(None, False, "private" if res.private else "none"))
            return
        cfg = self._cfg()
        ctx = self._resolver.ctx(_blank_view(res.window),
                                 self._resolver.resolve(_blank_view(res.window)))
        text, max_words = res.text, cfg.ai.max_words
        replaces = action in sb.REPLACING or action == "fix_grammar"

        def work() -> RewriteOutcome:
            try:
                if tone is not None:
                    return rewrite_text(sup, text, tone, ctx,
                                        window=word_window(text, 0, max_words))
                return answer_text(sup, text, action, ctx, instruction, max_words=max_words,
                                   replaces=replaces)
            except Exception as e:  # noqa: BLE001 - the panel must always leave "loading"
                log_event("selection_action_error", kind=type(e).__name__)
                return RewriteOutcome(None, False, "failed")

        self._run_bg(work, finish)

    def _answered(self, res: _Result, outcome: Any, local_clean: bool = False) -> None:
        if self._res is not res:
            return
        if outcome.text is not None:
            res.answer, res.state = outcome.text, "ready"
            res.applicable = bool(outcome.applicable)
            res.note = None
        elif local_clean and outcome.reason in ("none", "no_change", "limit", "battery",
                                                "private"):
            res.answer, res.state, res.applicable = None, "ready", False
            res.note = NO_FIXES
        else:
            res.state = "failed"
            res.note = NOTES.get(outcome.reason or "failed", NOTES["failed"])
        self._render()

    # ------------------------------------------------------------------ text from a shot

    def open_text(self, text: str, region: Rect | None = None, private: bool = False) -> None:
        """Glance Shot's Extract text (`ShotController.text_panel`): the text read on this
        computer, in this result panel, with Copy, Fix grammar, Translate, Explain and Ask a
        follow-up. `private`: the region showed a password field, a denylisted app or a
        private window, so nothing of it goes to an AI (Fix grammar stays local)."""
        self.hide()
        self.close()
        self._sa.close()
        if region is not None:
            xy = (region.x, region.y + region.h)
        else:
            xy = self._selection.pointer()
        res = _Result(text, SHOT_WINDOW, "text", SHOT_TITLE, xy, answer=text, state="ready",
                      note=NOTES["private"] if private else None, from_shot=True,
                      private=private)
        self._res = res
        log_event("selection_shot_text", chars=len(text), private=private)
        self._render()

    def result_action(self, key: str) -> None:
        """One of the result panel's extra buttons (text from a shot: it acts on the text;
        "Translate to…": a language)."""
        r = self._res
        if r is not None and r.action == "choose":
            if key.startswith("lang:"):
                self._translate_to(r.window, r.text, r.xy, key[5:])
            elif key == "lang_other":
                self._overlay.ask_text(TRANSLATE_PROMPT, lambda lang, _back: self._res is r
                                       and self._translate_to(r.window, r.text, r.xy, lang))
            return
        if r is None or not r.from_shot or key not in dict(self._result_actions(r)):
            log_event("selection_action_unknown")
            return
        text = r.answer or r.text
        if key == "fix":
            self._fix(r.window, text, r.xy, private=r.private, from_shot=True)
        elif key in ("translate", "explain") and not r.private:
            target = self._cfg().selection.translate_to
            self._start(r.window, text, r.xy, key,
                        f"Target language: {target}" if key == "translate" else None,
                        from_shot=True)

    def _result_actions(self, r: _Result) -> tuple[tuple[str, str], ...]:
        if not r.from_shot or r.action != "text" or r.state != "ready" or not r.answer:
            return ()
        acts = [("fix", FIX_TITLE)]
        if not r.private and self._supervisor is not None:
            acts += [("translate", f"Translate to {self._cfg().selection.translate_to}"),
                     ("explain", "Explain")]
        return tuple(acts)

    # ------------------------------------------------------------------ result panel

    @property
    def is_open(self) -> bool:
        return self._res is not None

    def copy_result(self) -> None:
        r = self._res
        if r is None or not r.answer:
            return
        self._overlay.copy_to_clipboard(r.answer)
        self.close()

    def replace(self) -> None:
        """Type the answer over the selection, only through the verified write-back."""
        r = self._res
        if r is None or not r.answer or r.state != "ready":
            return
        edit = self._verified_edit(r) if r.applicable else None
        if edit is None:
            r.note = CANNOT_REPLACE
            self._render()
            return
        if self._c.request_apply(edit):
            self.close()
        else:
            r.note = BUSY
            self._render()

    def follow_up(self) -> None:
        r = self._res
        if r is None or r.private:
            return
        self._overlay.ask_text(FOLLOW_PROMPT, lambda q, _back: self._ask(r, q))

    def _ask(self, r: _Result, question: str) -> None:
        q = question.strip()
        if self._res is not r or not q:
            return
        r.action, r.title = "ask", ASK_TITLE
        r.applicable = False
        self._ask_ai(r, "ask", q)

    def close(self) -> None:
        r, self._res = self._res, None
        if r is not None:
            if r.state == "loading" and self._supervisor is not None:
                self._supervisor.cancel_all()
            self._overlay.hide_panel()

    def _verified_edit(self, r: _Result) -> Any:
        """The edit for the verified path: the daemon sees this very field and the selection
        occurs in it exactly once. None otherwise (then there is no "Replace selection")."""
        from glance.protocol import Edit

        cur = self._c.current
        if (cur is None or cur.gated or cur.pid != r.window.pid
                or cur.wm_class.casefold() != r.window.wm_class.casefold()
                or cur.text.count(r.text) != 1):
            return None
        start = cur.text.index(r.text)
        return Edit(start, start + len(r.text), r.answer or "")

    def _render(self) -> None:
        from glance.ui.panel_model import PanelView, ResultView

        r = self._res
        if r is None:
            return
        can_replace = (r.state == "ready" and r.applicable and bool(r.answer)
                       and self._verified_edit(r) is not None)
        choices: tuple[tuple[str, str], ...] = ()
        if r.action == "choose":
            from glance.languages import choices as languages

            current = self._cfg().selection.translate_to
            choices = (*((f"lang:{n}", n) for n in languages(current)),
                       ("lang_other", OTHER_LANGUAGE))
        view = PanelView(mode="result", result=ResultView(
            title=r.title, source="" if r.action == "text" else sb.excerpt(r.text),
            text=r.answer, state=r.state, note=r.note, can_replace=can_replace,
            can_follow=self._supervisor is not None and not r.private and not choices,
            choices=choices, chosen=f"lang:{self._cfg().selection.translate_to}"
            if choices else None,
            actions=self._result_actions(r),
            origin=None if not r.from_shot else "shot_text" if r.action == "text" else "shot"))
        self._overlay.show_panel(view, *r.xy)


TRANSLATE_PROMPT = "Translate to which language?"
TRANSLATE_TITLE = "Translate to"
OTHER_LANGUAGE = "Other…"
REPLY_PROMPT = "What should the reply say? (optional)"
FOLLOW_PROMPT = "Ask a follow-up"
FIX_TITLE = "Fix grammar"
ASK_TITLE = "Answer"
NO_FIXES = "No spelling or grammar issues found."
LOCAL_ONLY = "The AI did not answer: these are the quick local fixes."
CANNOT_REPLACE = "Glance cannot replace this selection safely. Use Copy."
BUSY = "Glance is busy with another change. Try again."
NOTES = {
    "none": "Connect an AI to use this: run `glance backend list`. Fix grammar works locally.",
    "limit": "AI paused (limit). Fix grammar works locally.",
    "battery": "AI is on demand only while on battery.",
    "no_change": "Nothing to change.",
    "cancelled": "Stopped.",
    "private": "This shot shows a private window: its text is not sent to an AI.",
    "failed": "That did not work. Try again.",
}
SHOT_TITLE = "Extracted text"  # read on this computer
SHOT_WINDOW = WindowInfo("Glance Shot", "Glance Shot", os.getpid(), 0, None)
_TITLES = {"explain": "Explain", "summarize": "Summary", "reply": "Reply",
           "fix": FIX_TITLE, "fix_grammar": FIX_TITLE, "ask": ASK_TITLE}


def _title(action: str, tone: str | None, target: str) -> str:
    if action == "translate":
        return f"Translate to {target}"
    if action == "tone" and tone is not None:
        from glance.backends.prompts import TONES

        spec = TONES.get(tone)
        return f"Rewrite: {spec.label if spec else tone}"
    return _TITLES.get(action, action.capitalize())


def _blank_view(w: WindowInfo) -> FieldSnapshot:
    """What the audience resolver gets: the window's class and title, never its text."""
    return FieldSnapshot(
        app=w.wm_class, wm_class=w.wm_class, title=w.title, url=None, role="selection",
        text="", caret=0, caret_rect=None, prev_char_rect=None, field_rect=None,
        window_rect=None, field_id="selection", capture_confidence=1.0, seq=0, pid=w.pid,
        gated=None)

