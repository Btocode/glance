"""Test doubles shared across the suite."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from glance.backends.base import Analysis, BackendError, Ctx, Health
from glance.capture.ports import A11yTimeout, FieldRead
from glance.grammar.issues import Issue
from glance.privacy.gate import FieldMeta
from glance.protocol import ApplyRequest, Rect
from glance.ui.chip_model import ChipView
from glance.ui.hotkeys import parse_chord
from glance.ui.panel_model import PanelView
from glance.ui.placement import Anchor
from glance.ui.ports import WindowInfo
from glance.ui.strip_model import StripView
from glance.writeback.ports import ClipSnapshot, KeysAborted


class FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeClock:
    """Deterministic Clock: time moves only through advance()."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._seq = 0
        self._timers: list[tuple[float, int, Callable[[], None], FakeHandle]] = []

    def now(self) -> float:
        return self._now

    def call_later(self, ms: int, fn: Callable[[], None]) -> FakeHandle:
        h = FakeHandle()
        self._seq += 1
        self._timers.append((self._now + ms / 1000.0, self._seq, fn, h))
        return h

    def post(self, fn: Callable[[], None]) -> None:
        fn()

    def advance(self, ms: int) -> None:
        target = self._now + ms / 1000.0
        while True:
            due = sorted(
                (t for t in self._timers if t[0] <= target and not t[3].cancelled),
                key=lambda t: (t[0], t[1]),
            )
            if not due:
                break
            t = due[0]
            self._timers.remove(t)
            self._now = max(self._now, t[0])
            t[2]()
        self._timers = [t for t in self._timers if not t[3].cancelled]
        self._now = target


class FakeField:
    """A11yField double that also behaves like an editor for FakeKeys.

    `reads` counts read() calls; `raise_timeout` makes every call fail, and
    `timeout_after_reads = n` makes every call fail once n reads have succeeded.

    Editor knobs:
      mangle           the editor alters inserted text (autocorrect)
      post_edit        the editor alters the whole text after an insert
      selection_fails  set_selection returns False (set_caret still works)
      selection_lies   set_selection returns True and selects nothing
      undo_broken      Ctrl+Z does nothing
      split_undo       a typed replace is two undo steps, as in a GtkSourceView (S4.4): the
                       first typed character replaces the selection, the rest is a second
                       step
    """

    def __init__(self, key: str = "100:1:0", text: str = "", caret: int = 0,
                 meta: FieldMeta | None = None, *,
                 mangle: Callable[[str], str] | None = None,
                 post_edit: Callable[[str], str] | None = None,
                 selection_fails: bool = False, selection_lies: bool = False,
                 undo_broken: bool = False, split_undo: bool = False) -> None:
        self.mangle = mangle
        self.post_edit = post_edit
        self.selection_fails = selection_fails
        self.selection_lies = selection_lies
        self.undo_broken = undo_broken
        self.split_undo = split_undo
        self.timeout_after_reads: int | None = None
        self._undo: list[str] = []
        self.key = key
        self.text = text
        self.caret = caret
        self.selection: tuple[int, int] | None = None
        self._meta = meta or FieldMeta(
            app="gedit", wm_class="gedit", title="notes", url=None, role="text",
            states=frozenset({"editable", "focused"}), pid=100, text_len=0,
        )
        self.caret_rect: Rect | None = Rect(100, 200, 1, 18)
        self.prev_char_rect: Rect | None = None
        self.field_rect: Rect | None = Rect(90, 190, 400, 300)
        self.win_rect: Rect | None = Rect(50, 50, 800, 600)
        self.raise_timeout = False
        self.reads = 0
        self.metas = 0
        self.selections: list[tuple[int, int]] = []
        self.carets: list[int] = []
        self.accept_selection = True

    def set_meta(self, **changes: object) -> None:
        self._meta = replace(self._meta, **changes)

    def _check(self) -> None:
        after = self.timeout_after_reads
        if self.raise_timeout or (after is not None and self.reads >= after):
            raise A11yTimeout(self.key)

    def meta(self) -> FieldMeta:
        self._check()
        self.metas += 1
        return replace(self._meta, text_len=len(self.text))

    def window_rect(self) -> Rect | None:
        self._check()
        return self.win_rect

    def read(self) -> FieldRead:
        self._check()
        self.reads += 1
        return FieldRead(self.text, self.caret, self.selection, self.caret_rect,
                         self.prev_char_rect, self.field_rect, self.win_rect)

    def set_selection(self, start: int, end: int) -> bool:
        self._check()
        self.selections.append((start, end))
        if self.selection_fails:
            return False
        if self.accept_selection and not self.selection_lies:
            self.selection = (start, end)
            self.caret = end
        return self.accept_selection

    def set_caret(self, offset: int) -> bool:
        self._check()
        self.carets.append(offset)
        if self.accept_selection:
            self.caret = offset
            self.selection = None
        return self.accept_selection

    # ---------------------------------------------------------------- editor side

    def insert(self, text: str, typed: bool = False) -> None:
        """What the application does with typed or pasted text: replace the selection."""
        start, end = self.selection or (self.caret, self.caret)
        if self.mangle is not None:
            text = self.mangle(text)
        self._undo.append(self.text)
        if self.split_undo and typed and end > start and len(text) > 1:
            self._undo.append(self.text[:start] + text[0] + self.text[end:])
        self.text = self.text[:start] + text + self.text[end:]
        if self.post_edit is not None:
            self.text = self.post_edit(self.text)
        self.caret = start + len(text)
        self.selection = None

    def backspace(self) -> None:
        start, end = self.selection or (max(0, self.caret - 1), self.caret)
        self._undo.append(self.text)
        self.text = self.text[:start] + self.text[end:]
        self.caret = start
        self.selection = None

    def select_left(self) -> None:
        start, end = self.selection or (self.caret, self.caret)
        self.selection = (max(0, start - 1), end)

    def undo(self) -> None:
        if self.undo_broken or not self._undo:
            return
        self.text = self._undo.pop()
        self.selection = None
        self.caret = min(self.caret, len(self.text))


class FakeA11y:
    """A11yPort double: tests drive it with focus() and change()."""

    def __init__(self) -> None:
        self.current: FakeField | None = None
        self._on_focus: Callable[[FakeField | None], None] | None = None
        self._on_change: Callable[[FakeField], None] | None = None

    def start(self, on_focus: Callable[[FakeField | None], None],
              on_change: Callable[[FakeField], None]) -> None:
        self._on_focus = on_focus
        self._on_change = on_change

    def focused(self) -> FakeField | None:
        return self.current

    def focus(self, field: FakeField | None) -> None:
        self.current = field
        assert self._on_focus is not None, "port not started"
        self._on_focus(field)

    def change(self, field: FakeField) -> None:
        assert self._on_change is not None, "port not started"
        self._on_change(field)


class FakeClipboard:
    """ClipboardPort double. `content` is whatever the clipboard holds (text or anything else).

    Knobs: `unsaveable` (save() returns None, e.g. an image its owner will not hand over),
    `serves = False` (the target application gets nothing when it pastes).
    """

    def __init__(self, content: object = None) -> None:
        self.content = content
        self.calls: list[str] = []
        self.secret_hint_used = False
        self.unsaveable = False
        self.serves = True
        self.secret_window: int | None = None  # the only window the replacement is offered to
        self._secret: str | None = None

    def save(self) -> ClipSnapshot | None:
        self.calls.append("save")
        return None if self.unsaveable else ClipSnapshot(self.content)

    def set_secret_text(self, text: str, target_window: int) -> None:
        self.calls.append("set_secret_text")
        self.secret_hint_used = True
        self.secret_window = target_window
        self.content = self._secret = text

    def restore(self, snap: ClipSnapshot) -> None:
        self.calls.append("restore")
        self.content = snap.payload
        self._secret = None

    def paste_text(self) -> str | None:
        """What the focused application receives on Ctrl+V."""
        if self._secret is not None:
            return self._secret if self.serves else None
        return self.content if isinstance(self.content, str) else None


class FakeKeys:
    """KeyPort double: keys act on the field `target()` returns (the focused one).

    `events` is every synthesized input in order; `typed` and `chords` are the two kinds.
    `window` and `active` are the X window that has the keyboard and its pid (window 0: no
    usable target, e.g. the shell's overview has the focus).
    Knobs: `held` (a key stays down, so wait_modifiers_released fails), `down` (a key is
    down when asked without waiting), `untypeable`
    (extra characters can_type rejects; non-ASCII is always rejected), `on_wait` (called
    during the modifier wait), `abort_typing_after = n` (type n characters, call `on_abort`,
    raise KeysAborted).
    """

    def __init__(self, target: Callable[[], FakeField | None],
                 clip: FakeClipboard | None = None) -> None:
        self._target = target
        self._clip = clip
        self.events: list[tuple[str, object]] = []
        self.typed: list[str] = []
        self.chords: list[tuple[str, ...]] = []
        self.waits: list[int] = []
        self.delays: list[int] = []
        self.held = False
        self.window = 0x3A00007
        self.active = 100  # pid owning that window; FakeField's default pid
        self.down = False
        self.type_windows: list[int | None] = []
        self.untypeable = ""
        self.on_wait: Callable[[], None] | None = None
        self.abort_typing_after: int | None = None
        self.on_abort: Callable[[], None] | None = None
        self.on_chord: Callable[[tuple[str, ...]], None] | None = None  # after its effect

    def can_type(self, text: str) -> bool:
        return all(c.isascii() and c.isprintable() and c not in self.untypeable for c in text)

    def focus_target(self) -> tuple[int, int]:
        return (self.window, self.active) if self.window else (0, 0)

    def any_key_down(self) -> bool:
        return self.down

    def wait_modifiers_released(self, timeout_ms: int = 1000) -> bool:
        self.waits.append(timeout_ms)
        if self.on_wait is not None:
            self.on_wait()
        return not self.held

    def type_text(self, text: str, delay_ms: int, window: int | None = None) -> None:
        self.type_windows.append(window)
        self.events.append(("type", text))
        self.typed.append(text)
        self.delays.append(delay_ms)
        field = self._target()
        if self.abort_typing_after is not None:
            if field is not None and self.abort_typing_after > 0:
                field.insert(text[: self.abort_typing_after], typed=True)
            if self.on_abort is not None:
                self.on_abort()
            raise KeysAborted("active window changed")
        if field is not None:
            field.insert(text, typed=True)

    def chord(self, *keys: str) -> None:
        self.events.append(("chord", keys))
        self.chords.append(keys)
        field = self._target()
        if field is None:
            if self.on_chord is not None:
                self.on_chord(keys)
            return
        if keys == ("ctrl", "v"):
            text = self._clip.paste_text() if self._clip is not None else None
            if text is not None:
                field.insert(text)
        elif keys == ("ctrl", "z"):
            field.undo()
        elif keys == ("shift", "Left"):
            field.select_left()
        elif keys == ("BackSpace",):
            field.backspace()
        if self.on_chord is not None:
            self.on_chord(keys)


# ---------------------------------------------------------------- daemon-side ports


class FakeOverlay:
    """OverlayPort double: `chip` / `panel` are what is on screen now (None when hidden)."""

    def __init__(self, monitors: list[Rect] | None = None) -> None:
        self.chip: ChipView | None = None
        self.chip_xy: tuple[int, int] | None = None
        self.panel: PanelView | None = None
        self.panel_xy: tuple[int, int] | None = None
        self.panel_anchor: Anchor | None = None
        self.strip: StripView | None = None
        self.strip_anchor: Anchor | None = None
        self.monitor_list = monitors if monitors is not None else [Rect(0, 0, 1920, 1080)]
        self.clipboard: list[str] = []
        self.asked: list[tuple[str, Callable[[str, bool], None]]] = []

    def show_chip(self, view: ChipView, x: int, y: int) -> None:
        self.chip, self.chip_xy = view, (x, y)

    def hide_chip(self) -> None:
        self.chip = self.chip_xy = None

    def show_panel(self, view: PanelView, x: int, y: int, anchor: Anchor | None = None) -> None:
        self.panel, self.panel_xy, self.panel_anchor = view, (x, y), anchor

    def hide_panel(self) -> None:
        self.panel = self.panel_xy = self.panel_anchor = None

    def show_strip(self, view: StripView, anchor: Anchor) -> None:
        self.strip, self.strip_anchor = view, anchor

    def hide_strip(self) -> None:
        self.strip = self.strip_anchor = None

    def monitors(self) -> list[Rect]:
        return list(self.monitor_list)

    def copy_to_clipboard(self, text: str) -> None:
        self.clipboard.append(text)

    def ask_text(self, prompt: str, on_done: Callable[[str, bool], None]) -> None:
        """Records the question; the test answers with `asked[-1][1](text, focus_returned)`."""
        self.asked.append((prompt, on_done))


class FakeCapture:
    """CapturePort double: records what the daemon sends to glance-capture."""

    def __init__(self) -> None:
        self.connected = True
        self.applies: list[ApplyRequest] = []
        self.reverts = 0
        self.pastes: list[str] = []
        self.paste_targets: list[tuple[int, str]] = []
        self.resets = 0

    def send_apply(self, req: ApplyRequest) -> None:
        self.applies.append(req)

    def send_revert(self) -> None:
        self.reverts += 1

    def send_paste(self, text: str, window: int = 0, wm_class: str = "") -> None:
        self.pastes.append(text)
        self.paste_targets.append((window, wm_class))

    def send_reset(self) -> None:
        self.resets += 1


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, summary: str, body: str) -> None:
        self.calls.append((summary, body))


class FakeTray:
    def __init__(self) -> None:
        self.state: str | None = None
        self.states: list[str] = []

    def set_state(self, state: str) -> None:
        self.state = state
        self.states.append(state)


class FakeHarper:
    """GrammarPort double: one issue per occurrence of each needle in `patterns`.

    `patterns` maps a needle to `(rule, replacements)`. As with the real client, an issue is
    `safe` when it has exactly one replacement and is not a spelling suggestion. `calls` is
    every text checked.
    """

    def __init__(self, patterns: dict[str, tuple[str, tuple[str, ...]]] | None = None) -> None:
        self.patterns = dict(patterns or {})
        self.alive = True
        self.calls: list[str] = []

    def check(self, text: str) -> list[Issue]:
        self.calls.append(text)
        out: list[Issue] = []
        for needle, (rule, replacements) in self.patterns.items():
            start = text.find(needle)
            while start != -1:
                end = start + len(needle)
                out.append(Issue(f"harper:{rule}:{start}:{end}", "harper", rule,
                                 f"{rule} problem", start, end, tuple(replacements),
                                 len(replacements) == 1 and rule != "SpellCheck"))
                start = text.find(needle, end)
        return sorted(out, key=lambda i: i.start)


# ---------------------------------------------------------------- AI backend


@dataclass(frozen=True)
class FakeCall:
    kind: str  # analyze | rewrite | complete
    text: str
    ctx: Ctx
    tone: str | None = None
    custom: str | None = None


_CANCEL = object()


class FakeBackend:
    """Backend double: scripted results, simulated delays and a call log.

    `analysis` / `deltas` answer every call until `script()` queues one-off results; a queued
    exception is raised instead of returned. `delay_ms` is how long each call claims to take:
    nothing sleeps, the time is added to `elapsed_ms` for `FakeBg` to turn into a late
    delivery. `cancelled` counts `cancel()` calls; `health_value` is what `health()` reports.

    With `hold = True` a rewrite stays open, as a slow backend's would, until the test calls
    `finish(deltas)` (or `finish(an_exception)`); `cancel()` ends it with a `cancelled`
    BackendError unless `ignore_cancel` is set (a backend whose result beat the cancel).
    Held calls need a `FakeBg` built with this backend: it runs the work on a thread.
    """

    def __init__(self, name: str = "claude", analysis: Analysis | None = None,
                 deltas: tuple[str, ...] = ("Thanks so much, will do!",),
                 delay_ms: int = 0) -> None:
        self.name = name
        self.analysis = analysis or Analysis("neutral", False, None, ())
        self.deltas = deltas
        self.delay_ms = delay_ms
        self.elapsed_ms = 0
        self.health_value = Health(True, True, "fake")
        self.calls: list[FakeCall] = []
        self.cancelled = 0
        self.hold = False
        self.ignore_cancel = False
        self.bg: FakeBg | None = None
        self._held: list[queue.Queue[Any]] = []
        self._script: list[Any] = []
        self.completion: tuple[str, ...] = ("tomorrow morning.",)
        self._completions: list[Any] = []

    def script(self, *results: Any) -> None:
        self._script += results

    def _next(self, default: Any) -> Any:
        self.elapsed_ms += self.delay_ms
        result = self._script.pop(0) if self._script else default
        if isinstance(result, BaseException):
            raise result
        return result

    def analyze(self, text: str, ctx: Ctx) -> Analysis:
        self.calls.append(FakeCall("analyze", text, ctx))
        return self._next(self.analysis)

    def rewrite(self, text: str, tone: str, ctx: Ctx, custom: str | None = None) -> Iterator[str]:
        self.calls.append(FakeCall("rewrite", text, ctx, tone, custom))
        if not self.hold:
            yield from self._next(self.deltas)
            return
        box: queue.Queue[Any] = queue.Queue()
        self._held.append(box)
        result = box.get(timeout=10)
        if result is _CANCEL:
            raise BackendError("cancelled")
        if isinstance(result, BaseException):
            raise result
        yield from result

    @property
    def holding(self) -> int:
        """How many rewrites are open, waiting for `finish()`."""
        return len(self._held)

    def finish(self, deltas: Any) -> None:
        """End the oldest held rewrite with these deltas, then deliver what that completes."""
        assert self._held, "no rewrite is being held"
        self._held.pop(0).put(deltas)
        if self.bg is not None:
            self.bg.settle()

    def complete(self, prefix: str, ctx: Ctx) -> Iterator[str]:
        """`completion`, or what `finish_complete()` queued; `script()` results come first."""
        self.calls.append(FakeCall("complete", prefix, ctx))
        default = self._completions.pop(0) if self._completions else self.completion
        yield from self._next(default)

    def finish_complete(self, deltas: Any) -> None:
        """The next `complete()` call ends with these deltas."""
        self._completions.append(tuple(deltas))

    def cancel(self) -> None:
        self.cancelled += 1
        if not self.ignore_cancel:
            while self._held:
                self._held.pop(0).put(_CANCEL)

    def health(self) -> Health:
        return self.health_value


class FakeBg:
    """`run_bg` double: the work runs at once, on the caller's thread.

    Its result is handed to `done` at once too, unless one of `backends` says the work took
    time (`elapsed_ms` grew): then `done` runs that much later on the clock.

    When one of `backends` holds its rewrites (`hold`), every job runs on a thread instead and
    the call returns once all jobs have finished or are held; `settle()` (called by
    `FakeBackend.finish`) waits for that state again and delivers finished results in order,
    on the caller's thread.
    """

    def __init__(self, clock: FakeClock, *backends: FakeBackend) -> None:
        self._clock = clock
        self._backends = backends
        self._jobs: list[tuple[threading.Thread, dict[str, Any], Callable[[Any], None]]] = []
        for b in backends:
            b.bg = self

    def _elapsed(self) -> int:
        return sum(b.elapsed_ms for b in self._backends)

    def __call__(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        if any(b.hold for b in self._backends):
            box: dict[str, Any] = {}
            thread = threading.Thread(target=lambda: box.update(result=work()), daemon=True)
            self._jobs.append((thread, box, done))
            thread.start()
            self.settle()
            return
        before = self._elapsed()
        result = work()
        took = self._elapsed() - before
        if took:
            self._clock.call_later(took, lambda: done(result))
        else:
            done(result)

    def settle(self, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            finished = next((j for j in self._jobs if not j[0].is_alive()), None)
            if finished is not None:
                self._jobs.remove(finished)
                finished[2](finished[1]["result"])  # may start further jobs
                continue
            if len(self._jobs) <= sum(b.holding for b in self._backends):
                return
            assert time.monotonic() < deadline, "background work neither finished nor held"
            time.sleep(0.001)


class HeldBg:
    """`run_bg` double that starts nothing: jobs wait until `run()`, like a slow backend."""

    def __init__(self) -> None:
        self._jobs: list[tuple[Callable[[], Any], Callable[[Any], None]]] = []

    @property
    def pending(self) -> int:
        return len(self._jobs)

    def __call__(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        self._jobs.append((work, done))

    def run(self) -> None:
        while self._jobs:
            work, done = self._jobs.pop(0)
            done(work())


# ---------------------------------------------------------------- autocomplete ports


@dataclass(frozen=True)
class GhostShown:
    text: str
    x: int
    y: int
    font_px: int
    max_width: int


class FakeGhost:
    """GhostPort double: `shown` is what is on screen now (None when hidden)."""

    def __init__(self) -> None:
        self.shown: GhostShown | None = None
        self.hides = 0

    def show(self, text: str, x: int, y: int, font_px: int, max_width: int) -> None:
        self.shown = GhostShown(text, x, y, font_px, max_width)

    def hide(self) -> None:
        self.shown = None
        self.hides += 1


class FakeHotkeys:
    """HotkeyPort double: `taken` chords fail like a BadAccess from the X server.

    `press(name)` is the user pressing the chord grabbed under `name`; `log` is every grab and
    ungrab in order.
    """

    def __init__(self, taken: set[str] = frozenset()) -> None:
        self.taken = set(taken)
        self.grabs: dict[str, tuple[str, Callable[[], None]]] = {}
        self.log: list[tuple[str, str]] = []

    def grab(self, name: str, chord: str, cb: Callable[[], None]) -> bool:
        parse_chord(chord)
        self.log.append(("grab", name))
        if chord in self.taken:
            return False
        self.grabs[name] = (chord, cb)
        return True

    def ungrab(self, name: str) -> None:
        self.log.append(("ungrab", name))
        self.grabs.pop(name, None)

    def grabbed(self) -> dict[str, str]:
        return {name: chord for name, (chord, _cb) in self.grabs.items()}

    def press(self, name: str) -> None:
        self.grabs[name][1]()


def win(wm_class: str, xid: int = 1, title: str = "", pid: int = 100) -> WindowInfo:
    """A top-level window as the selection port reports it."""
    return WindowInfo(wm_class, title or wm_class, pid, xid, None)


class FakeSelection:
    """SelectionPort double: `set()` says what PRIMARY holds, who owns it and what is active."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.owner_win: WindowInfo | None = None
        self.active_win: WindowInfo | None = None
        self.xy = (300, 200)
        self.reads: list[int] = []

    def set(self, text: str | None, owner: WindowInfo | None,
            active: WindowInfo | None = None) -> None:
        self.text, self.owner_win = text, owner
        self.active_win = active if active is not None else owner

    def primary_text(self, max_bytes: int) -> str | None:
        self.reads.append(max_bytes)
        return self.text

    def owner(self) -> WindowInfo | None:
        return self.owner_win

    def active(self) -> WindowInfo | None:
        return self.active_win

    def pointer(self) -> tuple[int, int]:
        return self.xy
