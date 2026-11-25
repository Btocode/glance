"""Backend contract: context, analysis types, errors, and the shared analyze/rewrite flow."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from glance.log import log_event

ERROR_KINDS = ("timeout", "auth", "rate_limit", "bad_output", "unavailable", "cancelled")
ANALYZE_TIMEOUT_S = 8.0
REWRITE_TIMEOUT_S = 20.0
COMPLETE_TIMEOUT_S = 8.0

# The one sign-in table (spec 5.2): onboarding, `glance backend list`, doctor and the chip all
# read it. CONNECT is the command to copy; SIGN_IN is the same thing as a sentence.
CONNECT: dict[str, str] = {
    "claude": "claude",
    "codex": "codex login",
    "gemini": "gemini",
    "opencode": "opencode auth login",
    "api": "glance backend set-key",
}
SIGN_IN: dict[str, str] = {
    "claude": "run `claude` and log in",
    "codex": "run `codex login`",
    "gemini": "run `gemini` to authenticate (or use an API key)",
    "opencode": "run `opencode auth login`",
    "api": "run `glance backend set-key`",
}


@dataclass(frozen=True)
class Ctx:
    profile: str
    target_tone: str
    strictness: str
    app_label: str
    mixed_language: bool = False


@dataclass(frozen=True)
class AnalysisIssue:
    quote: str
    occurrence: int  # 1-based
    message: str
    replacement: str | None
    kind: str  # clarity | grammar | tone


@dataclass(frozen=True)
class Analysis:
    tone: str
    mismatch: bool
    reason: str | None
    issues: tuple[AnalysisIssue, ...]
    flags: tuple[str, ...] = ()  # subset of hostile, missing_attachment, missing_link


@dataclass(frozen=True)
class Health:
    installed: bool
    signed_in: bool | None
    detail: str


UNKNOWN_MODEL = "unknown_model"  # BackendError.detail when the CLI rejected the model name
_UNKNOWN_MODEL = re.compile(
    r"model_not_found|ModelNotFound|unknown model|invalid model|"
    r"model\b.{0,60}(?:not found|does not exist|not supported|unsupported|not available|"
    r"is invalid|unknown)|(?:not found|invalid|unknown|unsupported).{0,30}\bmodel\b",
    re.IGNORECASE | re.DOTALL)


def error_detail(text: str, default: str) -> str:
    """`default`, or UNKNOWN_MODEL when the CLI's error text says the model name is wrong."""
    return UNKNOWN_MODEL if _UNKNOWN_MODEL.search(text or "") else default


class BackendError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown backend error kind: {kind}")
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


def extract_json_object(text: str) -> dict:
    """First JSON object found in `text` (tolerates prose and code fences around it)."""
    dec = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = dec.raw_decode(text, pos)
        except ValueError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            return obj
        pos = text.find("{", pos + 1)
    raise BackendError("bad_output", "no json object")


class Backend(Protocol):
    name: str

    def analyze(self, text: str, ctx: Ctx) -> Analysis: ...

    def rewrite(self, text: str, tone: str, ctx: Ctx, custom: str | None = None) -> Iterator[str]: ...

    def complete(self, prefix: str, ctx: Ctx) -> Iterator[str]: ...

    def cancel(self) -> None: ...

    def health(self) -> Health: ...


class BaseBackend:
    """Subclasses supply `_run_json` and `_run_stream`; prompts and validation live here."""

    name = "base"
    model = ""  # "" runs the CLI's own default (no -m)
    _asked_model = ""  # what the config last asked for; see set_model

    def set_model(self, model: str) -> None:
        """Follow the configured model. A no-op while the config is unchanged, so a model the
        CLI rejected (and `_drop_model` cleared) is not tried again on every call."""
        if model != self._asked_model:
            self._asked_model, self.model = model, model

    def _drop_model(self, e: BackendError) -> bool:
        """The CLI rejected the model name: fall back to its own default, once. Only the error
        type is logged, never the model name or any text."""
        if e.detail != UNKNOWN_MODEL or not self.model:
            return False
        log_event("model_rejected", backend=self.name)
        self.model = ""
        return True

    def _json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        try:
            return self._run_json(system, user, schema, timeout_s)
        except BackendError as e:
            if not self._drop_model(e):
                raise
        return self._run_json(system, user, schema, timeout_s)

    def _stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        got = False
        try:
            for delta in self._run_stream(system, user, timeout_s):
                got = True
                yield delta
            return
        except BackendError as e:
            if got or not self._drop_model(e):
                raise
        yield from self._run_stream(system, user, timeout_s)

    def _run_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        raise NotImplementedError

    def _run_stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        raise NotImplementedError

    def analyze(self, text: str, ctx: Ctx) -> Analysis:
        from glance.backends.prompts import (
            ANALYZE_SCHEMA,
            REPAIR_SUFFIX,
            analyze_system,
            parse_analysis,
            user_block,
        )

        system, user = analyze_system(ctx), user_block(text)
        try:
            return parse_analysis(self._json(system, user, ANALYZE_SCHEMA, ANALYZE_TIMEOUT_S))
        except BackendError as e:
            if e.kind != "bad_output":
                raise
        return parse_analysis(
            self._json(system + REPAIR_SUFFIX, user, ANALYZE_SCHEMA, ANALYZE_TIMEOUT_S)
        )

    def rewrite(self, text: str, tone: str, ctx: Ctx, custom: str | None = None) -> Iterator[str]:
        from glance.backends.prompts import rewrite_system, user_block

        instruction = custom if tone == "custom" else None  # typed text: stdin/body only
        yield from self._stream(
            rewrite_system(tone, ctx), user_block(text, instruction), REWRITE_TIMEOUT_S
        )

    def complete(self, prefix: str, ctx: Ctx) -> Iterator[str]:
        from glance.backends.prompts import complete_system, user_block

        yield from self._stream(complete_system(ctx), user_block(prefix), COMPLETE_TIMEOUT_S)
