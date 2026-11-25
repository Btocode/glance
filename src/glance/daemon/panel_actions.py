"""What a click in a panel does: the field's panel, select-anything's and the send-guard
banner share one overlay, so one router decides who gets each action. Main loop only."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.log import log_event

# Actions of select-anything's panel; `tone` and `tone_hover` there preview a rewrite.
SELECTION_ACTIONS = ("tone", "tone_hover", "sel_copy", "sel_paste", "close")


def dispatch_panel_action(controller: Any, action: str, args: dict[str, Any],
                          ai: Any = None,
                          remember_profile: Callable[[str, str], None] | None = None) -> None:
    """Run what a click in the strip or the drawer asks for; a malformed action does nothing.

    The tone actions need `ai` (the AiCoordinator). `remember_profile(context key, name)`
    stores an audience the user picked.
    """
    try:
        if action in ("tone", "tone_hover", "tone_apply", "apply_tone", "profile"):
            if ai is None:
                log_event("panel_action_unknown")
            elif action == "tone":
                ai.choose(str(args["key"]))
            elif action == "tone_apply":  # a click on a tone card: made first if need be
                key = str(args["key"])
                if key == "custom":
                    ai.choose(key)  # asks how first
                else:
                    ai.apply(key)
            elif action == "tone_hover":
                ai.preview(str(args["key"]))
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
        else:
            log_event("panel_action_unknown")
    except (KeyError, TypeError, ValueError):
        log_event("panel_action_malformed")


class PanelRouter:
    """Sends a panel action to select-anything while its panel is open, the guard banner's
    buttons to the send-guard, and the rest to `dispatch_panel_action`."""

    def __init__(self, controller: Any, ai: Any = None,
                 remember_profile: Callable[[str, str], None] | None = None,
                 selection: Any = None, guard: Any = None) -> None:
        self._c, self._ai, self._remember = controller, ai, remember_profile
        self.selection, self.guard = selection, guard

    def action(self, action: str, args: dict[str, Any]) -> None:
        sa = self.selection
        if sa is not None and sa.is_open and action in SELECTION_ACTIONS:
            self._selection_action(sa, action, args)
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
        if action in ("tone", "tone_hover"):
            key = args.get("key")
            if isinstance(key, str):
                sa.preview(key)
            else:
                log_event("panel_action_malformed")
        elif action == "sel_copy":
            sa.copy()
        elif action == "sel_paste":
            sa.paste_over()
        else:
            sa.close()
