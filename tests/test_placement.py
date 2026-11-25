from glance.protocol import Rect
from glance.ui.placement import place

MON = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]


def test_below_right_of_caret():
    assert place(Rect(100, 100, 1, 18), None, None, None, (22, 22), MON) == (105, 122)


def test_zero_width_caret_with_height_is_valid():
    assert place(Rect(100, 100, 0, 18), None, None, None, (22, 22), MON) == (104, 122)


def test_clamped_to_caret_monitor():
    assert place(Rect(1915, 1070, 1, 18), None, None, None, (22, 22), MON) == (1898, 1058)


def test_second_monitor_stays_on_second():
    x, _ = place(Rect(1925, 500, 1, 18), None, None, None, (22, 22), MON)
    assert x >= 1920


def test_second_monitor_right_edge_clamps_to_second():
    assert place(Rect(3835, 500, 1, 18), None, None, None, (22, 22), MON) == (3818, 522)


def test_garbage_caret_rect_falls_back():
    for bad in (Rect(0, 0, 0, 0), Rect(-1, -1, 0, 0), Rect(50, 50, 1, 5000), Rect(9000, 9000, 1, 18)):
        assert place(bad, None, Rect(200, 200, 400, 100), None, (22, 22), MON) == (574, 274)


def test_garbage_caret_falls_to_prev_char_first():
    assert place(Rect(0, 0, 0, 0), Rect(100, 100, 8, 18), Rect(200, 200, 400, 100), None, (22, 22), MON) == (112, 122)


def test_garbage_prev_char_falls_to_field():
    assert place(None, Rect(0, 0, 0, 0), Rect(200, 200, 400, 100), None, (22, 22), MON) == (574, 274)


def test_fallback_order_prev_char_then_field_then_window_then_none():
    assert place(None, Rect(100, 100, 8, 18), None, None, (22, 22), MON) == (112, 122)
    assert place(None, None, None, Rect(0, 0, 800, 600), (22, 22), MON) == (774, 574)
    assert place(None, None, None, None, (22, 22), MON) is None


def test_tall_field_is_usable_but_tall_caret_is_not():
    assert place(None, None, Rect(100, 100, 400, 600), None, (22, 22), MON) == (474, 674)


def test_field_off_screen_falls_to_window():
    assert place(None, None, Rect(9000, 9000, 100, 100), Rect(0, 0, 800, 600), (22, 22), MON) == (774, 574)


def test_negative_origin_monitor():
    mons = [Rect(-1920, 0, 1920, 1080), Rect(0, 0, 1920, 1080)]
    assert place(Rect(-100, 100, 1, 18), None, None, None, (22, 22), mons) == (-95, 122)
    assert place(Rect(-3, 100, 1, 18), None, None, None, (22, 22), mons) == (-22, 122)


def test_no_monitors_gives_none():
    assert place(Rect(100, 100, 1, 18), None, None, None, (22, 22), []) is None


def test_clamp_to_monitor_keeps_a_big_window_on_the_anchor_monitor():
    from glance.ui.placement import clamp_to_monitor

    assert clamp_to_monitor(100, 100, (420, 480), MON) == (100, 100)
    assert clamp_to_monitor(1800, 900, (420, 480), MON) == (1500, 600)
    assert clamp_to_monitor(3700, 1000, (420, 480), MON) == (3420, 600)  # second monitor
    assert clamp_to_monitor(1925, 10, (420, 480), MON) == (1925, 10)  # never pulled to the first


def test_clamp_to_monitor_point_outside_every_monitor_uses_the_nearest():
    from glance.ui.placement import clamp_to_monitor

    assert clamp_to_monitor(4000, 500, (100, 100), MON) == (3740, 500)
    assert clamp_to_monitor(-50, -50, (100, 100), [Rect(-1920, 0, 1920, 1080)] + MON) == (-100, 0)
    assert clamp_to_monitor(5, 5, (100, 100), []) == (5, 5)


# ---------------------------------------------------------------------- the strip

from glance.ui.placement import STRIP_MAX_W, STRIP_MIN_W, Anchor, Placed, place_strip

FIELD = Rect(200, 300, 500, 120)


def test_strip_under_the_caret_line_aligned_with_the_field():
    a = Anchor(caret=Rect(260, 330, 1, 18), field=FIELD)
    assert place_strip(a, 40, MON) == Placed(200, 354, 500, False)


def test_strip_width_is_the_field_width_within_limits():
    narrow = Anchor(caret=Rect(110, 330, 1, 18), field=Rect(100, 300, 120, 40))
    assert place_strip(narrow, 40, MON).w == STRIP_MIN_W
    wide = Anchor(caret=Rect(110, 330, 1, 18), field=Rect(100, 300, 1500, 40))
    assert place_strip(wide, 40, MON).w == STRIP_MAX_W


def test_strip_flips_above_the_line_when_there_is_no_room_below():
    a = Anchor(caret=Rect(260, 1040, 1, 18), field=Rect(200, 1000, 500, 70))
    p = place_strip(a, 40, MON)
    assert p.above and p.y + 40 <= 1040  # never over the line being typed


def test_strip_falls_back_to_previous_char_field_then_window():
    prev = Anchor(prev_char=Rect(260, 330, 8, 18), field=FIELD)
    assert place_strip(prev, 40, MON) == Placed(200, 354, 500, False)
    field = Anchor(caret=Rect(0, 0, 0, 0), field=FIELD)
    assert place_strip(field, 40, MON) == Placed(200, 426, 500, False)
    window = Anchor(window=Rect(50, 50, 800, 600))
    assert place_strip(window, 40, MON) == Placed(50, 656, 720, False)
    assert place_strip(Anchor(), 40, MON) is None


def test_strip_clamped_to_its_monitor():
    a = Anchor(caret=Rect(1850, 330, 1, 18), field=Rect(1800, 300, 400, 40))
    p = place_strip(a, 40, MON)
    assert p.x + p.w <= 1920 or p.x >= 1920
    a = Anchor(caret=Rect(3700, 330, 1, 18), field=Rect(3500, 300, 600, 40))
    p = place_strip(a, 40, MON)
    assert p.x + p.w == 3840


def test_anchor_moves_with_its_window():
    a = Anchor(caret=Rect(260, 330, 1, 18), field=FIELD, window=Rect(0, 0, 900, 700), pid=7)
    b = a.moved(40, -10)
    assert (b.caret, b.field, b.window, b.prev_char, b.pid) == (
        Rect(300, 320, 1, 18), Rect(240, 290, 500, 120), Rect(40, -10, 900, 700), None, 7)
    assert place_strip(b, 40, MON) == Placed(240, 344, 500, False)
