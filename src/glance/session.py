"""Which desktop session Glance runs in: X11, GNOME on Wayland, or one it does not support."""
from __future__ import annotations

from collections.abc import Mapping

X11 = "x11"
GNOME_WAYLAND = "gnome-wayland"
GNOME_MIN, GNOME_MAX = 46, 50  # the GNOME Shell versions the extension declares
SUPPORTED = ("an X11 session, or GNOME Shell "
             f"{GNOME_MIN}–{GNOME_MAX} on Wayland (Ubuntu's default session)")


def is_gnome(env: Mapping[str, str]) -> bool:
    """XDG_CURRENT_DESKTOP names GNOME (Ubuntu says `ubuntu:GNOME`)."""
    desktops = env.get("XDG_CURRENT_DESKTOP", "") or env.get("XDG_SESSION_DESKTOP", "")
    return any(d.strip().casefold() == "gnome" or d.strip().casefold().startswith("gnome-")
               for d in desktops.replace(";", ":").split(":"))


def session_kind(env: Mapping[str, str]) -> str | None:
    """X11 (a DISPLAY in an x11 session), GNOME_WAYLAND, or None for anything else
    (another Wayland desktop, no display at all)."""
    kind = env.get("XDG_SESSION_TYPE", "")
    if kind == "x11" and env.get("DISPLAY"):
        return X11
    if kind == "wayland" and env.get("WAYLAND_DISPLAY") and is_gnome(env):
        return GNOME_WAYLAND
    return None


def gnome_version_supported(major: int | None) -> bool:
    return major is not None and GNOME_MIN <= major <= GNOME_MAX
