"""Direct HTTPS backend: Anthropic Messages API and any OpenAI-compatible server.

The request body (user text) and the API key are never logged, and error details never include
the response body (a server may echo the key).
"""
from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx

from glance.backends.base import VISION_TIMEOUT_S, BackendError, BaseBackend, Health
from glance.backends.keystore import KeyStore
from glance.backends.usage import Usage, make_usage
from glance.config import API_DEFAULT_MODEL as DEFAULT_ANTHROPIC_MODEL
from glance.log import log_event

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_URL = "https://api.anthropic.com"
OPENAI_URL = "https://api.openai.com/v1"
MAX_TOKENS = 4096  # a whole message of `[ai] max_words` (600) words, rewritten, fits
# Connections are kept open between calls (httpx's default drops an idle one after 5 s), and
# one is opened when a field gets the focus (`prepare`): a check then skips DNS, TCP and TLS.
KEEPALIVE_S = 120.0
PREPARE_EVERY_S = 30.0  # at most one connection-opening request per this many seconds


def _status_error(status: int) -> BackendError:
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        kind = "rate_limit"
    else:
        kind = "unavailable"
    return BackendError(kind, f"HTTP {status}")


def _api_usage(u: object) -> Usage | None:
    """Tokens from an Anthropic (`input_tokens`) or OpenAI (`prompt_tokens`) usage object."""
    if not isinstance(u, dict):
        return None
    pick = lambda *keys: next((u[k] for k in keys if k in u), None)
    return make_usage(pick("input_tokens", "prompt_tokens"),
                      pick("output_tokens", "completion_tokens"))


def image_content(provider: str, png: bytes, text: str) -> list[dict]:
    """The user message's content for an image and a question: Anthropic's `image` block
    (base64 source), or OpenAI's `image_url` with a data URL."""
    data = base64.b64encode(png).decode("ascii")
    if provider == "anthropic":
        return [{"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": data}},
                {"type": "text", "text": text}]
    return [{"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}]


class ApiBackend(BaseBackend):
    name = "api"
    vision = True  # Anthropic's models and OpenAI-compatible vision models take images

    def __init__(
        self,
        keys: KeyStore,
        provider: str = "anthropic",
        model: str = "",
        base_url: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._keys = keys
        self.configure(provider, model, base_url)
        self._client = client or httpx.Client(
            limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))
        self._prepared_at: float | None = None
        self._lock = threading.Lock()
        self._resp: httpx.Response | None = None
        self._cancelled = False
        self._stream_tokens: list[int | None] = [None, None]

    # -- public -----------------------------------------------------------------------------

    def configure(self, provider: str, model: str | None = "",
                  base_url: str | None = "") -> bool:
        """Take new `backend.api_*` settings (a config reload). True when anything changed."""
        if provider not in ("anthropic", "openai"):
            raise ValueError(f"unknown api provider: {provider}")
        new = (provider,
               model or (DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else ""),
               (base_url or (ANTHROPIC_URL if provider == "anthropic" else OPENAI_URL))
               .rstrip("/"))
        old = getattr(self, "_settings", None)
        self._settings = new
        self._provider, self._model, self._base = new
        return new != old

    def health(self) -> Health:
        signed_in = self._keys.get(self._provider) is not None
        if self._provider == "openai" and not self._model:
            return Health(True, signed_in, "set backend.api_model")
        return Health(True, signed_in, "ok" if signed_in else "no API key stored")

    def vision_key(self) -> str:
        return f"api:{self._provider}:{urlsplit(self._base).hostname or self._base}"

    def vision_label(self) -> str:
        host = urlsplit(self._base).hostname or self._base
        who = "Anthropic" if self._provider == "anthropic" else "the OpenAI-compatible API"
        return f"{who} ({host})"

    def see(self, png: bytes, system: str, user: str) -> Iterator[str]:
        yield from self._stream_content(system, image_content(self._provider, png, user),
                                        VISION_TIMEOUT_S)

    def prepare(self, _ctx: object = None) -> None:
        """A field got the focus: open the connection the next call will use, with a bare
        HEAD request to the server's base URL. No key and nothing of the text is sent; any
        answer will do. Skipped without a key, and when done in the last PREPARE_EVERY_S."""
        now = time.monotonic()
        if self._prepared_at is not None and now - self._prepared_at < PREPARE_EVERY_S:
            return
        if not self._keys.get(self._provider):
            return
        self._prepared_at = now
        try:
            self._client.head(self._base + "/", timeout=httpx.Timeout(3.0))
        except httpx.HTTPError:
            pass  # only a head start

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            resp = self._resp
        if resp is not None:
            resp.close()

    # -- request plumbing -------------------------------------------------------------------

    def _prepare(self) -> tuple[str, dict[str, str]]:
        key = self._keys.get(self._provider)
        if not key:
            raise BackendError("auth", "no API key")
        if not self._model:
            raise BackendError("unavailable", "set backend.api_model")
        if self._provider == "anthropic":
            return f"{self._base}/v1/messages", {
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
        return f"{self._base}/chat/completions", {
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
        }

    def _open(self, body: dict, timeout_s: float) -> httpx.Response:
        url, headers = self._prepare()
        with self._lock:
            self._cancelled = False
        try:
            req = self._client.build_request(
                "POST", url, headers=headers, content=json.dumps(body),
                timeout=httpx.Timeout(timeout_s),
            )
            resp = self._client.send(req, stream=True)
        except httpx.TimeoutException as e:
            raise BackendError("timeout", type(e).__name__) from None
        except httpx.HTTPError as e:
            raise BackendError("unavailable", type(e).__name__) from None
        with self._lock:
            self._resp = resp
        if resp.status_code >= 400:
            status = resp.status_code
            resp.close()
            log_event("api_http_error", provider=self._provider, status=status)
            raise _status_error(status)
        return resp

    def _close(self, resp: httpx.Response) -> None:
        with self._lock:
            if self._resp is resp:
                self._resp = None
        resp.close()

    def _post_json(self, body: dict, timeout_s: float) -> dict:
        resp = self._open(body, timeout_s)
        try:
            resp.read()
            data = resp.json()
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except (httpx.HTTPError, ValueError) as e:
            if self._cancelled:
                raise BackendError("cancelled") from None
            raise BackendError("bad_output", type(e).__name__) from None
        finally:
            self._close(resp)
        if not isinstance(data, dict):
            raise BackendError("bad_output", "response is not an object")
        self._add_usage(_api_usage(data.get("usage")))
        return data

    # -- primitives -------------------------------------------------------------------------

    def _run_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        if self._provider == "anthropic":
            return self._anthropic_json(system, user, schema, timeout_s)
        return self._openai_json(system, user, schema, timeout_s)

    def _anthropic_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        data = self._post_json(
            {
                "model": self._model,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "tools": [{"name": "report", "description": "Report the analysis.",
                           "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": "report"},
            },
            timeout_s,
        )
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                inp = block.get("input")
                if isinstance(inp, dict):
                    return inp
        raise BackendError("bad_output", "no tool_use block")

    def _openai_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        def body(fmt: dict) -> dict:
            return {
                "model": self._model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "response_format": fmt,
            }

        strict = {"type": "json_schema",
                  "json_schema": {"name": "report", "schema": schema, "strict": True}}
        try:
            data = self._post_json(body(strict), timeout_s)
        except BackendError as e:
            if e.detail != "HTTP 400":
                raise
            data = self._post_json(body({"type": "json_object"}), timeout_s)
        try:
            out = json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise BackendError("bad_output", "no JSON content") from None
        if not isinstance(out, dict):
            raise BackendError("bad_output", "content is not an object")
        return out

    def _run_stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        yield from self._stream_content(system, user, timeout_s)

    def stream_body(self, system: str, content: str | list[dict]) -> dict:
        """The streaming request body; `content` is the user text or a list of blocks."""
        if self._provider == "anthropic":
            return {
                "model": self._model,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": content}],
                "stream": True,
            }
        return {
            "model": self._model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "stream": True,
        }

    def _stream_content(self, system: str, content: str | list[dict],
                        timeout_s: float) -> Iterator[str]:
        body = self.stream_body(system, content)
        resp = self._open(body, timeout_s)
        self._stream_tokens = [None, None]
        try:
            for line in resp.iter_lines():
                if self._cancelled:
                    break
                text = self._delta(line)
                if text:
                    yield text
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except httpx.HTTPError as e:
            if self._cancelled:
                raise BackendError("cancelled") from None
            raise BackendError("unavailable", type(e).__name__) from None
        finally:
            self._close(resp)
            self._add_usage(make_usage(*self._stream_tokens))
        if self._cancelled:
            raise BackendError("cancelled")

    def _note_stream_usage(self, ev: dict) -> None:
        """Token counts that ride on stream events: Anthropic's `message_start` (input) and
        `message_delta` (output so far), an OpenAI-compatible chunk's `usage`."""
        if self._provider == "anthropic":
            msg = ev.get("message")
            u = _api_usage(msg.get("usage") if isinstance(msg, dict) else ev.get("usage"))
        else:
            u = _api_usage(ev.get("usage"))
        if u is not None:
            if u.input_tokens is not None:
                self._stream_tokens[0] = u.input_tokens
            if u.output_tokens is not None:
                self._stream_tokens[1] = u.output_tokens

    def _delta(self, line: str) -> str | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            ev = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(ev, dict):
            return None
        self._note_stream_usage(ev)
        if self._provider == "anthropic":
            if ev.get("type") != "content_block_delta":
                return None
            delta = ev.get("delta")
            text = delta.get("text") if isinstance(delta, dict) else None
        else:
            choices = ev.get("choices")
            delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else None
            text = delta.get("content") if isinstance(delta, dict) else None
        return text if isinstance(text, str) else None
