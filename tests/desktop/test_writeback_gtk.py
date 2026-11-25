"""Write-back into a real GTK window. Run inside the GNOME session:

    .venv/bin/pytest -m desktop tests/desktop/test_writeback_gtk.py -v

The tests open their own window (a GtkSourceView, the widget gedit uses) and send keys only
while that window is the active one. The window's buffer, printed on SIGUSR1, is the ground
truth; AT-SPI is not trusted to judge its own writes.

Side effect: `test_paste_fix_restores_clipboard` pastes through the real CLIPBOARD (the
application reads no other selection). It puts nothing of its own there except the
replacement, which only the test window may read; afterwards the engine's adapter holds the
user's previous content, and with no clipboard manager in the session that content is gone
once pytest exits. Do not hold a key down while these run.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from glance.config import AppCfg
from glance.protocol import ApplyRequest, Edit, text_sha256
from tests.desktop.conftest import pump as _pump
from tests.desktop.test_capture_gtk import Desktop, Lines, _stop

pytestmark = pytest.mark.desktop

APP = Path(__file__).with_name("gtk_app.py")
TEXT = "Teh quick brown fox jump over the lazy dog"


@pytest.fixture
def rig():
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    from glance.capture.atspi_port import AtspiPort
    from glance.writeback.clipboard_xlib import XlibClipboard
    from glance.writeback.engine import WriteBackEngine
    from glance.writeback.keys_xlib import XlibKeys

    app = subprocess.Popen([sys.executable, str(APP), "--text", TEXT, "--undo"],
                           stdout=subprocess.PIPE, text=True)
    keys = clip = port = None
    try:
        out = Lines(app)
        assert out.wait_for(lambda line: line == "READY", timeout=10), "gtk_app not ready"
        desktop = Desktop()
        _pump(0.3)  # Wnck must know the new window before it can activate it
        assert desktop.activate(app.pid), "could not give the test window focus"
        port = AtspiPort()  # not started: focused() finds the field by walking the tree
        _pump(0.4)
        keys, clip = XlibKeys(), XlibClipboard()
        engine = WriteBackEngine(port.focused, keys, clip, lambda wm_class: AppCfg(), time.sleep)

        def buffer() -> str:
            seen = len(out.lines)
            app.send_signal(signal.SIGUSR1)
            line = out.wait_for(lambda text: text, timeout=2.0, start=seen)
            assert line is not None, "gtk_app did not print its buffer"
            return json.loads(line)

        def request(edit: Edit, method: str = "auto") -> ApplyRequest:
            assert desktop.active_pid() == app.pid, "test window is not active; nothing sent"
            field = port.focused()
            for _ in range(4):
                if field is not None:
                    break
                _pump(0.5)
                field = port.focused()
            assert field is not None, "no focused text field found"
            text = field.read().text
            assert text == buffer(), "AT-SPI and the buffer disagree before the write"
            return ApplyRequest("t1", field.key, text_sha256(text), edit, method)

        yield SimpleNamespace(app=app, engine=engine, keys=keys, clip=clip, desktop=desktop,
                              buffer=buffer, request=request)
    finally:
        if keys is not None:
            keys.close()
        if clip is not None:
            clip.close()
        if port is not None:
            port.stop()
        _stop(app)
        # Wnck is one object for the whole test process: it has to see this window go
        # before the next test's window appears, or it keeps reporting the dead one.
        _pump(0.4)


def test_type_fix_in_textview(rig):
    result = rig.engine.apply(rig.request(Edit(0, 3, "The")))
    assert result.status == "verified", result
    assert rig.buffer() == "The" + TEXT[3:]


def test_fix_in_the_middle_keeps_everything_else(rig):
    start = TEXT.index("jump")
    result = rig.engine.apply(rig.request(Edit(start, start + 4, "jumps")))
    assert result.status == "verified", result
    assert rig.buffer() == TEXT.replace("jump", "jumps")


def test_selection_by_keys(rig):
    engine = rig.engine
    engine._app_cfg = lambda wm_class: AppCfg(select="keys")
    result = engine.apply(rig.request(Edit(0, 3, "The")))
    assert result.status == "verified", result
    assert rig.buffer() == "The" + TEXT[3:]


def test_paste_fix_restores_clipboard(rig):
    from glance.writeback.clipboard_xlib import XlibClipboard

    # No marker goes on the real clipboard (it would end up in the clipboard history):
    # whatever the session holds right now is the "before", compared target by target.
    reader = XlibClipboard()
    try:
        before = reader.save()
        if before is None:
            pytest.skip("the session's clipboard content cannot be snapshotted")
        new = ("The quick brown fox jumps over the lazy dog. " * 3)[:120]
        assert len(new) == 120
        result = rig.engine.apply(rig.request(Edit(0, len(TEXT), new)))
        assert result.status == "verified", result
        assert rig.buffer() == new
        after = reader.save()
        assert after is not None and after.payload == before.payload
    finally:
        reader.close()


def test_single_ctrl_z_restores(rig):
    # A pasted replace is one undo step in a GtkSourceView; a typed one is two (S4.4).
    result = rig.engine.apply(rig.request(Edit(0, 3, "The"), method="paste"))
    assert result.status == "verified", result
    assert rig.buffer() == "The" + TEXT[3:]
    assert rig.desktop.active_pid() == rig.app.pid
    rig.keys.chord("ctrl", "z")
    time.sleep(0.2)
    assert rig.buffer() == TEXT


def test_revert_last_restores_a_typed_fix(rig):
    result = rig.engine.apply(rig.request(Edit(0, 3, "The")))
    assert result.status == "verified", result
    assert rig.desktop.active_pid() == rig.app.pid
    assert rig.engine.revert_last().status == "reverted"
    assert rig.buffer() == TEXT


def test_stale_request_changes_nothing(rig):
    req = rig.request(Edit(0, 3, "The"))
    stale = ApplyRequest(req.apply_id, req.field_id, text_sha256(TEXT + "x"), req.edit, "auto")
    assert rig.engine.apply(stale).status == "stale"
    assert rig.buffer() == TEXT


def test_refused_while_a_key_is_held(rig):
    from Xlib import XK, X, display
    from Xlib.ext import xtest

    req = rig.request(Edit(0, 3, "The"))
    dpy = display.Display()
    shift = dpy.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
    xtest.fake_input(dpy, X.KeyPress, shift)
    dpy.sync()
    try:
        started = time.monotonic()
        result = rig.engine.apply(req)
        waited = time.monotonic() - started
    finally:
        xtest.fake_input(dpy, X.KeyRelease, shift)
        dpy.sync()
        dpy.close()
    assert result.status == "refused" and "release" in result.detail
    assert 1.4 < waited < 2.5
    assert rig.buffer() == TEXT
