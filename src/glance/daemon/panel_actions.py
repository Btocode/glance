"""What a click in a panel does: the field's panel, select-anything's and the send-guard
banner share one overlay, so one router decides who gets each action. Main loop only."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.log import log_event

# Actions of select-anything's panel; `tone` there selects a tone and shows its rewrite.
SELECTION_ACTIONS = ("tone", "sel_copy", "sel_paste", "close")


def dispatch_panel_action(controller: Any, action: str, args: dict[str, Any],
                          ai: Any = None,
                          remember_profile: Callable[[str, str], None] | None = None) -> None:
    """Run what a click in the strip or the drawer asks for; a malformed action does nothing.

    The tone actions need `ai` (the AiCoordinator). `remember_profile(context key, name)`
    stores an audience the user picked.
    """
    try:
        if action in ("tone", "tone_apply", "apply_tone", "profile"):
            if ai is None:
                log_event("panel_action_unknown")
            elif action == "tone":  # a click on a tone chip: select it (again: unselect)
                ai.select(str(args["key"]))
            elif action == "tone_apply":  # the hover menu's Shorter...: made, then applied
                key = str(args["key"])
                if key == "custom":
                    ai.select(key)  # asks how first
                else:
                    ai.apply(key)
            elif action == "apply_tone":
                ai.apply()
            else:
                ai.set_profile_override(str(args["name"]))
                resolved = ai.resolved()
                if (remember_profile is not None and resolved is not None
                        and resolved.source == "override"):
                    remember_profile(resolved.key, resolved.profile.name)
        elif action == "copy":
            if ai is None or not ai.copy_rewrite():
                controller.copy_instead()
        elif action == "use":
            controller.use_strip()
        elif action == "more":
            controller.toggle_panel()
        elif action == "accept":
            controller.accept_issue(str(args["issue_id"]), int(args.get("choice", 0)))
        elif action == "fix_all":
            controller.fix_all_safe()
        elif action == "add_dict":
            controller.add_to_dictionary(str(args["issue_id"]))
        elif action == "ignore":
            controller.ignore_rule(str(args["issue_id"]), str(args["scope"]))
        elif action == "close":
            controller.close_panel()
        elif action == "check":  # the status icon's hover menu
            controller.check_now()
        elif action == "check_tone":
            controller.check_tone()
        elif action == "rewrite":
            controller.open_panel()
        elif action == "resume":
            controller.resume()
        else:
            log_event("panel_action_unknown")
    except (KeyError, TypeError, ValueError):
        log_event("panel_action_malformed")


class PanelRouter:
    """Sends a panel action to select-anything while its panel is open, the guard banner's
    buttons to the send-guard, and the rest to `dispatch_panel_action`."""

    def __init__(self, controller: Any, ai: Any = None,
                 remember_profile: Callable[[str, str], None] | None = None,
                 selection: Any = None, guard: Any = None,
                 on_setup: Callable[[], None] | None = None) -> None:
        self._c, self._ai, self._remember = controller, ai, remember_profile
        self.selection, self.guard = selection, guard
        self._on_setup = on_setup

    def action(self, action: str, args: dict[str, Any]) -> None:
        sa = self.selection
        if sa is not None and sa.is_open and action in SELECTION_ACTIONS:
            self._selection_action(sa, action, args)
            return
        if action == "setup":  # the hover menu's "Connect an AI…"
            if self._on_setup is not None:
                self._on_setup()
            return
        if action == "guard_silence":
            if self.guard is None or args.get("scope") not in ("app", "global"):
                log_event("panel_action_malformed")
            else:
                self.guard.silence(args["scope"])
            return
        dispatch_panel_action(self._c, action, args, self._ai, self._remember)

    def escape(self) -> None:
        sa = self.selection
        if sa is not None and sa.is_open:
            sa.close()
        else:
            self._c.close_panel(snooze=True)

    @staticmethod
    def _selection_action(sa: Any, action: str, args: dict[str, Any]) -> None:
        if action == "tone":
            key = args.get("key")
            if isinstance(key, str):
                sa.select(key)
            else:
                log_event("panel_action_malformed")
        elif action == "sel_copy":
            sa.copy()
        elif action == "sel_paste":
            sa.paste_over()
        else:
            sa.close()
