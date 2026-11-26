"""Glance Shot: capture a screen region and act on it (copy, save, extract the text, explain).

The pure parts live here (the image in memory, the picker's geometry, the bar's model, the
one-time notice, the privacy check, OCR and the controller); the renderers are per platform:
`glance.ui.shot_gtk` (X11), `glance.ui.shot_shell` (GNOME Wayland, drawn by the extension) and
`glance.platform.macos.shot`.

An image lives in memory only: nothing is written to disk unless the user clicks Save, and it
is dropped when the bar closes. It goes to an AI only when the user clicks Explain or Ask.
"""
