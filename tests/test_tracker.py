from dataclasses import replace

import pytest

from glance.capture.tracker import FocusTracker
from glance.config import AppCfg, Config
from glance.privacy.gate import PrivacyGate
from glance.protocol import FOCUS_LOST, SNAPSHOT, FieldSnapshot, Rect
from tests.fakes import FakeA11y, FakeClock, FakeField


@pytest.fixture
def rig():
    clock = FakeClock()
    port = FakeA11y()
    out: list[tuple[str, dict]] = []
    tracker = FocusTracker(port, PrivacyGate(Config()), lambda t, p: out.append((t, p)), clock)
    tracker.start()
    return tracker, port, clock, out


def snap(out, i=-1) -> FieldSnapshot:
    t, p = out[i]
    assert t == SNAPSHOT
    return FieldSnapshot.from_dict(p)


def test_emits_snapshot_after_coalesce_window(rig):
    tracker, port, clock, out = rig
    field = FakeField(text="hello world", caret=5)
    port.focus(field)
    clock.advance(15)
    assert out == []
    clock.advance(1)
    t, p = out[-1]
    s = FieldSnapshot.from_dict(p)
    assert t == SNAPSHOT
    assert (s.text, s.caret, s.field_id, s.gated) == ("hello world", 5, field.key, None)
    assert (s.app, s.wm_class, s.title, s.role, s.pid) == ("gedit", "gedit", "notes", "text", 100)
    assert s.caret_rect == field.caret_rect and s.field_rect == field.field_rect
    assert s.window_rect == field.win_rect
    assert tracker.current_field() is field


def test_burst_of_changes_is_one_read(rig):
    _, port, clock, out = rig
    field = FakeField(text="hello")
    port.focus(field)
    clock.advance(16)
    out.clear()
    reads = field.reads
    for _ in range(10):
        port.change(field)
        clock.advance(1)
    clock.advance(16)
    assert field.reads == reads + 1 and len(out) == 1


def test_focus_and_changes_in_one_window_are_one_read(rig):
    _, port, clock, out = rig
    field = FakeField(text="hello")
    port.focus(field)
    for _ in range(10):
        port.change(field)
    clock.advance(16)
    assert field.reads == 1 and len(out) == 1


def test_seq_increases(rig):
    _, port, clock, out = rig
    field = FakeField(text="a")
    port.focus(field)
    clock.advance(20)
    field.text = "ab"
    port.change(field)
    clock.advance(20)
    assert [snap(out, 0).seq, snap(out, 1).seq] == [1, 2]
    assert snap(out, 1).text == "ab"


def test_password_field_text_is_never_read(rig):
    _, port, clock, out = rig
    field = FakeField(text="hunter2", caret=7)
    field.set_meta(role="password text")
    port.focus(field)
    clock.advance(16)
    port.change(field)
    clock.advance(16)
    s = snap(out)
    assert field.reads == 0 and s.gated == "password" and s.text == ""
    assert s.caret == 0
    assert (s.caret_rect, s.prev_char_rect) == (None, None)
    # Where the field is (not what it holds): its grey icon sits by the field.
    assert s.field_rect == field.field_rect
    assert s.window_rect == field.win_rect
    assert "hunter2" not in repr(out)


def test_gated_snapshots_keep_counting_seq(rig):
    _, port, clock, out = rig
    secret = FakeField(key="100:1:1", text="x")
    secret.set_meta(role="password text")
    port.focus(secret)
    clock.advance(16)
    port.focus(FakeField(key="100:1:2", text="plain"))
    clock.advance(16)
    assert [snap(out, 0).seq, snap(out, 1).seq] == [1, 2]
    assert snap(out, 1).gated is None


def test_blur_emits_focus_lost(rig):
    tracker, port, clock, out = rig
    field = FakeField(text="hello")
    port.focus(field)
    clock.advance(16)
    port.focus(None)
    assert out[-1] == (FOCUS_LOST, {"reason": "blur"})
    assert tracker.current_field() is None
    port.change(field)  # a late event from the field that lost focus
    clock.advance(16)
    assert out[-1] == (FOCUS_LOST, {"reason": "blur"}) and field.reads == 1


def test_blur_before_read_cancels_the_read(rig):
    _, port, clock, out = rig
    field = FakeField(text="hello")
    port.focus(field)
    port.focus(None)
    clock.advance(16)
    assert field.reads == 0 and out == [(FOCUS_LOST, {"reason": "blur"})]


def test_change_from_another_field_is_ignored(rig):
    _, port, clock, out = rig
    field, other = FakeField(key="100:1:1", text="a"), FakeField(key="200:1:9", text="b")
    port.focus(field)
    clock.advance(16)
    port.change(other)
    clock.advance(16)
    assert other.reads == 0 and other.metas == 0 and len(out) == 1


def test_timeout_emits_focus_lost_and_stops_reading(rig):
    tracker, port, clock, out = rig
    field = FakeField(text="hello")
    port.focus(field)
    clock.advance(16)
    field.raise_timeout = True
    port.change(field)
    clock.advance(16)
    assert out[-1] == (FOCUS_LOST, {"reason": "timeout"})
    assert tracker.current_field() is None
    field.raise_timeout = False
    reads, metas, n = field.reads, field.metas, len(out)
    port.change(field)
    clock.advance(16)
    assert (field.reads, field.metas, len(out)) == (reads, metas, n)
    port.focus(field)  # the next focus event revives it
    clock.advance(16)
    assert field.reads == reads + 1 and snap(out).text == "hello"


def test_timeout_during_read_emits_focus_lost(rig):
    _, port, clock, out = rig
    field = FakeField(text="hello")

    def boom():
        from glance.capture.ports import A11yTimeout
        raise A11yTimeout("read")

    field.read = boom
    port.focus(field)
    clock.advance(16)
    assert out == [(FOCUS_LOST, {"reason": "timeout"})]


def test_missing_caret_rect_lowers_confidence(rig):
    _, port, clock, out = rig
    field = FakeField(text="hello")
    port.focus(field)
    clock.advance(16)
    assert snap(out).capture_confidence == 1.0
    field.caret_rect = None
    field.prev_char_rect = Rect(90, 200, 9, 18)
    port.change(field)
    clock.advance(16)
    s = snap(out)
    assert s.capture_confidence == 0.7 and s.caret_rect is None
    assert s.prev_char_rect == Rect(90, 200, 9, 18)


def test_gate_swap_applies_to_next_event(rig):
    tracker, port, clock, out = rig
    field = FakeField(text="hello")
    port.focus(field)
    clock.advance(16)
    assert snap(out).gated is None
    tracker.set_gate(PrivacyGate(Config(apps={"gedit": AppCfg(capture=False)})))
    reads = field.reads
    port.change(field)
    clock.advance(16)
    s = snap(out)
    assert s.gated == "app_disabled" and s.text == "" and field.reads == reads


def test_gate_swap_regates_the_current_field_without_an_event(rig):
    tracker, port, clock, out = rig
    port.focus(FakeField(text="hello"))
    clock.advance(16)
    tracker.set_gate(PrivacyGate(Config(apps={"gedit": AppCfg(capture=False)})))
    clock.advance(16)
    assert snap(out).gated == "app_disabled"


def test_field_longer_than_reported_is_gated_after_the_read(rig):
    # A rich editor's root reports a short length while the caret block is long.
    _, port, clock, out = rig
    field = FakeField(text="x" * 20001)
    field.meta = lambda: replace(field._meta, text_len=2)
    port.focus(field)
    clock.advance(16)
    s = snap(out)
    assert s.gated == "too_long" and s.text == "" and s.caret_rect is None


def test_incognito_frame_name_is_gated_without_reading(rig):
    # The marker exists only in the AT-SPI frame name, which the port passes as title.
    _, port, clock, out = rig
    field = FakeField(text="secret plans")
    field.set_meta(wm_class="Google-chrome", url="https://example.com/",
                   title="Example - Google Chrome (Incognito)", role="entry")
    port.focus(field)
    clock.advance(16)
    s = snap(out)
    assert s.gated == "incognito" and field.reads == 0
    assert (s.title, s.url) == ("", None)  # a private tab's title and URL stay in capture
    assert (s.app, s.wm_class, s.role) == ("gedit", "Google-chrome", "entry")


@pytest.mark.parametrize("title", ["", "   "])
def test_web_field_with_unreadable_frame_name_is_gated(rig, title):
    # No frame name means the private-window marker cannot be checked: fail closed.
    _, port, clock, out = rig
    field = FakeField(text="secret plans")
    field.set_meta(wm_class="Google-chrome", url="https://example.com/", title=title,
                   role="entry")
    port.focus(field)
    clock.advance(16)
    s = snap(out)
    assert s.gated == "title_unknown" and s.text == "" and field.reads == 0


def test_browser_field_without_frame_name_or_url_is_gated(rig):
    _, port, clock, out = rig
    field = FakeField(text="secret plans")
    field.set_meta(wm_class="firefox_firefox", url=None, title="", role="entry")
    port.focus(field)
    clock.advance(16)
    assert snap(out).gated is not None and field.reads == 0


def test_untitled_native_window_is_not_gated(rig):
    _, port, clock, out = rig
    field = FakeField(text="hello")
    field.set_meta(title="")
    port.focus(field)
    clock.advance(16)
    assert snap(out).gated is None


def test_protected_state_is_gated_as_password(rig):
    # The port sets `protected` when attributes say text-input-type: password.
    _, port, clock, out = rig
    field = FakeField(text="hunter2")
    field.set_meta(role="entry", states=frozenset({"editable", "focused", "protected"}))
    port.focus(field)
    clock.advance(16)
    assert snap(out).gated == "password" and field.reads == 0


def test_start_picks_up_an_already_focused_field():
    clock, port, out = FakeClock(), FakeA11y(), []
    port.current = FakeField(text="already here")
    FocusTracker(port, PrivacyGate(Config()), lambda t, p: out.append((t, p)), clock).start()
    clock.advance(16)
    assert snap(out).text == "already here"


def test_resync_reads_the_current_field_again(rig):
    tracker, port, clock, out = rig
    field = FakeField(text="hello")
    tracker.resync()  # nothing focused: nothing to do
    clock.advance(16)
    assert out == []
    port.focus(field)
    clock.advance(16)
    tracker.resync()
    clock.advance(16)
    assert field.reads == 2 and snap(out).seq == 2


# ---------------------------------------------------------------- only editable fields


def test_a_focused_page_being_read_is_no_field_not_a_private_one(rig):
    """Chrome's document web area takes focus while the user reads a page: it has text but
    is not editable. It is reported as no field (nothing shown), never as a gated one."""
    _, port, clock, out = rig
    page = FakeField(key="doc", text="Dear Afsan, ...", caret=0)
    page.set_meta(role="document web", states=frozenset({"focused", "focusable"}),
                  wm_class="Google-chrome", url="https://mail.google.com/", title="")
    port.focus(page)
    clock.advance(16)
    assert out == [] and page.reads == 0


def test_leaving_a_field_for_a_page_reports_the_field_lost(rig):
    _, port, clock, out = rig
    field = FakeField(text="hello", caret=5)
    port.focus(field)
    clock.advance(16)
    page = FakeField(key="doc", text="a page", caret=0)
    page.set_meta(role="document web", states=frozenset({"focused"}))
    port.focus(page)
    clock.advance(16)
    assert out[-1] == (FOCUS_LOST, {"reason": "blur"}) and page.reads == 0
    port.change(page)
    clock.advance(16)
    assert [t for t, _ in out] == [SNAPSHOT, FOCUS_LOST]  # once, not per change


def test_a_password_field_is_still_a_field(rig):
    _, port, clock, out = rig
    field = FakeField(text="hunter2", caret=7)
    field.set_meta(role="password text", states=frozenset({"focused", "protected"}))
    port.focus(field)
    clock.advance(16)
    assert snap(out).gated == "password" and field.reads == 0
