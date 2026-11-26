"""Glance Shot's region picker, the shared part: the image in memory, the geometry, the picker's rules (drag, Enter, Space, Esc), and `[shot]` in the config."""
from __future__ import annotations

import pytest

from glance.config import Config, ShotCfg, load_config
from glance.protocol import Rect
from glance.shot import geometry as geo
from glance.shot.image import (
    Shot,
    bgrx_to_rgb,
    crop,
    encode_png,
    png_size,
    scaled_rect,
)
from glance.shot.picker import Picker
from tests.shot_fakes import decode_png

# ------------------------------------------------------------------ the image


def test_png_round_trip_and_size():
    px = bytes(range(2 * 3 * 3))
    png = encode_png(px, 3, 2)
    assert png_size(png) == (3, 2)
    assert decode_png(png) == (3, 2, 3, px)
    assert decode_png(encode_png(b"\x01\x02", 2, 1, channels=1))[3] == b"\x01\x02"
    with pytest.raises(ValueError):
        encode_png(b"\x00", 3, 2)
    with pytest.raises(ValueError):
        png_size(b"GIF89a....")


def test_bgrx_to_rgb_and_crop():
    raw = bytes([3, 2, 1, 0, 6, 5, 4, 0])  # two pixels, B G R X
    assert bgrx_to_rgb(raw, 2, 1) == bytes([1, 2, 3, 4, 5, 6])
    padded = raw + b"\xff" * 8  # stride 16 for a 2-px row
    assert bgrx_to_rgb(padded, 2, 1, stride=16) == bytes([1, 2, 3, 4, 5, 6])
    img = bytes(range(16))  # 4x4 grey
    assert crop(img, 4, 1, Rect(1, 1, 2, 2)) == bytes([5, 6, 9, 10])
    assert scaled_rect(Rect(110, 60, 50, 20), Rect(100, 50, 400, 300), 2.0) == Rect(20, 20, 100, 40)


def test_shot_repr_has_no_pixels():
    s = Shot(b"\x89PNG secret", 10, 20, Rect(1, 2, 10, 20))
    assert "secret" not in repr(s) and "10x20" in repr(s)


# ------------------------------------------------------------------ geometry and the picker

SCREEN = [Rect(0, 0, 1920, 1080)]
TWO = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1280, 1024)]


def test_drag_rect_any_direction():
    assert geo.drag_rect(100, 200, 50, 20) == Rect(50, 20, 50, 180)
    assert not geo.is_region(Rect(0, 0, 3, 100))
    assert geo.bounds(TWO) == Rect(0, 0, 3200, 1080)
    assert geo.intersect(Rect(0, 0, 10, 10), Rect(20, 20, 5, 5)) is None


def test_label_follows_and_flips_at_the_edges():
    screen = Rect(0, 0, 1920, 1080)
    assert geo.label_position(100, 100, (80, 20), screen) == (116, 118)
    x, y = geo.label_position(1900, 1070, (80, 20), screen)
    assert x + 80 <= 1900 and y + 20 <= 1070


def test_bar_goes_below_then_above_then_inside():
    size = (520, 48)
    assert geo.bar_position(Rect(100, 100, 600, 300), size, SCREEN) == (180, 408)
    x, y = geo.bar_position(Rect(100, 700, 600, 360), size, SCREEN)
    assert y == 700 - 8 - 48
    x, y = geo.bar_position(Rect(0, 0, 1920, 1080), size, SCREEN)
    assert y == 1080 - 48 - 8 and x == 1920 - 520


def test_picker_drag_selects_and_click_does_not():
    p = Picker(SCREEN)
    p.press(10, 10)
    p.release(11, 11)
    assert p.done is None  # a click is not a region
    p.press(100, 100)
    p.motion(300, 250)
    assert p.selection == Rect(100, 100, 200, 150) and p.label() == "200 × 150"
    out = p.release(300, 250)
    assert out is not None and out.region == Rect(100, 100, 200, 150) and out.how == "drag"


def test_picker_drag_is_cut_to_the_screen():
    p = Picker(SCREEN)
    p.press(1800, 1000)
    assert p.release(2500, 1500).region == Rect(1800, 1000, 120, 80)


def test_picker_keys():
    p = Picker(TWO, windows=[Rect(50, 50, 400, 300), Rect(0, 0, 1920, 1080)])
    p.motion(2000, 100)
    assert p.key("Return").region == TWO[1]
    p = Picker(TWO, windows=[Rect(50, 50, 400, 300), Rect(0, 0, 1920, 1080)])
    p.motion(60, 60)
    assert p.hover_window() == Rect(50, 50, 400, 300)
    assert p.key("space").region == Rect(50, 50, 400, 300)
    p = Picker(SCREEN)
    assert p.key("Escape").region is None and p.done.how == "cancel"
    p = Picker(SCREEN, windows=[])
    assert p.key("space") is None  # no window there: keep picking


# ------------------------------------------------------------------ config


def test_shot_config(tmp_path):
    assert Config().shot == ShotCfg()
    assert Config().general.hotkeys["shot"] == "Super+Alt+c"
    p = tmp_path / "config.toml"
    p.write_text('[shot]\nenabled = false\nsave_dir = "~/Shots"\nocr = "tesseract"\n'
                 'ai_images = "nope"\n')
    cfg = load_config(p)
    assert cfg.shot.enabled is False and cfg.shot.save_dir == "~/Shots"
    assert cfg.shot.ocr == "tesseract" and cfg.shot.ai_images == "ask"  # typo: the default
    assert "shot" not in cfg.raw
