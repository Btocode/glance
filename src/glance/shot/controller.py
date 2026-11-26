"""Glance Shot's controller: the hotkey, the picker, the bar's actions. Main loop only; the
renderers and the slow work (OCR, AI) come in as ports, so this is tested with fakes.

The captured image lives in the session object and nowhere else: dropped when the bar
closes, written to disk only by Save, sent to an AI only by Explain / Ask after the notice.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from glance.backends.base import BackendError
from glance.backends.supervisor import AiUnavailable
from glance.log import log_event
from glance.protocol import Rect
from glance.shot import bar_model as bm
from glance.shot import ocr as ocr_mod
from glance.shot import prompts
from glance.shot.consent import ALLOW, ASK, ImageConsent
from glance.shot.geometry import size_label
from glance.shot.image import Shot
from glance.shot.privacy import private_reason
from glance.ui.ports import WindowInfo

ESCAPE = "shot_escape"  # the transient Escape grab while the bar is up
COPIED = "Copied to the clipboard"
COPY_FAILED = "Could not copy the image"
SAVE_FAILED = "Could not save the image"
READING = "Reading the text…"
FETCHING = "Getting the text reader (about 12 MB, once)…"
ASKING = "Asking {backend}…"
TEXT_FIXED = "{n} fix{es} applied (local grammar)"
NOTHING_TO_FIX = "No grammar fixes found"
FAILED = "That did not work. Try again"
UNAVAILABLE = {
    "none": "No AI is connected",
    "limit": "The AI limit is reached (daily calls or the monthly cost limit)",
    "battery": "AI is paused on battery",
    "failed": FAILED,
}
NO_VISION_NOTE = bm.NEEDS_VISION


@dataclass(frozen=True)
class PickResult:
    """A capture: the image, and the top-level windows on screen then (topmost first, with
    their frames) for the privacy check."""

    shot: Shot
    windows: tuple[WindowInfo, ...] = ()


class ShotUiPort(Protocol):
    """One platform's region picker and shot bar."""

    def pick(self, on_done: Callable[[PickResult | None], None]) -> None:
        """Dim the screen, let the user pick a region, capture it into memory; `on_done`
        once, on the main loop, with the capture or None (cancelled, or it failed)."""
        ...

    def cancel_pick(self) -> None: ...

    def show_bar(self, view: bm.ShotBarView, region: Rect) -> None: ...

    def hide_bar(self) -> None: ...

    def copy_image(self, png: bytes) -> bool:
        """Put the PNG on the clipboard (image/png); False when that failed."""
        ...


RunBg = Callable[[Callable[[], Any], Callable[[Any], None]], None]


@dataclass
class _Session:
    pick: PickResult
    private: str | None
    banner: str | None = None
    warn: bool = False
    notice: tuple[str, Callable[[], None]] | None = None  # (backend key, what to send)
    notice_label: str = ""
    result: bm.ShotResult | None = None
    busy: str | None = None
    text: str | None = None  # the OCR text, once read (Translate reuses it)
    ticket: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class ShotController:
    def __init__(self, *, ui: ShotUiPort, cfg: Callable[[], Any], supervisor: Any,
                 overlay: Any, notifier: Any, hotkeys: Any, run_bg: RunBg,
                 consent: ImageConsent, grammar: Any = None,
                 password_rect: Callable[[], Rect | None] = lambda: None,
                 ocr_engines: Callable[[str], list[Any]] = ocr_mod.engines,
                 text_panel: Callable[[str, Rect, bool], None] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        """`overlay` gives `ask_text` (Ask…) and `copy_to_clipboard` (text); `hotkeys` the
        transient Escape grab. `text_panel(text, region, private)`, when given, takes the
        extracted text instead of the bar's own result card: the selection actions' result
        panel (`SelectionActions.open_text`), placed under the region; `private` keeps it
        from any AI."""
        self._ui = ui
        self._cfg = cfg
        self._sup = supervisor
        self._overlay = overlay
        self._notifier = notifier
        self._hotkeys = hotkeys
        self._run_bg = run_bg
        self._consent = consent
        self._grammar = grammar
        self._password_rect = password_rect
        self._engines = ocr_engines
        self.text_panel = text_panel
        self._now = now
        self._sess: _Session | None = None
        self._picking = False
        self._tickets = 0

    # ------------------------------------------------------------------ entry points

    @property
    def is_open(self) -> bool:
        return self._sess is not None

    @property
    def picking(self) -> bool:
        return self._picking

    def enabled(self) -> bool:
        """The feature registry's `shot` switch (`[shot] enabled`)."""
        from glance.feature_switches import is_on

        return is_on(self._cfg(), "shot")

    def trigger(self) -> bool:
        """The hotkey, the tray's "Take a Glance Shot", `glance shot`: start the picker.
        False when Glance Shot is off (`[shot] enabled = false`)."""
        if not self.enabled():
            return False
        if self._picking:
            return True
        self.close()
        self._picking = True
        log_event("shot_pick")
        self._ui.pick(self._picked)
        return True

    def _picked(self, result: PickResult | None) -> None:
        self._picking = False
        if result is None:
            log_event("shot_cancelled")
            return
        cfg = self._cfg()
        private = private_reason(result.shot.rect, result.windows, cfg, self._password_rect())
        if private:
            log_event("shot_private", reason=private)
        self._tickets += 1
        self._sess = _Session(result, private, ticket=self._tickets,
                              banner=bm.PRIVATE if private else None, warn=bool(private))
        try:
            self._hotkeys.grab(ESCAPE, "Escape", self.close)
        except ValueError:
            pass
        log_event("shot_taken", w=result.shot.width, h=result.shot.height)
        self._render()

    def close(self) -> None:
        if self._picking:
            self._picking = False
            self._ui.cancel_pick()
        s, self._sess = self._sess, None
        if s is None:
            return
        if s.busy in ("explain", "ask", "translate", "text_translate", "text_explain"):
            self._sup.cancel_all()
        try:
            self._hotkeys.ungrab(ESCAPE)
        except Exception as e:  # noqa: BLE001 - closing must never fail
            log_event("shot_ungrab_failed", kind=type(e).__name__)
        self._ui.hide_bar()
        log_event("shot_closed")

    # ------------------------------------------------------------------ the bar

    def action(self, key: str, args: dict[str, Any] | None = None) -> None:
        """A click on the bar (or its result card, or the notice)."""
        s = self._sess
        if s is None:
            return
        handler = {
            "copy": self._copy, "save": self._save, "extract": self._extract,
            "explain": lambda: self._image_ai("explain"), "ask": lambda: self._image_ai("ask"),
            "translate": self._translate, "close": self.close,
            "text_copy": self._text_copy, "text_fix": self._text_fix,
            "text_translate": lambda: self._text_ai("text_translate"),
            "text_explain": lambda: self._text_ai("text_explain"),
            "notice_send": lambda: self._notice(False), "notice_always": lambda: self._notice(True),
            "notice_cancel": self._notice_cancel,
        }.get(key)
        if handler is None:
            return
        log_event("shot_action", key=key)
        tip = next((b.tip for b in self._buttons(s) if b.key == key and b.muted), None)
        if tip is None and s.result is not None:
            tip = next((b.tip for b in s.result.actions if b.key == key and b.muted), None)
        if tip is not None:
            self._say(tip, warn=True)
            return
        handler()

    def _say(self, banner: str | None, warn: bool = False) -> None:
        s = self._sess
        if s is not None:
            s.banner, s.warn = banner, warn
            self._render()

    def _copy(self) -> None:
        s = self._sess
        assert s is not None
        ok = self._ui.copy_image(s.pick.shot.png)
        log_event("shot_copy", ok=ok)
        self._say(COPIED if ok else COPY_FAILED, warn=not ok)

    def save_path(self) -> Path:
        """A new file in `[shot] save_dir`: "Glance Shot 2026-11-26 at 17.05.09.png"."""
        base = Path(os.path.expanduser(self._cfg().shot.save_dir or "~/Pictures/Glance"))
        stamp = time.strftime("%Y-%m-%d at %H.%M.%S", time.localtime(self._now()))
        path = base / f"Glance Shot {stamp}.png"
        n = 2
        while path.exists():
            path = base / f"Glance Shot {stamp} ({n}).png"
            n += 1
        return path

    def _save(self) -> None:
        s = self._sess
        assert s is not None
        path = self.save_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(s.pick.shot.png)
        except OSError as e:
            log_event("shot_save_failed", kind=type(e).__name__)
            self._say(SAVE_FAILED, warn=True)
            return
        log_event("shot_saved")
        home = str(Path.home())
        shown = str(path)
        if shown.startswith(home + os.sep):
            shown = "~" + shown[len(home):]
        self._say(f"Saved to {shown}")

    # ------------------------------------------------------------------ OCR

    def _ocr(self, then: Callable[[_Session, str], None], busy: str) -> None:
        s = self._sess
        assert s is not None
        if s.text is not None:
            then(s, s.text)
            return
        engines = self._engines(self._cfg().shot.ocr)
        fetching = any(getattr(e, "available", lambda: False)() and
                       getattr(e, "needs_download", lambda: False)() for e in engines[:1])
        s.busy = busy
        s.banner, s.warn = None, False
        s.result = bm.ShotResult("Text", None, "loading", note=FETCHING if fetching else READING)
        self._render()
        png = s.pick.shot.png

        def work() -> tuple[str | None, str]:
            try:
                return ocr_mod.extract_text(png, engines)
            except ocr_mod.OcrUnavailable as e:
                return None, e.reason
            except Exception as e:  # noqa: BLE001 - the card must leave "loading"
                log_event("shot_ocr_error", kind=type(e).__name__)
                return None, ocr_mod.NOTHING

        def done(out: tuple[str | None, str]) -> None:
            if self._sess is not s:
                return
            text, how = out
            s.busy = None
            if text is None:
                s.result = bm.ShotResult("Text", None, "failed", note=how)
                self._render()
                return
            log_event("shot_ocr", engine=how, chars=len(text))
            s.text = text
            then(s, text)

        self._run_bg(work, done)

    def _extract(self) -> None:
        def show(s: _Session, text: str) -> None:
            if self.text_panel is not None:
                panel, region, private = self.text_panel, s.pick.shot.rect, bool(s.private)
                self.close()
                panel(text, region, private)
                return
            s.result = bm.ShotResult("Text", text, "ready",
                                     bm.text_actions(ai=self._ai_connected(), private=bool(
                                         s.private)), note="Read on this computer")
            self._render()

        self._ocr(show, "extract")

    def _translate(self) -> None:
        self._ocr(lambda s, text: self._text_ai("text_translate", text), "translate")

    # ------------------------------------------------------------------ text actions

    def _text_copy(self) -> None:
        s = self._sess
        assert s is not None
        text = s.result.text if s.result is not None else None
        if text:
            self._overlay.copy_to_clipboard(text)
            self._say(COPIED)

    def _text_fix(self) -> None:
        s = self._sess
        assert s is not None
        r = s.result
        if r is None or not r.text or self._grammar is None or not getattr(
                self._grammar, "alive", False):
            self._say(NOTHING_TO_FIX)
            return
        text = r.text
        try:
            issues = list(self._grammar.check(text))
        except Exception as e:  # noqa: BLE001
            log_event("shot_fix_error", kind=type(e).__name__)
            issues = []
        fixed, n = apply_fixes(text, issues)
        if not n:
            self._say(NOTHING_TO_FIX)
            return
        s.result = replace(r, text=fixed)
        if s.text is not None and r.title == "Text":
            s.text = fixed
        self._say(TEXT_FIXED.format(n=n, es="" if n == 1 else "es"))

    def _text_ai(self, key: str, text: str | None = None) -> None:
        from glance.privacy.redact import shield, unshield

        s = self._sess
        assert s is not None
        if text is None:
            text = s.result.text if s.result is not None else None
        if not text:
            return
        if s.private:
            self._say(bm.PRIVATE, warn=True)
            return
        translate = key == "text_translate"
        title = "Translation" if translate else "Explanation"
        system = (prompts.translate_system(prompts.translate_target(self._cfg())) if translate
                  else prompts.EXPLAIN_TEXT)
        shielded = shield(text)
        user = prompts.text_user(shielded.text)

        def finish(raw: str) -> str:
            out = unshield(raw.strip(), shielded) if shielded.mapping else raw.strip()
            return out if out is not None else raw.strip()

        self._ask_ai(s, key, title, lambda: self._sup.answer_text(system, user), finish,
                     note=None)

    # ------------------------------------------------------------------ images to the AI

    def _vision(self) -> tuple[str, Any] | None:
        try:
            return self._sup.vision_backend()
        except Exception:  # noqa: BLE001
            return None

    def _ai_connected(self) -> bool:
        try:
            return self._sup.active() is not None
        except Exception:  # noqa: BLE001
            return False

    def _image_ai(self, key: str) -> None:
        s = self._sess
        assert s is not None
        if key == "ask":
            self._overlay.ask_text(bm.ASK_PROMPT, lambda q, _back: self._asked(s, q))
            return
        self._gate_image(s, key, prompts.EXPLAIN_IMAGE, prompts.EXPLAIN_IMAGE_USER)

    def _asked(self, s: _Session, question: str) -> None:
        q = question.strip()
        if self._sess is not s or not q:
            return
        self._gate_image(s, "ask", prompts.ASK_IMAGE, prompts.ask_user(q[:500]))

    def _gate_image(self, s: _Session, key: str, system: str, user: str) -> None:
        """`[shot] ai_images` and the one-time notice, then the call."""
        found = self._vision()
        if found is None:
            self._say(bm.NEEDS_VISION, warn=True)
            return
        name, backend = found
        bkey, label = backend.vision_key(), backend.vision_label()
        png = s.pick.shot.png

        def send() -> None:
            if self._sess is not s:
                return
            s.notice = None
            log_event("shot_image_sent", backend=name)
            self._ask_ai(s, key, "Explanation" if key == "explain" else "Answer",
                         lambda: self._sup.see(name, png, system, user), str.strip,
                         note=f"Answered by {label}")

        verdict = self._consent.decide(self._cfg().shot.ai_images, bkey)
        if verdict == ALLOW:
            send()
        elif verdict == ASK:
            s.notice = (bkey, send)
            s.notice_label = label
            s.banner = None
            self._render()
        else:
            self._say(bm.IMAGES_OFF, warn=True)

    def _notice(self, always: bool) -> None:
        s = self._sess
        if s is None or s.notice is None:
            return
        bkey, send = s.notice
        if always:
            self._consent.always_allow(bkey)
        send()

    def _notice_cancel(self) -> None:
        s = self._sess
        if s is not None and s.notice is not None:
            s.notice = None
            self._render()

    def _ask_ai(self, s: _Session, key: str, title: str, call: Callable[[], Iterator[str]],
                finish: Callable[[str], str], note: str | None) -> None:
        s.busy = key
        s.banner, s.warn = None, False
        s.result = bm.ShotResult(title, None, "loading", note="Thinking…")
        self._render()

        def work() -> tuple[str | None, str | None]:
            try:
                return finish("".join(call())), None
            except AiUnavailable as e:
                return None, UNAVAILABLE.get(e.reason, FAILED)
            except BackendError as e:
                if e.kind == "cancelled":
                    return None, None
                return None, FAILED
            except Exception as e:  # noqa: BLE001 - the card must leave "loading"
                log_event("shot_ai_error", kind=type(e).__name__)
                return None, FAILED

        def done(out: tuple[str | None, str | None]) -> None:
            if self._sess is not s or s.busy != key:
                return
            s.busy = None
            text, why = out
            if text:
                s.result = bm.ShotResult(title, text, "ready", bm.ANSWER_ACTIONS, note=note)
            else:
                s.result = bm.ShotResult(title, None, "failed", note=why or FAILED)
            self._render()

        self._run_bg(work, done)

    # ------------------------------------------------------------------ drawing

    def _buttons(self, s: _Session) -> tuple[bm.ShotButton, ...]:
        cfg = self._cfg().shot
        return bm.buttons(vision=self._vision() is not None, ai=self._ai_connected(),
                          images=cfg.ai_images, ocr=cfg.ocr != "off", private=bool(s.private),
                          busy=s.busy if s.busy in dict(bm.ACTIONS) else None)

    def view(self) -> bm.ShotBarView | None:
        s = self._sess
        if s is None:
            return None
        notice = None
        if s.notice is not None:
            notice = bm.ShotNotice(bm.notice_text(s.notice_label))
        return bm.ShotBarView(size_label(s.pick.shot.rect), self._buttons(s), s.banner, s.warn,
                              notice, s.result)

    def refresh(self) -> None:
        """Draw the bar again (the desktop switched between light and dark)."""
        self._render()

    def _render(self) -> None:
        s = self._sess
        view = self.view()
        if s is None or view is None:
            return
        self._ui.show_bar(view, s.pick.shot.rect)

    def status(self) -> dict[str, Any]:
        s = self._sess
        size = f"{s.pick.shot.width}x{s.pick.shot.height}" if s is not None else None
        return {"shot_open": self.is_open, "shot_picking": self._picking, "shot_size": size,
                "shot_result": None if s is None or s.result is None else s.result.state}


def apply_fixes(text: str, issues: list[Any]) -> tuple[str, int]:
    """Each issue's first replacement, applied from the end; overlapping ones skipped.
    Returns (text, how many were applied)."""
    chosen: list[Any] = []
    last_start = len(text) + 1
    for i in sorted(issues, key=lambda i: (i.start, i.end), reverse=True):
        reps = getattr(i, "replacements", ()) or ()
        if not reps or i.end > len(text) or i.start > i.end or i.end > last_start:
            continue
        chosen.append(i)
        last_start = i.start
    out = text
    for i in chosen:
        out = out[:i.start] + i.replacements[0] + out[i.end:]
    return out, len(chosen)
