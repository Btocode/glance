"""Glance Shot's glue: the GNOME Shell adapter (with a fake bus), the extension's caller
check, the switch that follows `[shot] enabled`, `glance shot` and the shortcut tables."""
from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from glance.config import Config, ShotCfg
from glance.daemon.control import CtlRouter
from glance.daemon.shot_wiring import SHOT_OFF, ShotSwitch, password_rect, shot_request
from glance.protocol import Rect
from glance.shellbus import DAEMON_METHODS, SHELL_METHODS, ShellUnavailable
from glance.shot import bar_model as bm
from glance.shot.image import encode_png
from glance.ui.hotkeys import parse_chord
from glance.ui.shortcut_table import GLANCE_ACTIONS, LINUX_RESERVED, MAC_RESERVED
from glance.ui.shot_shell import ShellShot
from tests.fakes import FakeHotkeys

REPO = Path(__file__).resolve().parents[1]


class FakeBus:
    def __init__(self, png: bytes | None = None, available: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.png = png
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    def call(self, method, payload=None, timeout_ms=1500):
        if not self._available:
            raise ShellUnavailable(method)
        self.calls.append((method, payload or {}))
        if method == "ShotCapture":
            if self.png is None:
                return {"ok": False}
            return {"ok": True, "png": base64.b64encode(self.png).decode()}
        return getattr(self, "answers", {}).get(method, {"ok": True})


def test_shell_shot_picks_captures_and_reports():
    png = encode_png(b"\x10" * (6 * 4 * 3), 6, 4)
    bus = FakeBus(png)
    acts, results = [], []
    s = ShellShot(bus, lambda: {"surface": "#fff"}, acts.append)
    s.pick(results.append)
    method, payload = bus.calls[0]
    assert method == "ShotPick" and payload["tokens"] == {"surface": "#fff"}
    assert "Esc" in payload["hint"] and payload["look"]["mark_dir"]
    s.on_picked({"rect": [10, 20, 3, 2], "how": "drag", "windows": [
        {"rect": [0, 0, 50, 50], "wm_class": "KeePassXC", "title": "db", "pid": 7}]})
    assert bus.calls[1] == ("ShotCapture", {"x": 10, "y": 20, "w": 3, "h": 2})
    r = results[0]
    assert r.shot.png == png and (r.shot.width, r.shot.height) == (6, 4)  # device pixels
    assert r.shot.rect == Rect(10, 20, 3, 2)
    assert r.windows[0].wm_class == "KeePassXC" and r.windows[0].rect == Rect(0, 0, 50, 50)
    s.on_picked({"rect": [1, 1, 5, 5]})  # nothing asked for: ignored
    assert len(results) == 1


def test_shell_shot_cancel_and_failures():
    results = []
    s = ShellShot(FakeBus(None), dict, lambda _k: None)
    s.pick(results.append)
    s.on_picked({"rect": None, "how": "cancel"})
    s.pick(results.append)
    s.on_picked({"rect": [0, 0, 10, 10]})  # the capture failed
    assert results == [None, None]
    s = ShellShot(FakeBus(available=False), dict, lambda _k: None)
    s.pick(results.append)
    assert results == [None, None, None]  # no extension: nothing, at once


def test_shell_shot_bar_clicks_and_clipboard():
    bus = FakeBus()
    acts = []
    s = ShellShot(bus, lambda: {"surface": "#fff"}, acts.append)
    view = bm.ShotBarView("3 × 2", bm.buttons(vision=True, ai=True, images="ask", ocr=True,
                                              private=False))
    s.show_bar(view, Rect(1, 2, 3, 4))
    method, payload = bus.calls[-1]
    assert method == "ShotBar" and payload["region"] == [1, 2, 3, 4]
    assert payload["view"]["buttons"][0]["label"] == "Text"
    assert s.on_clicked({"target": "shot", "args": {"action": "save"}}) is True
    assert s.on_clicked({"target": "action", "args": {"action": "close"}}) is False
    assert acts == ["save"]
    assert s.copy_image(b"PNG") is True
    assert bus.calls[-1] == ("ShotClipboard", {"png": "UE5H"})
    assert payload["words"]["hint_sub"] == bm.HINT_SUB
    assert s.ask_point() is None  # the extension did not say where the card is
    bus.answers = {"ShotBar": {"ok": True, "rect": [600, 40, 460, 300]}}
    s.show_bar(view, Rect(1, 2, 3, 4))
    assert s.ask_point() == (616, 144)  # over the card's body
    s.hide_bar()
    assert bus.calls[-1] == ("ShotBar", {"hide": True})
    assert s.ask_point() is None


def test_bus_names_and_extension_caller_check():
    for m in ("ShotPick", "ShotCancel", "ShotCapture", "ShotBar", "ShotClipboard"):
        assert m in SHELL_METHODS
    assert "ShotPicked" in DAEMON_METHODS
    ext = (REPO / "extension" / "extension.js").read_text()
    shot = (REPO / "extension" / "shot.js").read_text()
    # every Shot method is glanced's only: not glance-capture, not anybody else
    assert "DAEMON_ONLY.has(name) ? role === 'daemon' : role !== null" in ext
    for m in ("ShotPick", "ShotCancel", "ShotCapture", "ShotBar", "ShotClipboard"):
        assert f"'{m}'" in shot.split("DAEMON_ONLY")[1]
        assert f"'{m}'" in ext
    assert "screenshot_area" in shot and "MemoryOutputStream" in shot  # in memory, no file
    assert "'shot'" in ext.split("SCHEMA_KEYS")[1][:300]
    schema = (REPO / "extension" / "schemas" /
              "org.gnome.shell.extensions.glance.gschema.xml").read_text()
    assert '<key name="shot" type="as">' in schema and "&lt;Alt&gt;c" in schema


# ------------------------------------------------------------------ the switch


class FakeShot:
    def __init__(self, on: bool = True) -> None:
        self.on, self.closed, self.triggers = on, 0, 0

    def trigger(self) -> bool:
        self.triggers += 1
        return self.on

    def close(self) -> None:
        self.closed += 1


class FakeTray:
    def __init__(self) -> None:
        self.shot = "unset"

    def set_shot(self, fn) -> None:
        self.shot = fn


def _cfg(on: bool) -> Config:
    return dataclasses.replace(Config(), shot=ShotCfg(enabled=on))


def test_switch_follows_the_shot_feature_switch(tmp_path):
    shot, tray, hotkeys = FakeShot(), FakeTray(), FakeHotkeys()
    actions: dict = {}
    rebinds = []
    sw = ShotSwitch(shot, actions, tray, lambda: rebinds.append(1), hotkeys)
    sw.apply(_cfg(True), rebind=False)
    assert actions["shot"] == shot.trigger and tray.shot == shot.trigger and rebinds == []
    hotkeys.grab("shot", "Super+Alt+c", shot.trigger)
    sw.apply(_cfg(True))
    assert rebinds == []  # unchanged: nothing to do
    sw.apply(_cfg(False))
    assert "shot" not in actions and tray.shot is None and shot.closed == 1
    assert "shot" not in hotkeys.grabbed() and rebinds == [1]
    sw.apply(_cfg(True))
    assert "shot" in actions and rebinds == [1, 1]
    # `glance features shot off` / the tray's "Features ▸ Glance Shot" write the same key
    from glance.config import load_config, update_config
    from glance.feature_switches import is_on, set_feature

    path = tmp_path / "config.toml"
    path.write_text("[shot]\nsave_dir = \"~/Shots\"\n")
    assert set_feature(update_config, path, "shot", False)
    cfg = load_config(path)
    assert cfg.shot.enabled is False and not is_on(cfg, "shot") and cfg.shot.save_dir == "~/Shots"
    sw.apply(cfg)
    assert "shot" not in actions and tray.shot is None


def test_shot_request_and_ctl_router():
    on, off = FakeShot(True), FakeShot(False)
    assert shot_request(on)({}) == {"ok": True}
    assert shot_request(off)({}) == {"ok": False, "error": SHOT_OFF}
    replies = []
    router = CtlRouter(SimpleNamespace(), lambda *_a: None, extra={"shot": shot_request(on)})
    router.handle("shot", {"id": 7}, replies.append)
    assert replies == [{"ok": True, "id": 7}] and on.triggers == 2


def test_password_rect():
    snap = SimpleNamespace(gated="password", field_rect=Rect(1, 2, 3, 4),
                           window_rect=Rect(0, 0, 9, 9))
    c = SimpleNamespace(current=snap)
    assert password_rect(c)() == Rect(1, 2, 3, 4)
    c.current = SimpleNamespace(gated="password", field_rect=None, window_rect=Rect(0, 0, 9, 9))
    assert password_rect(c)() == Rect(0, 0, 9, 9)
    c.current = SimpleNamespace(gated=None, field_rect=Rect(1, 1, 1, 1), window_rect=None)
    assert password_rect(c)() is None


def test_cli_shot(monkeypatch, capsys):
    from glance import cli

    asked = []

    def fake_ask(msg_type, payload, timeout_s=3.0):
        asked.append(msg_type)
        return ({"ok": False, "error": SHOT_OFF}, 1) if asked.count("shot") > 1 \
            else ({"ok": True}, 0)

    monkeypatch.setattr(cli, "_ask", fake_ask)
    assert cli.main(["shot"]) == 0
    assert cli.main(["shot"]) == 1
    assert "Glance Shot is off" in capsys.readouterr().err


# ------------------------------------------------------------------ the shortcut


def test_shot_chord_is_free_on_every_desktop_table():
    from glance.platform.macos.hotkeys_mac import MAC_DEFAULTS, mac_hotkeys

    chord = Config().general.hotkeys["shot"]
    assert chord == "Super+Alt+c" and MAC_DEFAULTS["shot"] == "Ctrl+Alt+c"
    assert mac_hotkeys({"shot": chord}) == {"shot": "Ctrl+Alt+c"}
    linux = {parse_chord(c)[0] | {parse_chord(c)[1].casefold()} for c, _ in LINUX_RESERVED}
    mods, key = parse_chord(chord)
    assert mods | {key.casefold()} not in linux
    mac = {parse_chord(c)[0] | {parse_chord(c)[1].casefold()} for c, _ in MAC_RESERVED}
    mods, key = parse_chord("Ctrl+Alt+c")
    assert mods | {key.casefold()} not in mac
    assert ("shot", "Take a Glance Shot (capture a region)") in GLANCE_ACTIONS
    doc = (REPO / "docs" / "shortcuts.md").read_text()
    assert "| Take a Glance Shot (capture a region) | `shot` | `Super+Alt+c` | ⌃⌥C |" in doc


def test_disabled_config_keeps_the_hotkey_name_but_no_action():
    cfg = dataclasses.replace(Config(), shot=ShotCfg(enabled=False))
    assert cfg.general.hotkeys["shot"]  # the chord stays configured; the switch binds it


def test_vision_reading_order_is_pure():
    from glance.platform.macos.ocr import lines_in_reading_order

    assert lines_in_reading_order([(0.5, 0.8, "right"), (0.1, 0.8, "left"),
                                   (0.1, 0.9, "top"), (0.1, 0.2, " ")]) == \
        ["top", "left", "right"]


@pytest.mark.parametrize("mode", ["auto", "ocrs", "tesseract", "off"])
def test_ocr_modes_are_known(mode):
    from glance.config import SHOT_OCR

    assert mode in SHOT_OCR


def test_doctor_does_not_want_the_shot_chord_when_shot_is_off(tmp_path):
    from glance.config import GeneralCfg
    from glance.doctor.checks import Hotkeys

    cfg = tmp_path / "config.toml"
    cfg.write_text("[shot]\nenabled = false\n")
    grabbed = ",".join(n for n in GeneralCfg().hotkeys if n != "shot")
    env = SimpleNamespace(config_file=cfg, ctl=lambda t, p: {"ok": True,
                                                             "hotkeys_grabbed": grabbed},
                          gsettings=lambda *a: "")
    from glance.doctor import checks

    orig = checks._desktop_shortcuts
    checks._desktop_shortcuts = lambda _env: set()
    try:
        r = Hotkeys().run(env)
        assert r.status == "ok", r.detail
        cfg.write_text("")
        r = Hotkeys().run(env)
        assert r.status == "warn" and "shot (Super+Alt+c)" in r.detail
    finally:
        checks._desktop_shortcuts = orig
