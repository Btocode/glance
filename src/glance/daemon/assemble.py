"""The features that sit on top of the Controller and the AiCoordinator: autocomplete, the
send-guard, select-anything and the glance-edit window. No GTK here: the windows and the X
adapters come in as arguments, so the wiring itself is tested with fakes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from glance.daemon.wiring import RunBg, guard_panel, thread_runner


@dataclass
class Features:
    autocomplete: Any
    guard: Any
    selection: Any
    actions_bar: Any  # selection actions: the mark, its action bar and the result panel
    actions: dict[str, Callable[[], None]]  # hotkey name -> action, for bind_hotkeys


def attach_features(*, controller: Any, ai: Any, supervisor: Any, cfg: Callable[[], Any],
                    grammar: Any, issue_filter: Any, ghost: Any, hotkeys: Any, clock: Any,
                    run_ai: RunBg, language: Callable[[str], bool], selection_port: Any,
                    overlay: Any, capture: Any, notifier: Any, selection_watch: Any = None,
                    remember: Callable[[str, Any], None] = lambda key, value: None,
                    selection_hint: Callable[[], str | None] = lambda: None) -> Features:
    """Build and attach the features; every hook is appended, none replaced.

    `run_ai` runs work on the AI threads (never the grammar pool). `overlay` is the one
    select-anything draws on (it holds the Escape grab); `cfg()` is the whole Config.
    """
    from glance.features.autocomplete import Autocomplete
    from glance.features.select_anything import SelectAnything
    from glance.features.selection_actions import NoWatch, SelectionActions
    from glance.features.sendguard import SendGuard
    from glance.privacy.gate import PrivacyGate

    autocomplete = Autocomplete(controller, supervisor, ai.resolver, ghost, hotkeys, clock,
                                run_ai, cfg, language=language)
    autocomplete.attach()

    guard = SendGuard(controller, ai, issue_filter.ignored)
    guard.attach()
    controller.extra_panel.append(guard_panel(guard))

    sa = SelectAnything(controller, selection_port, lambda: PrivacyGate(cfg()), cfg, grammar,
                        issue_filter, supervisor, ai.resolver, overlay, capture, notifier,
                        run_ai, run_read=thread_runner(clock.post, "glance-selection"))
    controller.add_listener(sa)
    controller.extra_status.append(lambda: {"selection_open": sa.is_open})

    bar = SelectionActions(
        controller=controller, select_anything=sa, selection=selection_port,
        watch=selection_watch or NoWatch(), cfg=cfg, overlay=overlay, clock=clock,
        notifier=notifier, run_bg=run_ai, run_read=thread_runner(clock.post, "glance-selection"),
        supervisor=supervisor, resolver=ai.resolver, grammar=grammar, issue_filter=issue_filter,
        remember=remember, hint=selection_hint)
    controller.add_listener(bar)
    controller.extra_status.append(lambda: {"selection_actions": bar.watching})
    bar.sync()

    def select() -> None:
        # The action bar on the current selection, without the mark (works with the mark
        # switched off); one panel at a time: it closes the field's.
        bar.hotkey()

    return Features(autocomplete, guard, sa, bar,
                    {"complete": autocomplete.trigger, "selection": select})
