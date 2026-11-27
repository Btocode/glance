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
from glance.ui.ports import WindowInfo

ESCAPE = "shot_escape"  # the transient Escape grab while the bar is up
COPIED = "Image copied to the clipboard"
COPIED_TEXT = "Text copied to the clipboard"
MARKED = "Marked up: Copy, Save and the AI get the marked shot"
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
    results: dict[str, bm.ShotResult] = field(default_factory=dict)  # per tab
    shown: str | None = None  # the tab whose result is in the body
    busy: str | None = None
    text: str | None = None  # the OCR text, once read (Translate and Explain reuse it)
    ticket: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    asking: bool = False  # the body is an entry (renderers with `inline_ask`)
    asking_for: str = "ask"  # what the entry is for: "ask" (a question) or "language"
    marking: bool = False  # the region is being marked up (`annotate`)

    @property
    def result(self) -> bm.ShotResult | None:
        return self.results.get(self.shown) if self.shown is not None else None

    def put(self, tab: str, result: bm.ShotResult) -> None:
        """`result` is the tab's now, and on show."""
        self.results[tab] = result
        self.shown = tab


_TAB = {"text_translate": "translate", "text_explain": "explain", "text_exact": "extract"}


class ShotController:
    def __init__(self, *, ui: ShotUiPort, cfg: Callable[[], Any], supervisor: Any,
                 overlay: Any, notifier: Any, hotkeys: Any, run_bg: RunBg,
                 consent: ImageConsent, grammar: Any = None,
                 password_rect: Callable[[], Rect | None] = lambda: None,
                 ocr_engines: Callable[[str], list[Any]] = ocr_mod.engines,
                 text_panel: Callable[[str, Rect, bool], None] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        """`overlay` gives `ask_text` (Ask…) and `copy_to_clipboard` (text); `hotkeys` the
        transient Escape grab. `text_panel` is no longer used: the text read from a shot stays
        in the shot's own card (kept so older wiring still works)."""
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
        self._lang: str | None = None  # the language Translate used last (this run)
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
        # A shot is the user's own deliberate capture: nothing in it is off limits. It can be
        # copied, saved, read and (on an explicit Explain / Ask click) sent to the AI.
        private = None
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
        """A click on the card: a tab, a header tool, a result action or the notice."""
        s = self._sess
        if s is None:
            return
        if key in dict(bm.ACTIONS) and key != "ask" and key in s.results \
                and s.results[key].state != "failed" and s.busy != key:
            s.shown, s.notice, s.asking = key, None, False  # a tab already done: its result
            s.banner, s.warn = None, False
            log_event("shot_action", key=key)
            self._render()
            return
        if key.startswith("lang:"):
            log_event("shot_action", key="lang")
            self._translate_to(key[5:])
            return
        handler = {
            "copy": self._copy, "save": self._save, "extract": self._extract,
            "explain": self._explain, "ask": lambda: self._image_ai("ask"),
            "translate": self._choose_language, "close": self.close,
            "lang_other": self._other_language, "translate_lang": self._choose_language,
            "ask_submit": lambda: self._ask_submit((args or {}).get("text", "")),
            "ask_cancel": self._ask_cancel, "mark": self._mark,
            "text_exact": self._exact_text,
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
        s.banner, s.warn, s.notice = None, False, None
        title = {"translate": "Translation", "explain": "Explanation"}.get(busy, "Text")
        s.put(busy, bm.ShotResult(title, None, "loading", note=FETCHING if fetching else READING))
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
            if s.busy == busy:
                s.busy = None
            if text is None:
                s.put(busy, bm.ShotResult(title, None, "failed", note=how))
                self._render()
                return
            log_event("shot_ocr", engine=how, chars=len(text))
            s.text = text
            then(s, text)

        self._run_bg(work, done)

    def _extract(self) -> None:
        def show(s: _Session, text: str) -> None:
            s.put("extract", bm.ShotResult(
                "Text", text, "ready", bm.text_actions(
                    private=bool(s.private), vision=self._vision() is not None
                    and self._cfg().shot.ai_images != "never"),
                note="Read on this computer"))
            self._render()

        self._ocr(show, "extract")

    def _explain(self) -> None:
        """The image to a vision AI; without one (or with images off), the text read on this
        computer to any connected AI."""
        s = self._sess
        assert s is not None
        if self._vision() is not None and self._cfg().shot.ai_images != "never":
            self._image_ai("explain")
            return
        self._ocr(lambda _s, text: self._text_ai("text_explain", text), "explain")

    # ------------------------------------------------------------------ Translate

    def target(self) -> str:
        """The language Translate offers first: the last one used, else `[selection]
        translate_to`."""
        return self._lang or prompts.translate_target(self._cfg())

    def _choose_language(self) -> None:
        """Translate: the body shows the languages to pick from (nothing is sent yet)."""
        s = self._sess
        assert s is not None
        s.notice, s.asking = None, False
        s.banner, s.warn = None, False
        s.put("translate", bm.ShotResult(bm.TRANSLATE_TO, None, "choose",
                                         choices=bm.languages(self.target())))
        self._render()

    def _other_language(self) -> None:
        s = self._sess
        assert s is not None
        if getattr(self._ui, "inline_ask", False):
            s.asking, s.asking_for, s.notice = True, "language", None  # typed in the card
            s.banner, s.warn = None, False
            s.shown = "translate"
            self._render()
            return
        at = getattr(self._ui, "ask_point", lambda: None)()
        self._overlay.ask_text(bm.OTHER_PROMPT,
                               lambda lang, _back: self._sess is s and self._translate_to(lang),
                               at=at)

    def _translate_to(self, lang: str) -> None:
        lang = " ".join(lang.split())[:40]
        if not lang or self._sess is None:
            return
        self._lang = lang
        self._ocr(lambda _s, text: self._text_ai("text_translate", text, target=lang),
                  "translate")

    # ------------------------------------------------------------------ Mark

    def _mark(self) -> None:
        """Draw on the shot (a pen, boxes, arrows, a highlighter) before it is copied, saved
        or sent: the renderer's `annotate`, which gives back the marked-up PNG (or None)."""
        s = self._sess
        assert s is not None
        annotate = getattr(self._ui, "annotate", None)
        if annotate is None:
            self._say(bm.MARK_UNAVAILABLE, warn=True)
            return
        s.marking = True
        self._render()

        def done(png: bytes | None) -> None:
            if self._sess is not s:
                return
            s.marking = False
            if png:
                s.pick = replace(s.pick, shot=replace(s.pick.shot, png=png))
                # Answers about the old image go; the text read from it stays (marks add none).
                for k in ("explain", "ask"):
                    s.results.pop(k, None)
                if s.shown in ("explain", "ask"):
                    s.shown = None
                log_event("shot_marked")
                self._say(MARKED)
            else:
                self._render()

        annotate(s.pick.shot, done)

    # ------------------------------------------------------------------ text actions

    def _text_copy(self) -> None:
        s = self._sess
        assert s is not None
        text = s.result.text if s.result is not None else None
        if text:
            self._overlay.copy_to_clipboard(text)
            self._say(COPIED_TEXT)

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
        s.results[s.shown or "extract"] = replace(r, text=fixed)
        if s.text is not None and r.title == "Text":
            s.text = fixed
            for k in ("translate", "explain"):  # made from the old text: made again on a click
                if k in s.results and s.results[k].note != NO_VISION_NOTE:
                    s.results.pop(k, None)
        self._say(TEXT_FIXED.format(n=n, es="" if n == 1 else "es"))

    def _text_ai(self, key: str, text: str | None = None, target: str | None = None) -> None:
        from glance.privacy.redact import shield, unshield

        s = self._sess
        assert s is not None
        if text is None:
            text = s.text if s.text is not None else (
                s.result.text if s.result is not None else None)
        if not text:
            return
        if s.private:
            self._say(bm.PRIVATE, warn=True)
            return
        translate = key == "text_translate"
        title = "Translation" if translate else "Explanation"
        system = (prompts.translate_system(target or self.target()) if translate
                  else prompts.EXPLAIN_TEXT)
        shielded = shield(text)
        user = prompts.text_user(shielded.text)

        def finish(raw: str) -> str:
            out = unshield(raw.strip(), shielded) if shielded.mapping else raw.strip()
            return out if out is not None else raw.strip()

        self._ask_ai(s, key, title, lambda: self._sup.answer_text(system, user), finish,
                     note="From the text read on this computer" if key == "text_explain"
                     else f"Into {target or self.target()}" if translate else None,
                     actions=bm.TRANSLATION_ACTIONS if translate else bm.ANSWER_ACTIONS)

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
            if getattr(self._ui, "inline_ask", False):
                if s.asking and s.asking_for == "ask":
                    return  # already open: rebuilding it would only lose what is typed
                s.asking, s.asking_for, s.notice = True, "ask", None  # the question is typed in the card itself
                s.banner, s.warn = None, False
                s.shown = "ask"
                self._render()
                return
            at = getattr(self._ui, "ask_point", lambda: None)()
            self._overlay.ask_text(bm.ASK_PROMPT, lambda q, _back: self._asked(s, q), at=at)
            return
        self._gate_image(s, key, prompts.EXPLAIN_IMAGE, prompts.EXPLAIN_IMAGE_USER)

    def _ask_submit(self, question: str) -> None:
        s = self._sess
        if s is None or not s.asking:
            return
        if not question.strip():
            self._ask_cancel()
            return
        s.asking = False
        if s.asking_for == "language":
            self._translate_to(question)
            return
        self._asked(s, question)

    def _ask_cancel(self) -> None:
        s = self._sess
        if s is None:
            return
        s.asking = False
        if s.asking_for == "language":
            s.shown = "translate"  # back to the language chips
        elif s.shown == "ask" and "ask" not in s.results:
            s.shown = None
        self._render()

    def _exact_text(self) -> None:
        """Text, word for word: the image to a vision AI (the notice first, as Explain)."""
        s = self._sess
        assert s is not None
        self._gate_image(s, "text_exact", prompts.EXACT_TEXT_IMAGE, prompts.EXACT_TEXT_USER)

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
            if key == "text_exact":
                self._ask_ai(s, key, "Text", lambda: self._sup.see(name, png, system, user),
                             lambda raw: raw.strip("\n"), note=bm.EXACT_NOTE.format(
                                 backend=label), actions=bm.text_actions(vision=True))
                return
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
            s.shown = _TAB.get(key, key)  # the tab the notice is for
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
                finish: Callable[[str], str], note: str | None,
                actions: tuple[bm.ShotButton, ...] = bm.ANSWER_ACTIONS) -> None:
        tab = _TAB.get(key, key)
        s.busy = key
        s.banner, s.warn, s.notice = None, False, None
        s.put(tab, bm.ShotResult(title, None, "loading", note="Thinking…"))
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
            if self._sess is not s:
                return
            if s.busy == key:
                s.busy = None
            text, why = out
            if text:
                s.results[tab] = bm.ShotResult(title, text, "ready", actions, note=note)
                if key == "text_exact":  # the exact text is the text from now on
                    s.text = text
                    for k in ("translate", "explain"):
                        if k in s.results and s.results[k].note != NO_VISION_NOTE:
                            s.results.pop(k, None)
            else:
                s.results[tab] = bm.ShotResult(title, None, "failed", note=why or FAILED)
            self._render()

        self._run_bg(work, done)

    # ------------------------------------------------------------------ drawing

    def _buttons(self, s: _Session) -> tuple[bm.ShotButton, ...]:
        cfg = self._cfg().shot
        busy = _TAB.get(s.busy, s.busy) if s.busy else None
        return bm.buttons(vision=self._vision() is not None, ai=self._ai_connected(),
                          images=cfg.ai_images, ocr=cfg.ocr != "off", private=bool(s.private),
                          busy=busy if busy in dict(bm.ACTIONS) else None,
                          shown=s.shown)

    def view(self) -> bm.ShotBarView | None:
        s = self._sess
        if s is None:
            return None
        notice = None
        if s.notice is not None:
            notice = bm.ShotNotice(bm.notice_text(s.notice_label))
        result = None if notice is not None or s.asking else s.result
        tools = tuple(replace(t, selected=t.key == "mark" and s.marking)
                      for t in bm.tools())
        words = ((bm.OTHER_PROMPT, bm.OTHER_PLACEHOLDER, "translate")
                 if s.asking_for == "language" else
                 (bm.ASK_PROMPT, bm.ASK_PLACEHOLDER, "ask"))
        return bm.ShotBarView(size_label(s.pick.shot.rect), self._buttons(s), s.banner, s.warn,
                              notice, result, tools=tools,
                              expanded=notice is not None or result is not None or s.asking,
                              asking=s.asking, ask_prompt=words[0], ask_placeholder=words[1],
                              ask_go=words[2])

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
