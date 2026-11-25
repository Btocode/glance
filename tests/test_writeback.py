"""WriteBackEngine against an editor double: what reaches the field, the keys, the clipboard."""
from __future__ import annotations

import pytest

from glance.capture.ports import FieldRead
from glance.config import AppCfg
from glance.protocol import ApplyRequest, Edit, text_sha256
from glance.writeback.engine import TYPE_GROUP, WriteBackEngine, _mid_write, normalize
from tests.fakes import FakeClipboard, FakeField, FakeKeys

UNDO = ("ctrl", "z")
PASTE = ("ctrl", "v")
LEFT = ("shift", "Left")
OBJ = "\ufffc"


class Rig:
    """An engine wired to fakes. `focus` is what the desktop has focused right now."""

    def __init__(self, field: FakeField | None, cfg: AppCfg | None = None, allowed=None):
        self.focus = field
        self.cfg = cfg or AppCfg()
        self.clip = FakeClipboard("CLIP-ORIGINAL")
        self.keys = FakeKeys(lambda: self.focus, self.clip)
        self.sleeps: list[float] = []
        self.on_sleep = None
        self.wm_classes: list[str] = []
        kwargs = {} if allowed is None else {"allowed": allowed}
        self.engine = WriteBackEngine(
            lambda: self.focus, self.keys, self.clip, self._app_cfg, self._sleep, **kwargs)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))

    def _app_cfg(self, wm_class: str) -> AppCfg:
        self.wm_classes.append(wm_class)
        return self.cfg

    def apply(self, edit: Edit, method: str = "auto"):
        return self.engine.apply(req(self.focus, edit, method))


def other_app(rig: Rig) -> None:
    """The user switches to a window of another application."""
    rig.keys.window, rig.keys.active = 0x5000001, 4242


def req(field: FakeField, edit: Edit, method: str = "auto") -> ApplyRequest:
    return ApplyRequest("a1", field.key, text_sha256(field.text), edit, method)


# ---------------------------------------------------------------- the happy paths

def test_short_fix_is_typed_and_verified():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    r = rig.apply(Edit(0, 3, "The"))
    assert (r.apply_id, r.status) == ("a1", "verified")
    assert f.text == "The cat"
    assert rig.keys.typed == ["The"] and rig.keys.delays == [AppCfg().type_delay_ms]
    assert rig.keys.type_windows == [rig.keys.window]  # typing is tied to the pinned window
    assert rig.clip.calls == []
    assert f.selections == [(0, 3)]
    assert rig.wm_classes == ["gedit"]


def test_long_fix_is_pasted_and_clipboard_restored():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    new = "x" * 81
    assert rig.apply(Edit(0, 3, new)).status == "verified"
    assert f.text == new + " cat"
    assert rig.clip.content == "CLIP-ORIGINAL" and rig.clip.secret_hint_used
    assert rig.clip.calls == ["save", "set_secret_text", "restore"]
    assert rig.clip.secret_window == rig.keys.window  # offered to the pinned window only
    assert rig.keys.chords.count(PASTE) == 1 and rig.keys.typed == []
    assert 0.05 in rig.sleeps  # the application gets time to read before the restore


def test_non_text_clipboard_survives_a_paste():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    image = {"image/png": b"\x89PNG..."}
    rig.clip.content = image
    assert rig.apply(Edit(0, 3, "y" * 81)).status == "verified"
    assert rig.clip.content is image


def test_eighty_chars_typed_eighty_one_pasted():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "x" * 80)).status == "verified"
    assert "".join(rig.keys.typed) == "x" * 80 and rig.clip.calls == []
    assert {len(t) for t in rig.keys.typed} == {TYPE_GROUP}  # checked between groups


def test_untypeable_short_text_goes_to_paste():
    f = FakeField(text="naive -")
    rig = Rig(f)
    assert rig.apply(Edit(0, 7, "naïve —")).status == "verified"
    assert f.text == "naïve —"
    assert rig.keys.typed == [] and rig.keys.chords.count(PASTE) == 1


def test_newline_is_never_typed():
    # A typed Return sends the message in a chat application.
    f = FakeField(text="one two")
    rig = Rig(f)
    rig.keys.can_type = lambda text: True  # even if the keymap has a key for it
    assert rig.apply(Edit(3, 4, "\n")).status == "verified"
    assert rig.keys.typed == [] and rig.keys.chords.count(PASTE) == 1


def test_forced_type_of_untypeable_text_is_refused():
    f = FakeField(text="naive")
    rig = Rig(f)
    r = rig.apply(Edit(0, 5, "naïve"), method="type")
    assert r.status == "refused" and f.text == "naive"
    assert rig.keys.events == [] and f.selections == []


def test_forced_paste_of_short_text():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The"), method="paste").status == "verified"
    assert rig.keys.typed == [] and rig.keys.chords.count(PASTE) == 1


def test_empty_replacement_uses_backspace():
    f = FakeField(text="the the cat")
    rig = Rig(f)
    assert rig.apply(Edit(3, 7, "")).status == "verified"
    assert f.text == "the cat"
    assert rig.keys.events == [("chord", ("BackSpace",))] and rig.clip.calls == []


def test_insertion_sets_caret_only():
    f = FakeField(text="the cat", caret=7)
    rig = Rig(f)
    assert rig.apply(Edit(3, 3, " big")).status == "verified"
    assert f.text == "the big cat"
    assert f.selections == [] and f.carets == [3, 11]  # then back after "cat", where it was


def test_noop_edit_writes_nothing():
    f = FakeField(text="The cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    assert rig.keys.events == [] and rig.clip.calls == [] and f.selections == []


# ---------------------------------------------------------------- stale: nothing synthesized

def test_apply_is_stale_when_text_changed():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "The"))
    f.text = "Teh cats"
    assert rig.engine.apply(r0).status == "stale"
    assert rig.keys.events == [] and rig.clip.calls == []
    assert f.selections == [] and f.carets == [] and f.text == "Teh cats"
    assert rig.keys.waits == [] and f.reads == 1  # decided at once, on the first read


def test_stale_paste_never_touches_the_clipboard():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "x" * 81))
    f.text = "Teh cats"
    assert rig.engine.apply(r0).status == "stale"
    assert rig.clip.calls == [] and rig.keys.events == [] and rig.keys.waits == []


def test_stale_when_focus_moves_right_after_selecting():
    f = FakeField(key="100:1:1", text="Teh cat")
    other = FakeField(key="100:1:2", text="mine", caret=4)
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "The"))
    rig.on_sleep = lambda n: setattr(rig, "focus", other)  # during the settle after selecting
    assert rig.engine.apply(r0).status == "stale"
    assert rig.keys.events == [] and other.text == "mine" and f.text == "Teh cat"
    assert f.selection is None  # nothing stays selected in the field left behind


def test_stale_when_focus_moved():
    f, other = FakeField(key="100:1:1", text="Teh cat"), FakeField(key="100:1:2", text="Teh cat")
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "The"))
    rig.focus = other
    assert rig.engine.apply(r0).status == "stale"
    assert rig.keys.events == [] and rig.clip.calls == []
    assert other.text == "Teh cat" and other.selections == [] and other.reads == 0


def test_stale_when_another_applications_window_is_active():
    # The tracker may not have heard of the switch yet; the X server always knows.
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.active = 4242
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert rig.keys.events == [] and f.selections == [] and rig.keys.waits == []


def test_stale_when_window_switches_while_keys_are_held():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.on_wait = lambda: other_app(rig)
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert rig.keys.events == [] and f.selections == []


def test_no_undo_into_another_applications_window():
    f = FakeField(text="Teh cat", mangle=str.upper)
    rig = Rig(f)
    rig.on_sleep = lambda n: rig.keys.typed and other_app(rig)
    r = rig.apply(Edit(0, 3, "The"))
    assert r.status == "failed" and "Copy" in r.detail and UNDO not in rig.keys.chords


def test_revert_last_stale_when_another_window_is_active():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    other_app(rig)
    assert rig.engine.revert_last().status == "stale" and UNDO not in rig.keys.chords


def test_stale_when_another_window_of_the_same_application_is_active():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.on_wait = lambda: setattr(rig.keys, "window", 0x3A00999)  # same pid
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert rig.keys.events == [] and f.selections == []


def test_stale_when_no_window_has_the_keyboard():
    # The shell's overview, or a grab: the active window is not where keys would go.
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.window = 0
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert rig.keys.events == [] and f.selections == [] and rig.keys.waits == []


def test_key_selection_stops_when_the_window_changes_mid_loop():
    f = FakeField(text="Teh quick cat", selection_fails=True)
    rig = Rig(f)
    rig.keys.on_chord = lambda keys: setattr(rig.keys, "window", 0x5000001)
    r = rig.apply(Edit(4, 9, "slow"))
    assert r.status == "stale"
    assert rig.keys.events == [("chord", LEFT)]  # one went out, then nothing at all
    assert f.selection is None and f.text == "Teh quick cat"


def test_key_selection_checks_the_window_before_the_first_chord():
    f = FakeField(text="Teh cat")
    rig = Rig(f, AppCfg(select="keys"))
    rig.on_sleep = lambda n: setattr(rig.keys, "window", 0x5000001)  # during the settle
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert rig.keys.events == [] and f.selection is None


def test_stale_when_a_key_goes_down_just_before_the_write():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.down = True  # pressed after the wait-for-release returned
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert rig.keys.events == [] and f.text == "Teh cat"
    assert f.selection is None and f.carets == [0]  # the user's caret, not the range end


def test_no_paste_when_the_window_changes_after_the_clipboard_is_set():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    real = rig.clip.set_secret_text

    def set_then_switch(text: str, window: int) -> None:
        real(text, window)
        rig.keys.window = 0x5000001

    rig.clip.set_secret_text = set_then_switch
    assert rig.apply(Edit(0, 3, "x" * 81)).status == "stale"
    assert PASTE not in rig.keys.chords and rig.keys.events == []
    assert rig.clip.content == "CLIP-ORIGINAL" and f.selection is None


def test_no_paste_when_a_key_goes_down_after_the_clipboard_is_set():
    # With a modifier down, Ctrl+V becomes some other shortcut in the user's application.
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    real = rig.clip.set_secret_text

    def set_then_press(text: str, window: int) -> None:
        real(text, window)
        rig.keys.down = True

    rig.clip.set_secret_text = set_then_press
    assert rig.apply(Edit(0, 3, "x" * 81)).status == "stale"
    assert PASTE not in rig.keys.chords and rig.keys.events == []
    assert rig.clip.content == "CLIP-ORIGINAL" and rig.clip.calls[-1] == "restore"
    assert f.selection is None and f.text == "Teh cat"


def test_revert_last_stale_in_another_window_of_the_same_application():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    rig.keys.window = 0x3A00999
    assert rig.engine.revert_last().status == "stale" and UNDO not in rig.keys.chords


def test_stale_when_nothing_focused():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "The"))
    rig.focus = None
    assert rig.engine.apply(r0).status == "stale" and rig.keys.events == []


def test_stale_when_user_types_while_keys_are_held():
    f = FakeField(text="Teh cat", caret=7)
    rig = Rig(f)
    rig.keys.on_wait = lambda: f.insert("s")
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert f.text == "Teh cats" and rig.keys.events == [] and f.selections == []


def test_stale_when_focus_moves_while_keys_are_held():
    f, other = FakeField(key="100:1:1", text="Teh cat"), FakeField(key="100:1:2", text="Teh cat")
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "The"))
    rig.keys.on_wait = lambda: setattr(rig, "focus", other)
    assert rig.engine.apply(r0).status == "stale"
    assert rig.keys.events == [] and other.selections == [] and f.selections == []


# ---------------------------------------------------------------- refusals

def test_refuses_while_modifier_held():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.held = True
    r = rig.apply(Edit(0, 3, "The"))
    assert r.status == "refused" and "release" in r.detail
    assert rig.keys.waits == [1500]
    assert rig.keys.events == [] and f.text == "Teh cat"
    assert f.selections == [] and f.carets == []


def test_paste_refused_while_modifier_held_leaves_clipboard_alone():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.held = True
    assert rig.apply(Edit(0, 3, "x" * 81)).status == "refused"
    assert "set_secret_text" not in rig.clip.calls and rig.clip.content == "CLIP-ORIGINAL"


def test_writeback_off():
    f = FakeField(text="Teh cat")
    rig = Rig(f, AppCfg(writeback="off"))
    r = rig.apply(Edit(0, 3, "The"))
    assert r.status == "refused" and "off" in r.detail
    assert rig.keys.events == [] and f.reads == 0 and rig.keys.waits == []


def test_gated_field_is_refused_unread():
    f = FakeField(text="hunter2")
    rig = Rig(f, allowed=lambda meta: False)
    assert rig.apply(Edit(0, 0, "x")).status == "refused"
    assert rig.keys.events == [] and f.reads == 0


def test_paste_refused_when_clipboard_not_restorable():
    f = FakeField(text="Teh cat", caret=7)
    rig = Rig(f)
    rig.clip.unsaveable = True  # e.g. an image the owner will not hand over
    r = rig.apply(Edit(0, 3, "x" * 81))
    assert r.status == "refused" and "clipboard" in r.detail
    assert f.text == "Teh cat" and rig.keys.events == []
    assert rig.clip.calls == ["save"]
    assert f.selection is None and f.caret == 7  # the user's caret was never moved


@pytest.mark.parametrize("edit", [Edit(5, 3, "x"), Edit(-1, 2, "x"), Edit(0, 99, "x")])
def test_bad_range_is_refused(edit):
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(edit).status == "refused"
    assert rig.keys.events == [] and f.text == "Teh cat"


def test_refuses_edit_touching_mention():
    f = FakeField(text="hi @jhon teh")
    rig = Rig(f)
    r = rig.apply(Edit(3, 8, "@john"))
    assert r.status == "refused" and "protected" in r.detail
    assert rig.keys.events == [] and f.text == "hi @jhon teh" and f.selections == []


def test_refuses_edit_cutting_into_an_entity():
    f = FakeField(text="see https://a.io/x now")
    rig = Rig(f)
    # The replacement holds the whole link, but the range ends in the middle of it: the
    # result would be "See https://a.io/x" followed by the link's own tail.
    assert rig.apply(Edit(0, 11, "See https://a.io/x")).status == "refused"
    assert rig.keys.events == [] and f.text == "see https://a.io/x now"


def test_allows_edit_that_keeps_entities_verbatim_and_in_order():
    f = FakeField(text="ask @bob and @amy pls")
    rig = Rig(f)
    assert rig.apply(Edit(0, 21, "Please ask @bob and @amy.")).status == "verified"
    assert f.text == "Please ask @bob and @amy."


def test_refuses_edit_that_reorders_entities():
    f = FakeField(text="ask @bob and @amy pls")
    rig = Rig(f)
    assert rig.apply(Edit(0, 21, "Please ask @amy and @bob.")).status == "refused"


def test_refuses_edit_over_embedded_object_even_if_kept():
    # U+FFFC stands for an embedded emoji or link; typing the character does not recreate it.
    f = FakeField(text=f"hi {OBJ} teh")
    rig = Rig(f)
    assert rig.apply(Edit(0, 8, f"Hi {OBJ} the"), method="paste").status == "refused"
    assert rig.keys.events == []


def test_edit_next_to_entity_is_fine():
    f = FakeField(text="hi @john teh")
    rig = Rig(f)
    assert rig.apply(Edit(9, 12, "the")).status == "verified" and f.text == "hi @john the"


# ---------------------------------------------------------------- selection

def test_selection_fallback_uses_shift_left():
    f = FakeField(text="Teh cat", selection_fails=True)
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    assert f.text == "The cat" and f.carets == [3, 0]  # 0: the user's caret again
    assert rig.keys.events[:3] == [("chord", LEFT)] * 3
    assert rig.keys.chords.count(LEFT) == 3


def test_select_keys_config_skips_atspi_selection():
    f = FakeField(text="Teh cat")
    rig = Rig(f, AppCfg(select="keys"))
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    assert f.selections == [] and f.carets == [3, 0] and rig.keys.chords.count(LEFT) == 3


def test_unconfirmed_selection_falls_back_to_keys():
    f = FakeField(text="Teh cat", caret=7, selection_lies=True)
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    assert f.text == "The cat" and rig.keys.chords.count(LEFT) == 3


def test_refused_when_range_cannot_be_selected_at_all():
    f = FakeField(text="Teh cat", caret=7)
    f.accept_selection = False  # neither a selection nor a caret move is accepted
    rig = Rig(f)
    r = rig.apply(Edit(0, 3, "The"))
    assert r.status == "refused" and "select" in r.detail
    assert rig.keys.events == [] and f.text == "Teh cat"


def test_key_selection_refuses_ranges_where_left_is_not_one_character():
    # One Shift+Left steps over a whole cluster: counting code points would over-select.
    f = FakeField(text="cafe\u0301 x", selection_fails=True)
    rig = Rig(f)
    assert rig.apply(Edit(0, 5, "cafe")).status == "refused"
    assert rig.keys.events == [] and f.text == "cafe\u0301 x"


# ---------------------------------------------------------------- verify and auto-revert

def test_mismatch_triggers_single_undo_and_reverted():
    f = FakeField(text="Teh cat", mangle=lambda s: s.upper())
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "reverted"
    assert f.text == "Teh cat" and rig.keys.chords.count(UNDO) == 1


def test_undo_steps_from_app_config():
    f = FakeField(text="Teh cat", mangle=str.upper, split_undo=True)
    rig = Rig(f, AppCfg(undo_steps=2))
    assert rig.apply(Edit(0, 3, "The")).status == "reverted"
    assert f.text == "Teh cat" and rig.keys.chords.count(UNDO) == 2


def test_split_undo_is_restored_even_with_default_undo_steps():
    # S4.4: gedit splits a typed replace into two steps; the original must come back anyway.
    f = FakeField(text="Teh cat", mangle=str.upper, split_undo=True)
    rig = Rig(f, AppCfg(undo_steps=1))
    assert rig.apply(Edit(0, 3, "The")).status == "reverted"
    assert f.text == "Teh cat" and rig.keys.chords.count(UNDO) == 2


def test_undo_stops_as_soon_as_the_original_is_back():
    f = FakeField(text="Teh ca", caret=6, mangle=None)
    f.insert("t")  # the user's own last edit: one more Ctrl+Z would take it away
    f.mangle = str.upper
    rig = Rig(f, AppCfg(undo_steps=2))  # pasted replaces need only one step (S4.4)
    assert rig.apply(Edit(0, 3, "The"), method="paste").status == "reverted"
    assert f.text == "Teh cat" and rig.keys.chords.count(UNDO) == 1


def test_failed_when_undo_does_not_restore():
    f = FakeField(text="Teh cat", mangle=str.upper, undo_broken=True)
    rig = Rig(f)
    r = rig.apply(Edit(0, 3, "The"))
    assert r.status == "failed" and "Copy" in r.detail
    before_undo = [e for e in rig.keys.events if e != ("chord", UNDO)]
    assert before_undo == [("type", "The")]
    assert len(rig.keys.events) == len(before_undo) + AppCfg().undo_steps
    assert rig.keys.events[-1] == ("chord", UNDO)
    # Nothing further: the failed apply cannot be "reverted" later either.
    n = len(rig.keys.events)
    assert rig.engine.revert_last().status == "stale" and len(rig.keys.events) == n


def test_failed_with_configured_undo_steps_sends_exactly_that_many():
    f = FakeField(text="Teh cat", mangle=str.upper, undo_broken=True)
    rig = Rig(f, AppCfg(undo_steps=2))
    assert rig.apply(Edit(0, 3, "The")).status == "failed"
    assert rig.keys.chords.count(UNDO) == 2


def test_paste_that_never_lands_sends_no_undo():
    # Ctrl+V was swallowed: the text is untouched, so Ctrl+Z would undo the user's own edit.
    f = FakeField(text="Teh ca", caret=6)
    f.insert("t")
    rig = Rig(f)
    rig.clip.serves = False
    r = rig.apply(Edit(0, 3, "x" * 81))
    assert r.status == "reverted" and f.text == "Teh cat"
    assert UNDO not in rig.keys.chords
    assert f.selection is None  # a selection left behind would eat the next keystroke
    assert rig.clip.content == "CLIP-ORIGINAL"
    assert rig.sleeps.count(0.3) == 1  # it looked a second time before giving up


def test_paste_that_lands_late_is_verified_not_abandoned():
    # A slow application: the text shows up after the first re-read. Calling that "did not
    # land" would leave a change in the field that the user was told is not there.
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.clip.serves = False
    new = "x" * 81

    def late(_n: int) -> None:
        if rig.sleeps[-1] == 0.3:
            f.insert(new)

    rig.on_sleep = late
    assert rig.apply(Edit(0, 3, new)).status == "verified"
    assert f.text == new + " cat" and UNDO not in rig.keys.chords


def test_restore_failure_is_reported_even_when_the_text_is_right():
    f = FakeField(text="Teh cat")
    rig = Rig(f)

    def boom(snap) -> None:
        raise TimeoutError("clipboard worker")

    rig.clip.restore = boom
    r = rig.apply(Edit(0, 3, "x" * 81))
    assert f.text == "x" * 81 + " cat"
    assert r.status == "verified" and "clipboard" in r.detail


def test_nbsp_tolerated():
    f = FakeField(text="the cat", mangle=lambda s: s.replace(" ", "\u00a0"))
    rig = Rig(f)
    assert rig.apply(Edit(3, 3, " big")).status == "verified"
    assert f.text == "the\u00a0big cat" and UNDO not in rig.keys.chords


def test_trailing_newline_tolerated():
    f = FakeField(text="Teh cat", post_edit=lambda t: t + "\n")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    assert f.text == "The cat\n" and UNDO not in rig.keys.chords


def test_typographic_quotes_tolerated():
    # Slack turns a pasted ' into U+2019 (findings S4.3).
    f = FakeField(text="I dont know", mangle=lambda s: s.replace("'", "\u2019"))
    rig = Rig(f)
    assert rig.apply(Edit(2, 6, "don't")).status == "verified"
    assert f.text == "I don\u2019t know" and UNDO not in rig.keys.chords


def test_normalize():
    assert normalize("a\u00a0b\n") == "a b"
    assert normalize("a\n\n") == "a\n"
    assert normalize("\u2018a\u2019 \u201cb\u201d") == "'a' \"b\""
    assert normalize("e\u0301") == "é"
    assert normalize("teh") != normalize("the")


# ---------------------------------------------------------------- not responding / focus lost

def test_timeout_is_failed_without_keys():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "The"))
    f.raise_timeout = True
    r = rig.engine.apply(r0)
    assert r.status == "failed" and "not responding" in r.detail
    assert rig.keys.events == [] and rig.clip.calls == []


def test_timeout_after_paste_still_restores_clipboard_and_sends_no_undo():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    f.timeout_after_reads = 3  # hangs on the verifying read
    r = rig.apply(Edit(0, 3, "x" * 81))
    assert r.status == "failed" and "not responding" in r.detail
    assert rig.clip.content == "CLIP-ORIGINAL" and rig.clip.calls[-1] == "restore"
    assert rig.keys.events[-1] == ("chord", PASTE) and UNDO not in rig.keys.chords


def test_focus_lost_while_typing_sends_no_undo_elsewhere():
    f, other = FakeField(key="100:1:1", text="Teh cat"), FakeField(key="100:1:2", text="mine", caret=4)
    other.insert("!")
    rig = Rig(f)
    rig.keys.abort_typing_after = 2
    rig.keys.on_abort = lambda: setattr(rig, "focus", other)
    r = rig.apply(Edit(0, 3, "The"))
    assert r.status == "failed" and "Copy" in r.detail
    assert UNDO not in rig.keys.chords and other.text == "mine!"


def test_keys_that_all_went_elsewhere_are_reported_failed():
    f = FakeField(key="100:1:1", text="Teh cat")
    other = FakeField(key="100:1:2", text="mine", caret=4)
    rig = Rig(f)
    rig.keys.abort_typing_after = 0  # the focus is gone before the first key lands
    rig.keys.on_abort = lambda: setattr(rig, "focus", other)
    r = rig.apply(Edit(0, 3, "The"))
    assert r.status == "failed" and "Copy" in r.detail  # not "reverted": keys did go out
    assert UNDO not in rig.keys.chords
    assert f.selection is None and f.carets == [0]  # our selection does not stay behind


def test_write_that_landed_is_verified_even_if_focus_moves_afterwards():
    f, other = FakeField(key="100:1:1", text="Teh cat"), FakeField(key="100:1:2", text="mine")
    rig = Rig(f)
    r0 = req(f, Edit(0, 3, "The"))
    rig.on_sleep = lambda n: rig.keys.typed and setattr(rig, "focus", other)
    assert rig.engine.apply(r0).status == "verified"
    assert f.text == "The cat" and other.text == "mine" and UNDO not in rig.keys.chords


def test_undo_stops_when_focus_moves_between_presses():
    f = FakeField(key="100:1:1", text="Teh cat", mangle=str.upper, split_undo=True)
    other = FakeField(key="100:1:2", text="mi", caret=2)
    other.insert("ne")
    rig = Rig(f, AppCfg(undo_steps=2))
    r0 = req(f, Edit(0, 3, "The"))
    rig.keys.on_chord = lambda keys: keys == UNDO and setattr(rig, "focus", other)
    r = rig.engine.apply(r0)
    assert r.status == "failed" and "Copy" in r.detail
    assert rig.keys.chords.count(UNDO) == 1 and other.text == "mine"


def test_broken_clipboard_adapter_leaves_text_and_clipboard_alone():
    f = FakeField(text="Teh cat", caret=7)
    rig = Rig(f)

    def boom(text: str, window: int) -> None:
        raise RuntimeError("could not own the selection")

    rig.clip.set_secret_text = boom
    r = rig.apply(Edit(0, 3, "x" * 81))
    assert r.status == "reverted" and f.text == "Teh cat"
    assert UNDO not in rig.keys.chords and PASTE not in rig.keys.chords
    assert f.selection is None and rig.clip.content == "CLIP-ORIGINAL"


def test_typing_aborted_but_still_focused_is_undone():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    rig.keys.abort_typing_after = 2
    assert rig.apply(Edit(0, 3, "The")).status == "reverted"
    assert f.text == "Teh cat"


# ---------------------------------------------------------------- revert_last

def test_revert_last_restores_and_is_single_use():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    r = rig.engine.revert_last()
    assert (r.apply_id, r.status) == ("a1", "reverted") and f.text == "Teh cat"
    assert rig.keys.chords.count(UNDO) == 1
    assert rig.engine.revert_last().status == "stale"
    assert rig.keys.chords.count(UNDO) == 1


def test_revert_last_with_nothing_applied():
    rig = Rig(FakeField(text="Teh cat"))
    assert rig.engine.revert_last().status == "stale" and rig.keys.events == []


def test_revert_last_stale_after_user_typed():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    f.caret = len(f.text)
    f.insert("s")
    assert rig.engine.revert_last().status == "stale"
    assert UNDO not in rig.keys.chords and f.text == "The cats"
    assert rig.engine.revert_last().status == "stale"  # memory cleared either way


def test_revert_last_stale_when_focus_moved():
    f, other = FakeField(key="100:1:1", text="Teh cat"), FakeField(key="100:1:2", text="The cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    rig.focus = other
    assert rig.engine.revert_last().status == "stale" and UNDO not in rig.keys.chords


def test_revert_last_restores_split_undo_editor():
    f = FakeField(text="Teh cat", split_undo=True)
    rig = Rig(f)  # default undo_steps = 1, the editor needs 2
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    assert rig.engine.revert_last().status == "reverted"
    assert f.text == "Teh cat" and rig.keys.chords.count(UNDO) == 2


def test_revert_last_after_autocorrected_apply():
    f = FakeField(text="I dont know", mangle=lambda s: s.replace("'", "\u2019"))
    rig = Rig(f)
    assert rig.apply(Edit(2, 6, "don't")).status == "verified"
    assert rig.engine.revert_last().status == "reverted" and f.text == "I dont know"


def test_revert_last_waits_for_the_hotkey_to_be_released():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    rig.keys.held = True
    r = rig.engine.revert_last()
    assert r.status == "refused" and UNDO not in rig.keys.chords and f.text == "The cat"
    rig.keys.held = False  # nothing was done, so the revert is still available
    assert rig.engine.revert_last().status == "reverted" and f.text == "Teh cat"


def test_revert_last_failed_when_undo_does_not_restore():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    f.undo_broken = True
    r = rig.engine.revert_last()
    assert r.status == "failed" and "Copy" in r.detail and rig.keys.chords.count(UNDO) == 1


def test_revert_last_timeout_is_failed_without_keys():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    f.raise_timeout = True
    assert rig.engine.revert_last().status == "failed" and UNDO not in rig.keys.chords


def test_a_stale_apply_keeps_the_previous_one_revertable():
    f = FakeField(text="Teh cat")
    rig = Rig(f)
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    stale = ApplyRequest("a2", f.key, text_sha256("something else"), Edit(0, 1, "x"), "auto")
    assert rig.engine.apply(stale).status == "stale"
    assert rig.engine.revert_last().apply_id == "a1" and f.text == "Teh cat"


def test_mid_write_states():
    old, edit = "Teh cat", Edit(0, 3, "The")
    assert _mid_write(" cat", old, edit)        # range deleted, nothing typed yet
    assert _mid_write("T cat", old, edit)       # GtkSourceView's stop between two undos
    assert _mid_write("Th cat", old, edit)
    assert not _mid_write("The cat", old, edit)   # the finished write is not "mid"
    assert not _mid_write("Teh cats", old, edit)  # the user's own text
    assert not _mid_write("X cat", old, edit)
    assert not _mid_write("cat", old, edit)


def test_gtk_source_view_default_makes_one_apply_one_native_undo():
    from glance.config import Config

    f = FakeField(text="Teh cat", split_undo=True)
    rig = Rig(f, cfg=Config().app("gedit"))
    assert rig.apply(Edit(0, 3, "The")).status == "verified"
    assert rig.keys.typed == [] and rig.keys.chords.count(PASTE) == 1
    f.undo()  # the user's own Ctrl+Z, once
    assert f.text == "Teh cat"


# ---------------------------------------------------------------- the user is typing (F1)

class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def typing_rig(text: str, caret: int, cfg: AppCfg | None = None):
    """A rig whose engine has a TypingWatch fed the field's text, as capture feeds it."""
    from glance.writeback.activity import TypingWatch

    f = FakeField(text=text, caret=caret)
    rig = Rig(f, cfg)
    clock = Clock()
    watch = TypingWatch(now=clock, fold=normalize)
    rig.engine._watch = watch
    watch.observe(f.key, f.text)
    return f, rig, clock, watch


def user_types(f: FakeField, watch, ch: str, at: int | None = None) -> None:
    """A key of the user's lands at their caret (or `at`); capture sees the new text."""
    if at is not None:
        f.caret, f.selection = at, None
    f.insert(ch, typed=True)
    watch.observe(f.key, f.text)


def test_watch_sees_typing_but_not_caret_moves_new_fields_or_own_writes():
    from glance.writeback.activity import TypingWatch

    clock = Clock()
    w = TypingWatch(now=clock)
    assert w.quiet_s() == float("inf")
    w.observe("f1", "abc")
    w.observe("f1", "abc")          # the caret moved
    w.observe("f2", "other")        # another field
    assert w.quiet_s() == float("inf")
    w.expect(["abcZZ"])
    w.observe("f2", "abcZZ")
    w.observe("f2", "other")        # back to a text seen before, but not announced: typing
    assert w.quiet_s() == 0.0
    clock.t += 1.0
    w.expect(["mine"])
    w.observe("f2", "mine")
    assert w.quiet_s() == 1.0       # the engine's own write
    clock.t += 3.0                  # announcements expire
    w.observe("f2", "x")
    w.observe("f2", "mine")
    assert w.quiet_s() == 0.0


def test_no_apply_starts_while_the_user_is_typing():
    f, rig, clock, watch = typing_rig("Teh cat sat", caret=11)
    user_types(f, watch, "s")
    clock.t += 0.1
    r = rig.engine.apply(req(f, Edit(0, 3, "The")))
    assert (r.status, r.detail) == ("stale", "you are typing; try again when you pause")
    assert rig.keys.events == [] and f.selections == [] and f.carets == []  # nothing moved
    clock.t += 0.5  # a pause
    assert rig.engine.apply(req(f, Edit(0, 3, "The"))).status == "verified"


def test_typing_stops_between_key_groups_and_the_caret_goes_back():
    f, rig, _clock, _watch = typing_rig("Teh cat sat", caret=11)
    real = rig.keys.type_text

    def type_then_user_types(text, delay_ms, window=None):
        real(text, delay_ms, window)
        if len(rig.keys.typed) == 1:  # after the first group the user presses a key ...
            rig.keys.down = True      # ... that has not arrived in the text yet

    rig.keys.type_text = type_then_user_types
    replacement = "The quick brown"  # two groups
    r = rig.engine.apply(req(f, Edit(0, 3, replacement)))
    assert (r.status, r.detail) == ("stale", "you are typing; try again when you pause")
    assert rig.keys.typed == [replacement[:TYPE_GROUP]]  # the rest was never sent
    assert f.text == "Teh cat sat" and f.caret == 11     # taken back, caret where it was


def test_a_users_key_in_the_field_is_never_undone():
    f, rig, _clock, watch = typing_rig("Teh cat sat", caret=11)
    real = rig.keys.type_text

    def type_then_user_types(text, delay_ms, window=None):
        real(text, delay_ms, window)
        if len(rig.keys.typed) == 1:
            user_types(f, watch, "x")  # lands at the caret this write put there

    rig.keys.type_text = type_then_user_types
    r = rig.engine.apply(req(f, Edit(0, 3, "The quick brown")))
    assert r.status == "failed" and "you typed while Glance was writing" in r.detail
    assert UNDO not in rig.keys.chords and "x" in f.text  # their key stays
    assert f.caret == len(f.text)  # and their next one goes where they were typing


def test_an_apply_stopped_by_a_key_puts_the_users_caret_back():
    f, rig, _clock, _watch = typing_rig("Teh cat sat", caret=11)
    rig.keys.down = True
    assert rig.engine.apply(req(f, Edit(0, 3, "The"))).status == "stale"
    assert f.caret == 11 and f.selection is None


def test_an_apply_put_back_restores_the_users_own_selection():
    f = FakeField(text="Teh cat sat", caret=11)
    f.selection = (8, 11)
    rig = Rig(f)
    rig.keys.down = True
    assert rig.apply(Edit(0, 3, "The")).status == "stale"
    assert f.selection == (8, 11)


def test_a_verified_apply_behind_the_caret_leaves_the_caret_where_the_user_was():
    f, rig, _clock, _watch = typing_rig("Teh cat sat", caret=11)
    assert rig.engine.apply(req(f, Edit(0, 3, "Thee"))).status == "verified"
    assert f.text == "Thee cat sat" and f.caret == 12  # moved by the one extra character


def test_the_engines_own_writes_do_not_count_as_typing():
    f, rig, _clock, watch = typing_rig("Teh cat sat", caret=11)
    real = rig.keys.type_text

    def type_and_report(text, delay_ms, window=None):
        real(text, delay_ms, window)
        watch.observe(f.key, f.text)  # capture's snapshot of this write's own progress

    rig.keys.type_text = type_and_report
    assert rig.engine.apply(req(f, Edit(0, 3, "The quick brown"))).status == "verified"
    watch.observe(f.key, f.text)
    r = rig.engine.apply(req(f, Edit(len(f.text), len(f.text), "!")))  # at once: fix all
    assert r.status == "verified"


def test_an_application_a_group_behind_does_not_stop_the_typing():
    f, rig, _clock, _watch = typing_rig("Teh cat sat", caret=11)
    real_read, lagging = f.read, [True]

    def read():
        r = real_read()
        if lagging and len(rig.keys.typed) == 1:  # the first group has not arrived yet
            lagging.clear()
            return FieldRead("Teh cat sat", r.caret, r.selection, r.caret_rect,
                             r.prev_char_rect, r.field_rect, r.window_rect)
        return r

    f.read = read
    assert rig.engine.apply(req(f, Edit(0, 3, "The quick brown"))).status == "verified"
    assert "".join(rig.keys.typed) == "The quick brown"


def test_an_apply_right_after_the_last_key_waits_for_the_pause():
    f, rig, clock, watch = typing_rig("Teh cat sat", caret=11)
    user_types(f, watch, "s")  # and then stops: Super+Alt+F at once

    def sleep(seconds):
        rig.sleeps.append(seconds)
        clock.t += seconds

    rig.engine._sleep = sleep
    assert rig.engine.apply(req(f, Edit(0, 3, "The"))).status == "verified"
    assert 0.39 <= sum(rig.sleeps[:1]) <= 0.41  # waited out QUIET_S, no longer


def test_an_apply_gives_up_when_the_user_keeps_typing():
    from glance.writeback.engine import PAUSE_WAIT_S

    f, rig, clock, watch = typing_rig("Teh cat sat", caret=11)

    def sleep(seconds):
        rig.sleeps.append(seconds)
        clock.t += seconds
        user_types(f, watch, "s")  # still typing

    rig.engine._sleep = sleep
    user_types(f, watch, "s")
    r = rig.engine.apply(req(f, Edit(0, 3, "The")))
    assert (r.status, r.detail) == ("stale", "you are typing; try again when you pause")
    assert rig.keys.events == [] and f.carets == [] and sum(rig.sleeps) <= PAUSE_WAIT_S + 0.01
