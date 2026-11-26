"""macOS adapters: AX capture, Quartz write-back, AppKit overlay, Carbon hotkeys, launchd.

The modules without an `objc`/AppKit import (`geometry`, `ax_text`, `ax_roles`, `keyplan`,
`hotkeys_mac`, `launchd`, `power`) are pure and tested on Linux; the rest import PyObjC
lazily and run only on a Mac. See docs/superpowers/specs/2026-11-26-glance-macos-design.md.
"""
