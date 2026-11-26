"""Glance Shot's glue in glanced (Linux and macOS alike): the controller, its hotkey action,
the `shot` ctl request and the tray item, following the feature registry's `shot` switch
(`[shot] enabled`) live. No GTK here."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.protocol import Rect
from glance.shot.consent import ImageConsent
from glance.shot.controller import ShotController

SHOT_OFF = "Glance Shot is off: `glance features shot on` (or the tray's Features ▸ Glance Shot)"


def password_rect(controller: Any) -> Callable[[], Rect | None]:
    """The focused password field's rectangle (its window's, when the field's is unknown),
    or None: a shot over it never goes to an AI."""
    def rect() -> Rect | None:
        cur = getattr(controller, "current", None)
        if cur is None or cur.gated != "password":
            return None
        return cur.field_rect or cur.window_rect

    return rect


def build_shot(*, ui: Any, controller: Any, supervisor: Any, cfg: Callable[[], Any],
               overlay: Any, notifier: Any, hotkeys: Any, run_ai: Any, grammar: Any,
               consent_path: Any = None,
               text_panel: Callable[[str, Rect, bool], None] | None = None) -> ShotController:
    """The ShotController for glanced. `text_panel` is the seam for the selection actions'
    panel: when given, Extract text opens its text there."""
    if consent_path is None:
        from glance.paths import state_dir

        consent_path = state_dir() / "shot-images.json"
    shot = ShotController(ui=ui, cfg=cfg, supervisor=supervisor, overlay=overlay,
                          notifier=notifier, hotkeys=hotkeys, run_bg=run_ai,
                          consent=ImageConsent(consent_path), grammar=grammar,
                          password_rect=password_rect(controller), text_panel=text_panel)
    controller.extra_status.append(shot.status)
    return shot


def shot_request(shot: ShotController) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """`glance shot` over ctl.sock."""
    def handle(_payload: dict[str, Any]) -> dict[str, Any]:
        if shot.trigger():
            return {"ok": True}
        return {"ok": False, "error": SHOT_OFF}

    return handle


class ShotSwitch:
    """Keeps the hotkey action and the tray's "Take a Glance Shot" in step with the feature
    registry's `shot` switch (`feature_switches`, i.e. `[shot] enabled`: the same value the
    tray's "Features ▸ Glance Shot" and `glance features shot on|off` write). Off means no
    hotkey grab and no tray item (and an open picker or bar closes)."""

    def __init__(self, shot: ShotController, actions: dict[str, Callable[[], None]],
                 tray: Any, rebind: Callable[[], None], hotkeys: Any) -> None:
        self._shot, self._actions, self._tray, self._rebind = shot, actions, tray, rebind
        self._hotkeys = hotkeys
        self.on: bool | None = None

    def apply(self, cfg: Any, rebind: bool = True) -> None:
        """Follow the config (at start, and after every change of it)."""
        from glance.feature_switches import is_on

        enabled = is_on(cfg, "shot")
        if enabled == self.on:
            return
        self.on = enabled
        if enabled:
            self._actions["shot"] = self._shot.trigger
        else:
            self._actions.pop("shot", None)
            self._shot.close()
            if "shot" in self._hotkeys.grabbed():
                self._hotkeys.ungrab("shot")  # bind_hotkeys only releases names it binds
        setter = getattr(self._tray, "set_shot", None)
        if setter is not None:
            setter(self._shot.trigger if enabled else None)
        if rebind:
            self._rebind()
