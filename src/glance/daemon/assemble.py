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
    actions: dict[str, Callable[[], None]]  # hotkey name -> action, for bind_hotkeys


def attach_features(*, controller: Any, ai: Any, supervisor: Any, cfg: Callable[[], Any],
                    grammar: Any, issue_filter: Any, ghost: Any, hotkeys: Any, clock: Any,
                    run_ai: RunBg, language: Callable[[str], bool], selection_port: Any,
                    overlay: Any, capture: Any, notifier: Any) -> Features:
    """Build and attach the features; every hook is appended, none replaced.

    `run_ai` runs work on the AI threads (never the grammar pool). `overlay` is the one
    select-anything draws on (it holds the Escape grab); `cfg()` is the whole Config.
    """
    from glance.features.autocomplete import Autocomplete
    from glance.features.select_anything import SelectAnything
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

    def select() -> None:
        controller.close_panel()  # one panel at a time: the selection's replaces the field's
        sa.trigger()

    return Features(autocomplete, guard, sa,
                    {"complete": autocomplete.trigger, "selection": select})
