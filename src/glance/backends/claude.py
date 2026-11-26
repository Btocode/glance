"""claude CLI backend: analyze and rewrite both stream (`-p --output-format stream-json`).

Analyze does not use `--json-schema`: the CLI implements it as a tool call the model must
make, then a second turn after the tool result, which doubled the call's time (measured on
haiku: 8.6 s cold with the schema, 4.1 s without; the old 8 s budget timed out). The prompt
spells out the JSON instead and `parse_analysis` checks it, with one repair call when it is
off.

Speed (measured on haiku, CLI 2.1.332, from the request written to the answer):
- `--effort low`: the CLI's default effort has the model think first (a thinking block of
  3-4.6 s, 4.5-6.2 s to the first text); at low effort it does not (first text ~1.0 s, the
  whole answer ~1.4 s). Every call passes it; a CLI too old to know the flag gets the call
  again without it.
- Analyze streams: the "corrected" text is reported as it arrives (`partial`), and the call
  returns on the `result` line, not when the process exits (it lingers 0.5-1 s after).
- A spare analyze process (`prewarm("analyze", system)`): the exact argv of the next analyze,
  started ahead (on field focus) and waiting on stdin, so start-up is already paid. The CLI
  gives up on a stdin that stays silent for 3 s, but waits for the end once one byte came:
  the spare is sent "<" at once (every request starts with "<glance_text>" or another tag)
  and the rest when the check comes. Nothing of the user's text goes to it before then.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

from glance.backends.base import (
    VISION_TIMEOUT_S,
    BackendError,
    BaseBackend,
    Ctx,
    Health,
    error_detail,
    extract_json_object,
)
from glance.backends.partial import CorrectedWatcher
from glance.backends.proc import IdleProc, ProcRunner
from glance.backends.usage import Usage, make_usage
from glance.log import log_event

_AUTH = re.compile(
    r"log ?in|logged in|authenticat|unauthori[sz]ed|api key|oauth|credential|sign in", re.IGNORECASE
)
_RATE = re.compile(
    r"rate.?limit|usage limit|limit reached|too many requests|quota|overloaded_error", re.IGNORECASE
)


def classify_error(status: int | None, text: str) -> str:
    """Map a claude failure (HTTP-ish status and message text) to a BackendError kind."""
    if status in (401, 403) or _AUTH.search(text):
        return "auth"
    if status == 429 or _RATE.search(text):
        return "rate_limit"
    return "unavailable"


def _json_lines(lines: Iterator[str]) -> Iterator[dict]:
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def claude_usage(obj: dict) -> Usage | None:
    """Tokens and cost from a claude `result` object (cache reads and writes count as input)."""
    u = obj.get("usage")
    u = u if isinstance(u, dict) else {}
    parts = [u.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                "cache_read_input_tokens")]
    ints = [p for p in parts if type(p) is int and p >= 0]
    return make_usage(sum(ints) if ints else None, u.get("output_tokens"),
                      obj.get("total_cost_usd"))


UNKNOWN_EFFORT = "unknown_effort"  # BackendError.detail: the CLI does not know `--effort`
_UNKNOWN_OPTION = re.compile(r"unknown option.{0,8}--effort", re.IGNORECASE)
PRIMER = "<"  # written to a spare process at once: every request starts with a tag
SPARE_IDLE_S = 600.0  # a spare process unused this long is stopped (memory back)


def _raise_if_error(obj: dict) -> None:
    if obj.get("is_error"):
        text = str(obj.get("result") or "")
        raise BackendError(classify_error(obj.get("api_error_status"), text),
                           error_detail(text, "claude_error"))


WARM_SYSTEM = ("Follow the instructions in the <system> block of each user message "
               "exactly.")


def image_message(png: bytes, text: str) -> str:
    """One stream-json input line: a user message with the image (a base64 `image` block, the
    Messages API's shape) and the question."""
    data = base64.b64encode(png).decode("ascii")
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data}},
               {"type": "text", "text": text}]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


class ClaudeBackend(BaseBackend):
    name = "claude"
    # Images go in through `--input-format stream-json` (`claude --help`: "realtime streaming
    # input", only with --print), as an image content block on stdin: never a file.
    vision = True
    # Findings S6.2: a warm (pre-spawned, idle) process saves ~0.86 s a call for ~195 MB idle
    # RSS. The user prioritises speed, so it is on for the chosen backend when that is claude
    # (`backend.warm`, see BackendCfg). The supervisor turns it on with `set_warm`; a backend
    # that is not in use never has one. One idle process, for rewrites: analyze is faster
    # cold (see the module docstring).
    WARM_ENABLED = True

    def __init__(self, runner: ProcRunner, model: str = "haiku", binary: str = "claude",
                 warm: bool = False, effort: str = "low") -> None:
        self.runner, self.model, self.binary, self.warm = runner, model, binary, warm
        self.effort = effort  # "" leaves `--effort` out (a CLI that does not know it)
        self.spare_idle_s = SPARE_IDLE_S
        self._idle: dict[str, IdleProc] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._idle_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._spawner: ThreadPoolExecutor | None = None

    # -- argv ----------------------------------------------------------------
    def _base(self, system: str) -> list[str]:
        model = ["--model", self.model] if self.model else []  # "": the CLI's own default
        effort = ["--effort", self.effort] if self.effort else []
        return [
            self.binary, "-p", *model, *effort, "--tools", "",
            "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
            "--system-prompt", system,
        ]

    def argv_json(self, system: str, schema: dict) -> list[str]:
        """`schema` is described in the system prompt, not passed as `--json-schema` (which
        costs a second model turn)."""
        return self._base(system) + ["--output-format", "json"]

    def argv_analyze(self, system: str) -> list[str]:
        """Analyze streams too (the corrected text shows as it comes): the same argv as a
        rewrite's, so the call and its spare process share one shape."""
        return self.argv_stream(system)

    def argv_stream(self, system: str) -> list[str]:
        return self._base(system) + [
            "--output-format", "stream-json", "--include-partial-messages", "--verbose",
        ]

    def argv_vision(self, system: str) -> list[str]:
        return self._base(system) + [
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
        ]

    def vision_label(self) -> str:
        return "Claude (the claude CLI)"

    def see(self, png: bytes, system: str, user: str) -> Iterator[str]:
        lines = self.runner.stream(self.argv_vision(system), image_message(png, user),
                                   VISION_TIMEOUT_S)
        yield from self._parse_stream(lines, self._add_usage)

    def _argv_warm(self, kind: str) -> list[str]:
        """A warm process cannot know the call's system prompt (it depends on the profile,
        tone and app), so it starts with a fixed one and the call's goes in the message."""
        return self._base(WARM_SYSTEM) + ["--input-format", "stream-json", "--output-format",
                                          "stream-json", "--verbose",
                                          "--include-partial-messages"]

    # -- warm spawn ------------------------------------------------------------
    @property
    def _warm_on(self) -> bool:
        return self.warm and self.WARM_ENABLED

    def set_warm(self, on: bool) -> None:
        """On or off; turning it off kills the idle processes (memory back at once)."""
        was, self.warm = self.warm, on
        if not on:
            self.close()
        elif not was:
            self.prewarm("rewrite")

    def set_model(self, model: str) -> None:
        before = self.model
        super().set_model(model)
        if self.model != before:
            self._restart_warm()  # idle processes were started with the old model

    def _drop_model(self, e: BackendError) -> bool:
        if e.detail == UNKNOWN_EFFORT and self.effort:
            log_event("effort_rejected", backend=self.name)
            self.effort = ""  # an older CLI: the call again, as it was before the flag
            self._restart_warm()
            return True
        dropped = super()._drop_model(e)
        if dropped:
            self._restart_warm()
        return dropped

    def _restart_warm(self) -> None:
        self.close()
        if self._warm_on:
            self.prewarm("rewrite")

    def prewarm(self, kind: str, system: str | None = None) -> None:
        """Start an idle process for `kind` in the background. One per kind, at most.
        "analyze" needs `system` (the next analyze's system prompt, part of its argv): its
        spare is the exact cold call, waiting on stdin; a spare started for another system
        prompt is replaced. "rewrite" is the stream-json session (one fixed argv)."""
        if kind not in ("analyze", "rewrite"):
            raise ValueError(f"unknown prewarm kind: {kind}")
        if not self._warm_on or (kind == "analyze" and system is None):
            return
        with self._spawn_lock:
            if self._spawner is None:  # ONE long-lived thread does every spawn: a process
                # started with PR_SET_PDEATHSIG dies when the thread that started it ends
                self._spawner = ThreadPoolExecutor(1, thread_name_prefix="claude-warm")
            self._spawner.submit(self._spawn_idle, kind, system)

    def prepare(self, ctx: Ctx) -> None:
        """A field got the focus: have the spare processes ready for its next calls."""
        from glance.backends.prompts import analyze_system

        self.prewarm("analyze", analyze_system(ctx))
        self.prewarm("rewrite")

    def wait_warm(self) -> None:
        """Block until the spawns asked for so far are done (tests)."""
        with self._spawn_lock:
            spawner = self._spawner
        if spawner is not None:
            spawner.submit(lambda: None).result()

    def _spawn_idle(self, kind: str, system: str | None = None) -> None:
        stale: IdleProc | None = None
        with self._idle_lock:
            if not self._warm_on:
                return
            argv = self._argv_warm(kind) if kind == "rewrite" else self.argv_analyze(system or "")
            cur = self._idle.get(kind)
            if cur is not None and cur.alive and cur.argv == argv:
                cur.touched = time.monotonic()
                self._arm_expiry(kind, cur)
                return
            stale = self._idle.pop(kind, None)
            try:
                new = self.runner.spawn_idle(argv, PRIMER if kind == "analyze" else "")
            except BackendError:
                new = None
            if new is not None:
                self._idle[kind] = new
                self._arm_expiry(kind, new)
        if stale is not None:  # never two spares of a kind: the old one goes first thing
            stale.kill()

    def _arm_expiry(self, kind: str, proc: IdleProc) -> None:
        """(Under _idle_lock.) Stop `proc` once it has sat unused for `spare_idle_s`; the
        next field focus starts another. A sleeping timer thread: no CPU while idle."""
        old = self._timers.pop(kind, None)
        if old is not None:
            old.cancel()
        t = threading.Timer(self.spare_idle_s, self._expire, args=(kind, proc))
        t.daemon = True
        self._timers[kind] = t
        t.start()

    def _expire(self, kind: str, proc: IdleProc) -> None:
        with self._idle_lock:
            if self._idle.get(kind) is not proc:
                return
            if time.monotonic() - proc.touched < self.spare_idle_s:
                return
            del self._idle[kind]
            self._timers.pop(kind, None)
        proc.kill()

    def _take_idle(self, kind: str) -> IdleProc | None:
        with self._idle_lock:
            cur = self._idle.pop(kind, None)
            t = self._timers.pop(kind, None)
        if t is not None:
            t.cancel()
        return cur if cur is not None and cur.alive else None

    @staticmethod
    def _warm_message(system: str, user: str) -> str:
        content = f"<system>\n{system}\n</system>\n\n{user}"
        return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"

    def close(self) -> None:
        with self._idle_lock:
            idle, self._idle = list(self._idle.values()), {}
            timers, self._timers = list(self._timers.values()), {}
        for t in timers:
            t.cancel()
        for p in idle:
            p.kill()
        with self._spawn_lock:
            spawner, self._spawner = self._spawner, None
        if spawner is not None:  # its idle thread would otherwise hold glanced's exit
            spawner.shutdown(wait=False, cancel_futures=True)

    # -- primitives --------------------------------------------------------------
    def _run_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        """One analyze: on the spare process when it was started for this exact call, else a
        cold one. The "corrected" text is reported as it streams (`partial`); the answer is
        taken from the `result` line, without waiting for the process to exit."""
        argv = self.argv_analyze(system)
        spare = self._take_idle("analyze") if self._warm_on else None
        err: list[str] = []
        try:
            if spare is not None and spare.fits(argv, user):
                lines = spare.feed(user, timeout_s)
            else:
                if spare is not None:
                    spare.kill()  # started for another prompt (profile, app, language)
                lines = self.runner.stream(argv, user, timeout_s, err_sink=err)
            watch = CorrectedWatcher()
            text = ""
            for obj in _json_lines(lines):
                t = obj.get("type")
                if t == "stream_event":
                    ev = obj.get("event") or {}
                    d = ev.get("delta") or {}
                    if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                        text += d.get("text") or ""
                        watch.feed(text)
                elif t == "result":
                    release = getattr(lines, "release", None)
                    if release is not None:
                        release()  # the process may take another second to exit
                    self._add_usage(claude_usage(obj))
                    return self._structured(obj)
            raise self._no_result(err, "no json")
        finally:
            from glance.backends.prompts import REPAIR_SUFFIX

            if not system.endswith(REPAIR_SUFFIX):  # refill: the next check finds one ready
                self.prewarm("analyze", system)

    @staticmethod
    def _no_result(err: list[str], default: str) -> BackendError:
        """The stream ended with no `result` line: why, from what the CLI said on stderr."""
        said = "".join(err)
        if _UNKNOWN_OPTION.search(said):
            return BackendError("unavailable", UNKNOWN_EFFORT)
        if said.strip():
            return BackendError(classify_error(None, said), error_detail(said, default))
        return BackendError("bad_output", default)

    @staticmethod
    def _structured(obj: dict) -> dict:
        _raise_if_error(obj)
        so = obj.get("structured_output")
        if isinstance(so, dict):
            return so
        return extract_json_object(str(obj.get("result") or ""))  # fences/prose tolerated

    def _run_stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        idle = self._take_idle("rewrite") if self._warm_on else None
        try:
            err: list[str] = []
            if idle is not None:
                lines = idle.feed(self._warm_message(system, user), timeout_s)
            else:
                lines = self.runner.stream(self.argv_stream(system), user, timeout_s,
                                           err_sink=err)
            yield from self._parse_stream(lines, self._add_usage, err)
        finally:
            if idle is not None:
                idle.kill()
                self.prewarm("rewrite")

    @classmethod
    def _parse_stream(cls, lines: Iterator[str],
                      on_usage: Callable[[Usage | None], None] | None = None,
                      err: list[str] | None = None) -> Iterator[str]:
        """Text deltas, then whatever of the `result` text they did not cover. Stops at the
        `result` line: the process is left to exit by itself."""
        sent = ""
        final: str | None = None
        for obj in _json_lines(lines):
            t = obj.get("type")
            if t == "stream_event":
                ev = obj.get("event") or {}
                d = ev.get("delta") or {}
                if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                    text = d.get("text") or ""
                    if text:
                        sent += text
                        yield text
            elif t == "result":
                if on_usage is not None:
                    on_usage(claude_usage(obj))
                _raise_if_error(obj)
                final = obj.get("result") if isinstance(obj.get("result"), str) else None
                release = getattr(lines, "release", None)
                if release is not None:
                    release()
                break
        if final is None:
            if not sent:
                raise cls._no_result(err or [], "no result")
            return
        if final.startswith(sent):
            if len(final) > len(sent):
                yield final[len(sent):]
        elif not sent:
            yield final

    def cancel(self) -> None:
        self.runner.cancel()

    def health(self) -> Health:
        try:
            res = self.runner.run([self.binary, "auth", "status", "--json"], "", 3.0)
        except BackendError as e:
            if e.kind == "unavailable":
                return Health(False, None, "claude CLI not installed")
            return Health(True, None, e.kind)
        try:  # the CLI prints one indented JSON object over many lines
            obj = json.loads(res.stdout)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            obj = next(iter(reversed(list(_json_lines(iter(res.stdout.splitlines()))))), None)
        if obj is None or not isinstance(obj.get("loggedIn"), bool):
            return Health(True, None, "unrecognised auth status")
        return Health(True, obj["loggedIn"], "signed in" if obj["loggedIn"] else "signed out")
