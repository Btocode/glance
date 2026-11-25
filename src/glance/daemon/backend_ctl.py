"""ctl.sock messages for `glance backend ...`, `glance rewrite` and `glance profile`.

`handle` runs on the main loop. Everything that can block (health checks, a real backend
call, libsecret) goes through `run_bg(work, done)`, which must run `work` off the main loop
and call `done(result)` back on it. Replies go to the `reply` callback, without the request
id (the caller adds it). Typed text and API keys are never logged.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from glance.backends.base import Ctx
from glance.backends.keystore import KeyStore
from glance.backends.prompts import TONES
from glance.config import (
    CLI_MODEL_BACKENDS,
    DEFAULT_MODELS,
    MODEL_NAME,
    BackendCfg,
    ConfigError,
    update_config,
)
from glance.paths import config_path as default_config_path
from glance.ui.onboarding_model import ORDER, build_rows, effective_model

API_PROVIDERS = ("anthropic", "openai")
MAX_KEY_CHARS = 4096
MAX_TEXT_CHARS = 20_000
CLI_CTX = Ctx(profile="default", target_tone="neutral", strictness="normal", app_label="glance")
MESSAGES = ("backend_list", "backend_use", "backend_test", "backend_key", "backend_api", "backend_model",
            "rewrite", "profile_show", "profile_set", "profile_clear")

Reply = Callable[[dict[str, Any]], None]


def _fail(error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, **extra}


class BackendControl:
    def __init__(self, supervisor: Any, ai: Any, keystore: KeyStore,
                 cfg: Callable[[], Any], overrides: Any,
                 run_bg: Callable[[Callable[[], Any], Callable[[Any], None]], None],
                 config_path: Path | None = None) -> None:
        self._sup, self._ai, self._keys = supervisor, ai, keystore
        self._cfg, self._overrides, self._run_bg = cfg, overrides, run_bg
        self._config_path = config_path

    def handles(self, msg_type: str) -> bool:
        return msg_type in MESSAGES

    def handle(self, msg_type: str, payload: dict[str, Any], reply: Reply) -> None:
        getattr(self, f"_{msg_type}")(payload, reply)

    # -- helpers -------------------------------------------------------------------
    def _backend_cfg(self) -> BackendCfg:
        return self._cfg().backend

    def _bg(self, work: Callable[[], dict[str, Any]], reply: Reply,
            refresh_status: bool = False) -> None:
        def done(result: dict[str, Any]) -> None:
            if refresh_status:
                self._ai.refresh_status()
            reply(result)

        def safe() -> dict[str, Any]:
            try:
                return work()
            except ConfigError as e:
                return _fail(f"config: {e}")
            except Exception as e:  # noqa: BLE001 - one bad request must not end the daemon
                return _fail(f"failed: {e.__class__.__name__}")

        self._run_bg(safe, done)

    # -- backends ------------------------------------------------------------------
    def _backend_list(self, p: dict[str, Any], reply: Reply) -> None:
        refresh = p.get("refresh") is True

        def work() -> dict[str, Any]:
            healths = self._sup.healths(refresh)
            cfg = self._backend_cfg()
            return {"ok": True, "choice": cfg.choice,
                    "models": {n: effective_model(n, cfg) for n in ORDER}, "backends": {
                n: {"installed": h.installed, "signed_in": h.signed_in, "detail": h.detail}
                for n, h in healths.items()}}

        self._bg(work, reply)

    def _backend_use(self, p: dict[str, Any], reply: Reply) -> None:
        name = p.get("name")
        if name not in ORDER:
            reply(_fail("unknown backend"))
            return

        def work() -> dict[str, Any]:
            row = next(r for r in build_rows(self._sup.healths(), None) if r.name == name)
            if not row.selectable:
                return _fail("not_ready", name=name)
            self._sup.use(name)
            return {"ok": True, "name": name}

        self._bg(work, reply, refresh_status=True)

    def _backend_test(self, p: dict[str, Any], reply: Reply) -> None:
        name = p.get("name") or self._backend_cfg().choice
        if name is None:
            reply(_fail("no backend chosen"))
            return
        if name not in ORDER:
            reply(_fail("unknown backend"))
            return

        def work() -> dict[str, Any]:
            passed, detail = self._sup.test(name)  # a real analyze (rewrite for codex)
            return {"ok": True, "name": name, "passed": passed, "detail": detail}

        self._bg(work, reply, refresh_status=True)

    def _backend_key(self, p: dict[str, Any], reply: Reply) -> None:
        provider = p.get("provider") or self._backend_cfg().api_provider
        key = p.get("key")
        if provider not in API_PROVIDERS:
            reply(_fail("unknown provider"))
            return
        if not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY_CHARS:
            reply(_fail("bad key"))
            return
        key = key.strip()

        def work() -> dict[str, Any]:
            self._keys.set(provider, key)  # libsecret only: never config, never a log line
            self._sup.healths(True)  # so the onboarding row turns green
            return {"ok": True, "provider": provider}

        self._bg(work, reply, refresh_status=True)

    def _backend_model(self, p: dict[str, Any], reply: Reply) -> None:
        """Set a backend's model, or (`model` absent / null) go back to the default."""
        name, model = p.get("name"), p.get("model")
        if name not in (*CLI_MODEL_BACKENDS, "api"):
            reply(_fail("unknown backend"))
            return
        if model is not None and (not isinstance(model, str) or not MODEL_NAME.fullmatch(model)):
            reply(_fail("bad model"))
            return
        key = "backend.api_model" if name == "api" else f"backend.models.{name}"
        path = self._config_path or default_config_path()

        def work() -> dict[str, Any]:
            update_config(path, key, model)
            if model is None:  # `models` merges over the table, so the key is just removed
                shown = DEFAULT_MODELS.get(name, "") if name != "api" else ""
            else:
                shown = model
            return {"ok": True, "name": name, "model": shown, "default": model is None}

        self._bg(work, reply)

    def _backend_api(self, p: dict[str, Any], reply: Reply) -> None:
        changes: dict[str, str | None] = {}
        if "provider" in p:
            if p["provider"] not in API_PROVIDERS:
                reply(_fail("unknown provider"))
                return
            changes["backend.api_provider"] = p["provider"]
        if "model" in p:
            if not isinstance(p["model"], str) or len(p["model"]) > 200:
                reply(_fail("bad model"))
                return
            changes["backend.api_model"] = p["model"].strip() or None
        if "base_url" in p:
            url = p["base_url"]
            if not isinstance(url, str) or (url.strip() and not url.strip().startswith(
                    ("https://", "http://"))):
                reply(_fail("bad base url"))
                return
            changes["backend.api_base_url"] = url.strip() or None
        path = self._config_path or default_config_path()

        def work() -> dict[str, Any]:
            for key, value in changes.items():
                update_config(path, key, value)
            return {"ok": True}

        self._bg(work, reply)

    # -- rewrite -------------------------------------------------------------------
    def _rewrite(self, p: dict[str, Any], reply: Reply) -> None:
        from glance.daemon.rewrite import rewrite_text

        tone, text = p.get("tone"), p.get("text")
        if tone not in TONES or tone == "custom":
            reply(_fail("unknown tone"))
            return
        if not isinstance(text, str) or not text.strip():
            reply(_fail("no text"))
            return
        if len(text) > MAX_TEXT_CHARS:
            reply(_fail(f"text too long (limit {MAX_TEXT_CHARS} characters)"))
            return

        def work() -> dict[str, Any]:
            outcome = rewrite_text(self._sup, text, tone, CLI_CTX)
            if outcome.text is None:
                return _fail(outcome.reason or "failed")
            return {"ok": True, "text": outcome.text}

        self._bg(work, reply)

    # -- profile of the focused field ------------------------------------------------
    def _focused(self, reply: Reply) -> Any:
        r = self._ai.resolved()
        if r is None:
            reply(_fail("no focused field"))
        return r

    def _profile_show(self, _p: dict[str, Any], reply: Reply) -> None:
        r = self._focused(reply)
        if r is not None:
            reply({"ok": True, "profile": r.profile.name, "key": r.key, "source": r.source})

    def _profile_set(self, p: dict[str, Any], reply: Reply) -> None:
        r = self._focused(reply)
        if r is None:
            return
        name = p.get("name")
        if not isinstance(name, str):
            reply(_fail("unknown profile"))
            return
        try:
            self._ai.set_profile_override(name)
        except ValueError as e:
            reply(_fail(str(e)))
            return
        self._overrides.set(r.key, name)
        reply({"ok": True, "profile": name})

    def _profile_clear(self, _p: dict[str, Any], reply: Reply) -> None:
        r = self._focused(reply)
        if r is None:
            return
        self._ai.set_profile_override(None)
        self._overrides.clear(r.key)
        reply({"ok": True})
