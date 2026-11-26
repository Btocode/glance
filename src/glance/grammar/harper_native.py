"""Harper in glanced's own process, through the `glance_core` Rust extension.

Same interface as `HarperClient` (harper.py), so the daemon's `SupervisedHarper` and the
controller cannot tell them apart: `start`/`stop`/`alive`/`degraded`/`check`/
`set_disabled_rules`/`reload_dictionary`. There is no child to supervise, so it never dies;
`start` only builds the engine.

The lints go through the same `harper_issues` as harper-ls diagnostics, so the issue filter,
the corrections and `safe` behave identically. The extension reports Python `str` offsets
(code points) directly, so there is no UTF-16 mapping, and none of harper-ls's: its code
actions treated a UTF-16 column as a character index, so on a line with an emoji (or any
non-BMP character) before a lint, harper-ls offered the suggestions of the wrong spot.

The personal dictionary (store.py's file) is read at start and re-read whenever the file
changes (checked by stat before each check): a word the user adds stops being flagged by
Harper itself, not only by the issue filter. harper-ls only read it at (re)start.

`start` also runs one throwaway check on a word far from every dictionary word: Harper
builds a lookup structure (~40 MB, kept for the process's life) the first time it meets one,
which stalled that first check ~1 s. Building it on the start thread instead means no check
the user waits on ever pays it.

A panic inside Harper surfaces as `glance_core.GrammarPanic`: logged, that one check
returns nothing, and the engine has already rebuilt itself for the next one.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from collections.abc import Callable
from typing import Any

from glance.grammar.issues import HarperLint, Issue, harper_issues
from glance.log import log_event

ENGINE_ENV = "GLANCE_GRAMMAR"  # "harper-ls" forces the external process, "native" requires this
NATIVE, HARPER_LS = "native", "harper-ls"
WARM_TEXT = "zqxjvkword"  # far from every dictionary word (see start)

try:  # the extension is optional: without it Glance runs harper-ls
    import glance_core as _core
except ImportError:  # pragma: no cover - depends on the install
    _core = None


def available() -> bool:
    return _core is not None


def core_version() -> str | None:
    return getattr(_core, "__version__", None) if _core is not None else None


def harper_version() -> str | None:
    return getattr(_core, "HARPER_VERSION", None) if _core is not None else None


def engine_choice(environ: dict[str, str] | None = None) -> str:
    """Which engine glanced uses: native when `glance_core` imports, unless the environment
    asks for harper-ls."""
    env = os.environ if environ is None else environ
    if env.get(ENGINE_ENV, "").strip().lower() == HARPER_LS:
        return HARPER_LS
    return NATIVE if available() else HARPER_LS


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class NativeHarperClient:
    engine = NATIVE

    def __init__(self, user_dict: Path, dialect: str = "American",
                 grammar_factory: Any = None, warm: bool = True) -> None:
        self._user_dict = user_dict
        self._dialect = dialect
        self._factory = grammar_factory
        self._grammar: Any = None
        self._disabled: set[str] = set()
        self._dict_key: tuple[int, int] | None = None
        self._warm = warm
        self._lock = threading.Lock()  # lifecycle only; the engine has its own lock
        self.degraded = False

    @property
    def alive(self) -> bool:
        return self._grammar is not None

    def start(self) -> None:
        with self._lock:
            if self._grammar is not None:
                return
            factory = self._factory or (_core.Grammar if _core is not None else None)
            if factory is None:
                raise RuntimeError("glance_core is not installed")
            key = _stat_key(self._user_dict)
            grammar = factory(
                dialect=self._dialect, disabled_rules=sorted(self._disabled),
                dictionary=str(self._user_dict) if key is not None else None)
            if self._warm:
                grammar.check(WARM_TEXT)
            self._grammar = grammar
            self._dict_key = key
            self.degraded = False

    def stop(self) -> None:
        with self._lock:
            self._grammar = None

    def check(self, text: str) -> list[Issue]:
        g = self._grammar
        if g is None:
            self.degraded = True
            return []
        try:
            self._refresh_dictionary(g)
            lints = g.check(text)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:  # noqa: BLE001 - GrammarPanic, or pyo3's PanicException
            log_event("grammar_native_error", kind=type(e).__name__)
            self.degraded = True
            return []
        self.degraded = False
        return harper_issues(
            HarperLint(d["start"], d["end"], d["rule"], d["message"], tuple(d["replacements"]))
            for d in lints)

    def set_disabled_rules(self, rules: set[str]) -> None:
        self._disabled = set(rules)
        g = self._grammar
        if g is not None:
            g.set_disabled_rules(sorted(self._disabled))

    def reload_dictionary(self) -> None:
        g = self._grammar
        if g is not None:
            self._dict_key = None
            self._refresh_dictionary(g)

    def _refresh_dictionary(self, g: Any) -> None:
        key = _stat_key(self._user_dict)
        if key == self._dict_key:
            return
        if key is None:
            g.set_words([])
        else:
            g.load_dictionary(str(self._user_dict))
        self._dict_key = key


def make_client(binary: Path, user_dict: Path, environ: dict[str, str] | None = None,
                **kwargs: Any) -> Any:
    """The grammar client glanced runs: in-process Harper when available, else harper-ls."""
    from glance.grammar.harper import HarperClient

    if engine_choice(environ) == NATIVE:
        return FallbackClient(NativeHarperClient(user_dict),
                              lambda: HarperClient(binary, user_dict, **kwargs))
    return HarperClient(binary, user_dict, **kwargs)


class FallbackClient:
    """In-process Harper, switching for good to the harper-ls process if it cannot start."""

    def __init__(self, native: Any, fallback: Callable[[], Any]) -> None:
        self._inner, self._fallback, self.engine = native, fallback, NATIVE

    def start(self) -> None:
        if self.engine == NATIVE:
            try:
                self._inner.start()
                return
            except Exception as e:  # noqa: BLE001 - any engine failure: use harper-ls instead
                log_event("grammar_native_fallback", kind=type(e).__name__)
                self._inner, self.engine = self._fallback(), HARPER_LS
        self._inner.start()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
