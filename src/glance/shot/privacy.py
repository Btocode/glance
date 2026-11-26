"""Is a private window in the shot? Best effort, from the windows' classes and titles.

NOT USED by Glance Shot any more: the maintainer decided (2026-11-26) that a shot is the
user's own deliberate capture and nothing in it is off limits. Kept for a possible opt-in.

A region that overlaps a password field Glance knows of, a denylisted app (password
managers, remote desktops), a private/incognito browser window, or a browser window whose
title looks like a sign-in, bank or checkout page, is still the user's to copy, save or read
locally, but it is never sent to an AI (the bar says why).
"""
from __future__ import annotations

from collections.abc import Iterable

from glance.config import Config
from glance.privacy.gate import _compile, _contains_any
from glance.protocol import Rect
from glance.shot.geometry import overlaps
from glance.ui.ports import WindowInfo


def private_reason(region: Rect, windows: Iterable[WindowInfo], cfg: Config,
                   password: Rect | None = None) -> str | None:
    """Why the region must not go to an AI (password | app_denylist | incognito |
    url_denylist), or None. `windows` are the top-level windows with their frames; the ones
    without a frame are not judged. `password`: a password field's rectangle, if one is
    focused now."""
    if password is not None and overlaps(region, password):
        return "password"
    privacy = cfg.privacy
    patterns = [_compile(p) for p in privacy.url_denylist if p]
    for w in windows:
        if w.rect is None or not overlaps(region, w.rect):
            continue
        if _contains_any(privacy.app_denylist, w.wm_class):
            return "app_denylist"
        if _contains_any(privacy.incognito_title_patterns, w.title):
            return "incognito"
        if _contains_any(privacy.browser_wm_classes, w.wm_class) and any(
                rx.search(w.title) for rx in patterns):
            return "url_denylist"
    return None
