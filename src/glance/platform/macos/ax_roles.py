"""What an AX element is, in the terms the privacy gate and the tracker already use. Pure.

The gate (`privacy/gate.py`) is shared and unchanged: it knows AT-SPI role names
(`password text`, `entry`, `text`) and states (`editable`, `protected`). This module maps
`AXRole` / `AXSubrole` onto those, and adds the macOS bundle ids to the deny lists.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

SECURE = "AXSecureTextField"
WEB_AREA = "AXWebArea"

# AXRole -> the AT-SPI role name the gate and the rest of Glance know.
_ROLES = {
    "AXTextField": "entry",
    "AXTextArea": "text",
    "AXComboBox": "combo box",
    "AXSearchField": "entry",
}
# Roles a web page's editable element can have (contenteditable is an AXGroup / AXWebArea
# child with AXEditableAncestor; the focused element itself is what is read).
_WEB_EDITABLE = {"AXGroup", "AXWebArea", "AXStaticText", "AXTextArea", "AXTextField"}


def is_secure(role: str | None, subrole: str | None) -> bool:
    """A password field: never read. NSSecureTextField reports subrole AXSecureTextField
    (role AXTextField); some toolkits put it in the role itself."""
    return SECURE in ((role or ""), (subrole or ""))


def role_name(role: str | None, subrole: str | None) -> str:
    """The gate's role name for an AX role/subrole. Secure fields are `password text`, which
    the gate refuses before anything is read."""
    if is_secure(role, subrole):
        return "password text"
    if subrole == "AXSearchField":
        return "entry"
    return _ROLES.get(role or "", "text" if role in _WEB_EDITABLE else (role or "unknown"))


def is_text_role(role: str | None, subrole: str | None, editable_ancestor: bool) -> bool:
    """Whether a focused element can be a writable text field at all."""
    if is_secure(role, subrole):
        return True  # it is a field; the gate refuses it by role
    if role in _ROLES:
        return True
    return editable_ancestor and role in _WEB_EDITABLE


def states(*, editable: bool, secure: bool, focused: bool = True,
           multi_line: bool = False) -> frozenset[str]:
    """AT-SPI-style states for the gate: `editable` only when the value is settable."""
    out = set()
    if editable:
        out.add("editable")
    if secure:
        out.add("protected")
    if focused:
        out.add("focused")
    out.add("multi-line" if multi_line else "single-line")
    return frozenset(out)


# ---------------------------------------------------------------------- privacy defaults

# Substrings of bundle ids or app names. Matching is case-insensitive substring, like Linux.
MAC_APP_DENYLIST: tuple[str, ...] = (
    "com.1password", "com.agilebits", "com.bitwarden", "org.keepassxc", "com.apple.keychainaccess",
    "com.apple.Passwords", "com.lastpass", "com.dashlane", "in.sinew.Enpass",
    "com.apple.ScreenSharing", "com.microsoft.rdc", "com.anydesk", "com.teamviewer",
    "com.rustdesk", "com.realvnc", "com.parallels", "com.vmware.fusion", "com.utmapp",
    "com.apple.loginwindow", "com.apple.SecurityAgent", "com.apple.systempreferences",
)
MAC_BROWSERS: tuple[str, ...] = (
    "com.apple.Safari", "com.google.Chrome", "company.thebrowser.Browser", "com.brave.Browser",
    "org.mozilla.firefox", "com.microsoft.edgemac", "com.vivaldi.Vivaldi", "com.operasoftware",
    "org.chromium.Chromium",
)
MAC_TERMINALS: tuple[str, ...] = (
    "com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty", "net.kovidgoyal.kitty",
    "org.alacritty", "com.github.wez.wezterm", "dev.warp.Warp",
)
# Apps whose tree appears only when asked: Electron reads AXManualAccessibility, Chromium
# browsers AXEnhancedUserInterface (the macOS side of --force-renderer-accessibility).
CHROMIUM_BUNDLES: tuple[str, ...] = (
    "com.google.Chrome", "com.brave.Browser", "company.thebrowser.Browser",
    "com.microsoft.edgemac", "com.vivaldi.Vivaldi", "org.chromium.Chromium",
)


def _merge(base: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    seen = {b.casefold() for b in base}
    return base + tuple(e for e in extra if e.casefold() not in seen)


def mac_config(cfg: Any) -> Any:
    """`cfg` with the macOS bundle ids added to the privacy lists (never removed from)."""
    p = cfg.privacy
    return replace(cfg, privacy=replace(
        p,
        app_denylist=_merge(p.app_denylist, MAC_APP_DENYLIST),
        browser_wm_classes=_merge(p.browser_wm_classes, MAC_BROWSERS),
        terminal_wm_classes=_merge(p.terminal_wm_classes, MAC_TERMINALS),
    ))


def wants_manual_accessibility(bundle_id: str) -> str | None:
    """The attribute to set on the app element so its web/Electron tree is exposed:
    `AXEnhancedUserInterface` for Chromium browsers, `AXManualAccessibility` otherwise
    (Electron honours it; native Cocoa apps ignore it)."""
    if any(bundle_id.casefold() == b.casefold() for b in CHROMIUM_BUNDLES):
        return "AXEnhancedUserInterface"
    if bundle_id.casefold() in {"com.apple.safari", "org.mozilla.firefox"}:
        return None
    return "AXManualAccessibility"
