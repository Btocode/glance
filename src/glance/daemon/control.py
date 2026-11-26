"""The ctl.sock side of glanced: one router for every request. Main loop only, no GTK."""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from glance.daemon.test_hooks import handle_test_message
from glance.log import log_event

Reply = Callable[[dict[str, Any]], None]


def handle_control(controller: Any, msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The reply to one `ctl.sock` message (the caller adds the request id). Main loop only."""
    if msg_type == "ping":
        return {"ok": True, "pid": os.getpid()}
    if msg_type == "status":
        return {"ok": True, **controller.status()}
    if msg_type == "pause":
        seconds = payload.get("seconds")
        if seconds is not None and (
                isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0):
            return {"ok": False, "error": "bad seconds"}
        controller.resume()  # a second pause replaces the first one's duration
        controller.toggle_pause(seconds)
        return {"ok": True, "paused": True}
    if msg_type == "resume":
        controller.resume()
        return {"ok": True, "paused": False}
    reply = handle_test_message(controller, msg_type, payload)  # None unless GLANCE_TEST=1
    if reply is not None:
        return reply
    return {"ok": False, "error": "unknown request"}


def handle_test_panel_action(msg_type: str, payload: dict[str, Any],
                             dispatch: Callable[[str, dict[str, Any]], None],
                             env: Any = None) -> dict[str, Any] | None:
    """`test_panel_action {action, args}`: a click in the panel, for the tests that cannot
    aim at a button from outside. Exists only with GLANCE_TEST=1; None means "not mine"."""
    env = os.environ if env is None else env
    if msg_type != "test_panel_action" or env.get("GLANCE_TEST") != "1":
        return None
    action, args = payload.get("action"), payload.get("args", {})
    if not isinstance(action, str) or not isinstance(args, dict):
        return {"ok": False, "error": "bad request"}
    dispatch(action, args)
    return {"ok": True}


class CtlRouter:
    """Answers one ctl message. Most answer at once; `edit_session` (the user is editing)
    and the backend requests (health checks, a real call) answer later, still on the main
    loop. The request id is added to every reply here."""

    def __init__(self, controller: Any, panel_action: Callable[[str, dict[str, Any]], None],
                 backend_control: Any = None, edit_host: Any = None,
                 extra: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None
                 ) -> None:
        """`extra`: more requests, name -> handler(payload) -> reply (e.g. `shot`)."""
        self._c = controller
        self._panel_action = panel_action
        self._backends = backend_control
        self._edit = edit_host
        self._extra = dict(extra or {})

    def handle(self, msg_type: str, payload: dict[str, Any], send: Reply) -> None:
        def reply(msg: dict[str, Any]) -> None:
            out = dict(msg)
            if "id" in payload:
                out["id"] = payload["id"]
            try:
                send(out)
            except ConnectionError:
                pass  # the client left without waiting

        try:
            if msg_type == "edit_session" and self._edit is not None:
                self._edit.handle(payload, reply)
                return
            if self._backends is not None and self._backends.handles(msg_type):
                self._backends.handle(msg_type, payload, reply)
                return
            if msg_type in self._extra:
                reply(self._extra[msg_type](payload))
                return
        except Exception as e:  # noqa: BLE001 - one bad request must not end the daemon
            log_event("ctl_error", type=msg_type, kind=type(e).__name__)
            reply({"ok": False, "error": "failed"})
            return
        out = handle_test_panel_action(msg_type, payload, self._panel_action)
        reply(out if out is not None else handle_control(self._c, msg_type, payload))
