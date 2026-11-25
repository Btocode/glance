"""Select-anything: the PRIMARY selection of any app, rewritten on an explicit hotkey."""
import pytest

from glance.capture.main import handle_command
from glance.config import AppCfg, Config
from glance.features.select_anything import (
    PASTED_HINT,
    SelectAnything,
)
from glance.protocol import PASTE, ApplyResult, Edit, text_sha256
from glance.writeback.engine import WriteBackEngine
from tests.fakes import FakeClipboard, FakeField, FakeKeys, FakeSelection, win
from tests.test_ai_analyze import Rig, snap

SEL = "pls review teh patch"
REWRITTEN = "Please review the patch."
SECRET = "sk-ant-api03-AbCdEf0123456789xyzQRSTUV"


class Sel:
    def __init__(self, tmp_path, cfg=None, page_lookup=None):
        self.rig = Rig(tmp_path, patterns={"teh": ("Typo", ("the",))})
        if cfg is not None:
            self.rig.cfg = cfg
        self.rig.backend.deltas = (REWRITTEN,)
        self.sel = FakeSelection()
        from glance.privacy.gate import PrivacyGate
        r = self.rig
        self.sa = SelectAnything(
            r.c, self.sel, lambda: PrivacyGate(r.cfg), lambda: r.cfg, r.harper, r.c._filter,
            r.sup, r.ai._resolver, r.overlay, r.c._capture, r.notifier, r.bg,
            page_lookup=page_lookup)
        r.c.add_listener(self.sa)

    @property
    def overlay(self):
        return self.rig.overlay

    @property
    def capture(self):
        return self.rig.c._capture

    @property
    def notes(self):
        return [n[0] for n in self.rig.notifier.calls]

    @property
    def backend(self):
        return self.rig.backend

    def select(self, text=SEL, wm_class="Slack", xid=9, title="general - Slack"):
        w = win(wm_class, xid=xid, title=title)
        self.sel.set(text, w)
        return w


@pytest.fixture
def s(tmp_path):
    return Sel(tmp_path)


def test_no_selection_notifies(s):
    s.sel.set(None, win("Slack", xid=9))
    s.sa.trigger()
    assert s.notes == ["Select some text first"] and s.overlay.panel is None
    s.sel.set("  \n ", win("Slack", xid=9))
    s.sa.trigger()
    assert s.notes[-1] == "Select some text first" and s.overlay.panel is None


def test_selection_owned_by_denylisted_app_is_refused(s):
    s.sel.set("hunter2 master password", win("keepassxc", xid=7), win("keepassxc", xid=7))
    s.sa.trigger()
    assert s.overlay.panel is None and s.backend.calls == [] and s.rig.harper.calls == []
    assert s.sel.reads == []  # refused before the selection was read


def test_selection_while_a_password_field_is_focused_is_refused(s):
    # The window (Slack) is allowed; the focused field in it is a password entry.
    s.rig.c.handle_snapshot(snap("", gated="password"))
    s.select("hunter2 is my password")
    s.sa.trigger()
    assert s.notes == ["Glance does not read this window"] and s.overlay.panel is None
    assert s.sel.reads == [] and s.backend.calls == []  # refused before PRIMARY was read


def test_password_field_of_another_process_does_not_block(s):
    s.rig.c.handle_snapshot(snap("", gated="password"))  # pid 100
    s.sel.set(SEL, win("Slack", xid=9, pid=200))
    s.sa.trigger()
    assert s.overlay.panel is not None


def test_other_gated_reasons_of_the_focused_field_do_not_block(s):
    s.rig.c.handle_snapshot(snap("", gated="not_editable"))  # a selectable label, say
    s.select()
    s.sa.trigger()
    assert s.overlay.panel is not None


def test_selection_from_other_window_is_not_used(s):
    s.sel.set("hunter2", win("keepassxc", xid=7), win("Slack", xid=9))
    s.sa.trigger()
    assert s.overlay.panel is None and s.notes[-1] == "Select text in this window first"
    assert s.backend.calls == [] and s.rig.harper.calls == []
    assert s.sel.reads == []


def test_unknown_owner_is_not_used(s):
    s.sel.set(SEL, None, win("Slack", xid=9))
    s.sa.trigger()
    assert s.overlay.panel is None and s.notes[-1] == "Select text in this window first"
    assert s.sel.reads == []


def test_incognito_window_refused(s):
    s.select(title="Inbox - Google Chrome (Incognito)", wm_class="Slack")
    s.sa.trigger()
    assert s.overlay.panel is None and s.backend.calls == [] and s.sel.reads == []


def test_terminal_allowed_but_copy_only(s):
    w = win("com.mitchellh.ghostty", xid=3)
    s.sel.set(SEL, w)
    s.sa.trigger()
    assert s.overlay.panel is not None and not s.overlay.panel.can_paste
    assert s.overlay.panel.mode == "selection"
    s.sa.preview("friendlier")
    s.sa.paste_over()  # defence in depth: the button is hidden, the method refuses too
    assert s.capture.pastes == []


def test_app_with_capture_off_is_still_usable(tmp_path):
    cfg = Config(apps={"gtk_app": AppCfg(capture=False)})
    s = Sel(tmp_path, cfg)
    s.select(wm_class="gtk_app")
    s.sa.trigger()
    assert s.overlay.panel is not None


def test_panel_shows_local_issues_read_only_at_the_pointer(s):
    s.select()
    s.sel.xy = (640, 480)
    s.sa.trigger()
    p = s.overlay.panel
    assert s.overlay.panel_xy == (640, 480)
    assert p.mode == "selection" and p.can_paste and p.can_fix_all is False
    assert [r.excerpt for r in p.issues] == [SEL]
    assert p.tone_chips and p.tone_chips[0].key == "friendlier"
    assert all(c.hotkey is None for c in p.tone_chips)
    assert s.backend.calls == []  # nothing leaves until a tone is asked for


def test_secret_in_selection_is_shielded(s):
    s.select(f"rotate {SECRET} then ping sam")
    s.sa.trigger()
    s.sa.preview("friendlier")
    (call,) = s.backend.calls
    assert SECRET not in call.text and "[[GLANCE_S1]]" in call.text


def test_preview_shows_a_diff_and_is_ready(s):
    s.select()
    s.sa.trigger()
    s.sa.preview("friendlier")
    p = s.overlay.panel
    assert p.preview_state == "ready" and p.preview
    (call,) = s.backend.calls
    assert call.text == SEL and call.ctx.profile == "team_channel"


def test_copy_is_default_and_puts_rewrite_on_clipboard(s):
    s.select()
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.copy()
    assert s.overlay.clipboard == [REWRITTEN]
    assert s.capture.pastes == [] and s.capture.applies == []


def test_copy_without_a_preview_rewrites_with_the_first_chip_then_copies(s):
    s.select()
    s.sa.trigger()
    s.sa.copy()
    assert s.overlay.clipboard == [REWRITTEN]
    assert s.backend.calls[0].tone == "friendlier"


def test_paste_over_sends_paste_and_reports_unverified(s):
    s.select()
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.paste_over()
    assert s.capture.pastes == [REWRITTEN] and s.capture.applies == []
    assert s.capture.paste_targets == [(9, "Slack")]  # the window the selection is in
    s.rig.c.handle_apply_result(ApplyResult("paste", "unverified", ""))
    assert s.notes[-1] == PASTED_HINT == "Pasted — Ctrl+Z in the app undoes it"


def test_failed_paste_says_so(s):
    s.select()
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.paste_over()
    s.rig.c.handle_apply_result(ApplyResult("paste", "refused", "clipboard not restorable"))
    assert "Copy" in s.notes[-1] and s.notes[-1] != PASTED_HINT


def test_paste_over_waits_for_the_rewrite(s):
    s.select()
    s.sa.trigger()
    s.sa.paste_over()  # no preview yet: the first chip is made, then pasted
    assert s.capture.pastes == [REWRITTEN]


def test_paste_is_refused_when_the_window_changed(s):
    s.select()
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sel.active_win = win("Other", xid=11)
    s.sa.paste_over()
    assert s.capture.pastes == []


def test_unapplicable_rewrite_is_never_pasted(s):
    s.select("hi @sam, see https://x.test/a")  # a mention in a rich editor
    s.rig.backend.deltas = ("Hello, see the page.",)
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.paste_over()
    assert s.capture.pastes == []
    s.sa.copy()
    assert s.overlay.clipboard == ["Hello, see the page."]


def test_uses_verified_path_when_field_is_captured(s):
    text = f"aa {SEL} bb"
    s.rig.c.handle_snapshot(snap(text))
    s.sel.set(SEL, win("Slack", xid=9, pid=100))
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.paste_over()
    (req,) = s.capture.applies
    assert req.edit == Edit(3, 23, REWRITTEN)
    assert req.expected_sha256 == text_sha256(text)
    assert s.capture.pastes == []


def test_ambiguous_selection_in_captured_field_falls_back_to_selection_mode(s):
    s.rig.c.handle_snapshot(snap(f"{SEL} and {SEL}"))
    s.select()
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.paste_over()
    assert s.capture.applies == [] and s.capture.pastes == [REWRITTEN]


def test_snapshot_of_another_window_is_not_used_for_the_verified_path(s):
    s.rig.c.handle_snapshot(snap(f"aa {SEL} bb"))  # pid 100
    s.sel.set(SEL, win("Slack", xid=9, pid=555))
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.paste_over()
    assert s.capture.applies == [] and s.capture.pastes == [REWRITTEN]


def test_too_long_selection_refused(s):
    s.select("x" * 20001)
    s.sa.trigger()
    assert s.overlay.panel is None and s.backend.calls == []
    assert len(s.notes) == 1


def test_selection_is_forgotten_when_the_panel_closes(s):
    s.select()
    s.sa.trigger()
    s.sa.preview("friendlier")
    s.sa.close()
    assert s.overlay.panel is None
    s.sa.copy()
    s.sa.paste_over()
    assert s.overlay.clipboard == [] and s.capture.pastes == []
    assert SEL not in repr(vars(s.sa)) and REWRITTEN not in repr(vars(s.sa))


def test_a_new_snapshot_closes_the_panel(s):
    s.select()
    s.sa.trigger()
    s.rig.c.handle_snapshot(snap("something else entirely"))
    assert s.sa.is_open is False


def test_paused_daemon_does_nothing(s):
    s.rig.c.toggle_pause()
    s.select()
    s.sa.trigger()
    assert s.overlay.panel is None and s.rig.harper.calls == []


def test_custom_tone_asks_for_the_instruction(s):
    s.select()
    s.sa.trigger()
    s.sa.preview("custom")
    (_prompt, done) = s.overlay.asked[-1]
    assert s.backend.calls == []
    done("make it formal", True)
    assert s.backend.calls[-1].custom == "make it formal"


# ---------------------------------------------------------------- browsers (P3-3)

CHROME = "Google-chrome"
BANK = "Online banking - Google Chrome"


def test_browser_with_denylisted_url_is_refused(tmp_path):
    asked = []
    s = Sel(tmp_path, page_lookup=lambda done: (asked.append(1),
                                                done(("https://bank.example/login", BANK))))
    s.select(wm_class=CHROME, title=BANK)
    s.sa.trigger()
    assert asked and s.overlay.panel is None and s.backend.calls == []
    assert s.sel.reads == []


def test_browser_with_known_safe_url_proceeds(tmp_path):
    s = Sel(tmp_path, page_lookup=lambda done: done(("https://example.com/post", "Post")))
    s.select(wm_class=CHROME, title="Post - Google Chrome")
    s.sa.trigger()
    assert s.overlay.panel is not None and s.overlay.panel.confirm is None


def test_browser_incognito_title_from_the_page_lookup_is_refused(tmp_path):
    s = Sel(tmp_path, page_lookup=lambda done: done(
        ("https://example.com/", "Post - Google Chrome (Incognito)")))
    s.select(wm_class=CHROME, title="Post - Google Chrome")
    s.sa.trigger()
    assert s.overlay.panel is None


def test_browser_url_unknown_with_denylisted_title_is_refused(tmp_path):
    s = Sel(tmp_path, page_lookup=lambda done: done(None))
    s.select(wm_class=CHROME, title="My bank - Google Chrome")
    s.sa.trigger()
    assert s.overlay.panel is None and s.backend.calls == []


def test_browser_url_unknown_proceeds_without_asking(tmp_path):
    """The hotkey is the user's permission: an unidentified page is not asked about."""
    s = Sel(tmp_path)  # no lookup at all
    s.select(wm_class=CHROME, title="Some page - Google Chrome")
    s.sa.trigger()
    p = s.overlay.panel
    assert p is not None and p.confirm is None and p.tone_chips
    s.sa.copy()
    assert len(s.backend.calls) == 1 and len(s.overlay.clipboard) == 1


def test_browser_url_unknown_incognito_title_is_still_refused(tmp_path):
    s = Sel(tmp_path)
    s.select(wm_class=CHROME, title="Some page - Google Chrome (Incognito)")
    s.sa.trigger()
    assert s.overlay.panel is None and s.backend.calls == [] and s.sel.reads == []


# ---------------------------------------------------------------- engine and capture


class EngineRig:
    def __init__(self):
        self.clip = FakeClipboard("CLIP-ORIGINAL")
        self.field = FakeField(text="pls review teh patch")
        self.keys = FakeKeys(lambda: self.field, self.clip)
        self.sleeps = []
        self.engine = WriteBackEngine(lambda: None, self.keys, self.clip,
                                      lambda wm: AppCfg(),
                                      self.sleeps.append)


def test_paste_unverified_pastes_and_restores_the_clipboard():
    r = EngineRig()
    res = r.engine.paste_unverified(REWRITTEN, r.keys.window)
    assert res.status == "unverified"
    assert r.keys.chords == [("ctrl", "v")]
    assert r.clip.calls == ["save", "set_secret_text", "restore"] and r.clip.secret_hint_used
    assert r.clip.content == "CLIP-ORIGINAL" and r.clip.secret_window == r.keys.window
    assert r.keys.waits  # modifiers released first


def test_paste_unverified_refuses_when_clipboard_not_restorable():
    r = EngineRig()
    r.clip.unsaveable = True
    res = r.engine.paste_unverified(REWRITTEN, r.keys.window)
    assert res.status == "refused" and "clipboard" in res.detail
    assert r.keys.chords == [] and "set_secret_text" not in r.clip.calls


def test_paste_unverified_refuses_when_another_window_has_the_keyboard():
    r = EngineRig()
    res = r.engine.paste_unverified(REWRITTEN, r.keys.window + 1)
    assert res.status == "stale" and r.keys.chords == [] and r.clip.calls == []
    r.keys.window = 0
    assert r.engine.paste_unverified(REWRITTEN, 0).status == "stale"


def test_paste_unverified_refuses_while_keys_are_held():
    r = EngineRig()
    r.keys.held = True
    assert r.engine.paste_unverified(REWRITTEN, r.keys.window).status == "refused"
    assert r.keys.chords == [] and r.clip.calls == ["save"]


def test_paste_unverified_stops_if_focus_moves_while_taking_the_clipboard():
    r = EngineRig()
    orig = r.clip.set_secret_text

    def moving(text, window):
        orig(text, window)
        r.keys.window += 1

    r.clip.set_secret_text = moving
    res = r.engine.paste_unverified(REWRITTEN, 0x3A00007)
    assert res.status == "stale" and r.keys.chords == []
    assert r.clip.content == "CLIP-ORIGINAL"


def test_revert_does_nothing_for_an_unverified_paste():
    r = EngineRig()
    r.engine.paste_unverified(REWRITTEN, r.keys.window)
    assert r.engine.revert_last().status == "stale"
    assert ("ctrl", "z") not in r.keys.chords


def test_capture_routes_paste_to_the_engine():
    class E:
        def paste_unverified(self, text, window=0):
            self.args = (text, window)
            return ApplyResult("", "unverified", "")

    e = E()
    t, p = handle_command(PASTE, {"text": REWRITTEN, "window": 5, "wm_class": "Slack",
                                  "id": 3}, e)
    assert t == "apply_result" and p["status"] == "unverified" and p["id"] == 3
    assert p["apply_id"] == "paste" and e.args == (REWRITTEN, 5)


def test_capture_refuses_paste_into_a_terminal_and_without_engine():
    class E:
        def paste_unverified(self, *a):
            raise AssertionError("must not be called")

    _t, p = handle_command(PASTE, {"text": "x", "window": 5, "wm_class": "ghostty"}, E(),
                           is_terminal=lambda wm: wm == "ghostty")
    assert p["status"] == "refused"
    _t, p = handle_command(PASTE, {"text": "x", "window": 5, "wm_class": "Slack"}, None)
    assert p["status"] == "failed"
    _t, p = handle_command(PASTE, {"text": 5}, object())
    assert p["status"] == "failed"


def test_a_broken_denylist_pattern_still_blocks_and_does_not_raise(tmp_path):
    from glance.config import PrivacyCfg
    cfg = Config(privacy=PrivacyCfg(url_denylist=("(unclosed",)))
    s = Sel(tmp_path, cfg)
    s.select(wm_class=CHROME, title="Some (unclosed page - Google Chrome")
    s.sa.trigger()
    assert s.overlay.panel is None and s.sel.reads == []
