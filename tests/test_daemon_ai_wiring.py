"""How glanced wires the AI parts: status on the main loop, language detector, tone hotkeys
and the panel's tone actions. No GTK."""
from types import SimpleNamespace

from glance.backends.base import Health
from glance.config import BackendCfg, ConfigError, load_config
from glance.daemon.controller import Controller
from glance.daemon.links import PanelEscape
from glance.daemon.main import (
    FakeAiBackend,
    build_ai,
    build_issue_filter,
    fake_backend_wanted,
)
from glance.daemon.panel_actions import dispatch_panel_action
from glance.daemon.wiring import tone_hotkeys
from glance.grammar.issues import Issue
from glance.protocol import FieldSnapshot, Rect
from glance.ui.hotkeys import bind_hotkeys
from tests.fakes import (
    FakeBackend,
    FakeBg,
    FakeCapture,
    FakeClock,
    FakeHarper,
    FakeNotifier,
    FakeOverlay,
    FakeTray,
)
from tests.fakes import typing_config as Config  # these tests check while typing
from tests.test_hotkeys import FakeHotkeys

TEXT = "send me the report now, i need it before the meeting starts."
BANGLISH = "ami kal ashbo na. I will send the file to the whole team."


def snap(text=TEXT, wm_class="gedit", title="notes", field_id="f1"):
    return FieldSnapshot(
        app=wm_class, wm_class=wm_class, title=title, url=None, role="text", text=text,
        caret=len(text), caret_rect=Rect(100, 200, 1, 18), prev_char_rect=None,
        field_rect=Rect(90, 190, 400, 300), window_rect=Rect(50, 50, 800, 600),
        field_id=field_id, capture_confidence=1.0, seq=1, pid=100, gated=None)


class QueueClock(FakeClock):
    """`post` waits, as on the real main loop, until the test drains it."""

    def __init__(self) -> None:
        super().__init__()
        self.posted = []

    def post(self, fn) -> None:
        self.posted.append(fn)

    def drain(self) -> None:
        while self.posted:
            self.posted.pop(0)()


class NoOverrides:
    def __init__(self) -> None:
        self.saved = {}

    def get(self, key):
        return self.saved.get(key)

    def set(self, key, name) -> None:
        self.saved[key] = name


class Daemon:
    def __init__(self, tmp_path, clock=None, raw=None):
        self.clock = clock or FakeClock()
        self.cfg = Config(backend=BackendCfg(choice="claude"), raw=raw or {})
        self.backend, self.spec = FakeBackend("claude"), FakeBackend("claude")
        self.overlay, self.capture = FakeOverlay(), FakeCapture()
        self.bg = FakeBg(self.clock, self.backend, self.spec)
        self.filter, self.detector = build_issue_filter(tmp_path / "dictionary.txt",
                                                        tmp_path / "ignored.json")
        self.c = Controller(self.clock, lambda: self.cfg, FakeHarper({}), self.filter,
                            self.overlay, self.capture, FakeNotifier(), FakeTray(), self.bg)
        self.overrides = NoOverrides()
        self.profile_errors = []
        self.ai, self.sup = build_ai(
            self.c, self.clock, lambda: self.cfg, self.bg, self.overlay, self.detector,
            {"claude": self.backend}, {"claude": self.spec}, tmp_path / "usage.json",
            self.overrides, lambda: False, self.profile_errors.append)

    def focus(self, s=None):
        self.c.handle_snapshot(s or snap())
        self.clock.advance(1200)

    def rewrites(self):
        return [c for c in self.backend.calls if c.kind == "rewrite"]


def test_supervisor_status_reaches_the_coordinator_on_the_main_loop(tmp_path):
    clock = QueueClock()
    d = Daemon(tmp_path, clock)
    clock.drain()
    assert d.c.status()["ai"] == "ok"
    d.backend.health_value = Health(True, False, "signed out")
    d.sup.healths(refresh=True)  # as a worker thread would see it
    assert d.c.status()["ai"] == "ok" and len(clock.posted) == 1  # not on this thread
    clock.drain()
    assert d.c.status()["ai"] == "none"
    assert d.c.status()["backend"] == ""


def test_language_detector_filters_local_issues_and_tells_the_backend(tmp_path):
    d = Daemon(tmp_path)

    def harper(word):
        start = BANGLISH.index(word)
        return Issue(f"harper:SpellCheck:{start}", "harper", "SpellCheck", "spelling", start,
                     start + len(word), (), False)

    found = [harper(w) for w in ("ami", "kal", "ashbo", "whole")]
    kept = d.filter.apply(BANGLISH, found, "gedit")
    assert [BANGLISH[i.start:i.end] for i in kept] == ["whole"]  # the Banglish sentence is left
    d.focus(snap(BANGLISH))
    (call,) = [c for c in d.backend.calls if c.kind == "analyze"]
    assert call.ctx.mixed_language is True
    d.focus(snap(TEXT, field_id="f2"))
    assert d.backend.calls[-1].ctx.mixed_language is False


def test_tone_hotkeys_are_bound_by_default_and_apply_the_nth_chip(tmp_path):
    d = Daemon(tmp_path)
    actions = tone_hotkeys(d.ai)
    assert sorted(actions) == ["tone1", "tone2", "tone3", "tone4"]
    keys = FakeHotkeys()
    assert bind_hotkeys(keys, Config().general.hotkeys, actions) == {}
    assert keys.grabbed() == {f"tone{n}": f"Super+Alt+{n}" for n in range(1, 5)}
    d.focus()
    keys.grabs["tone3"][1]()  # Super+Alt+3
    assert [c.tone for c in d.rewrites()] == ["shorter"]
    assert [r.edit.replacement for r in d.capture.applies] == ["Thanks so much, will do!"]


def test_tone_hotkey_defaults_merge_with_the_users_own(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[general.hotkeys]\ntone2 = "Super+Alt+F2"\n')
    chords = load_config(path).general.hotkeys
    assert (chords["tone1"], chords["tone2"], chords["tone4"]) == (
        "Super+Alt+1", "Super+Alt+F2", "Super+Alt+4")
    assert chords["panel"] == "Super+Alt+space"


def test_a_taken_tone_chord_is_reported_and_the_rest_still_work(tmp_path):
    d = Daemon(tmp_path)
    keys = FakeHotkeys(taken={"Super+Alt+2"})
    system = [(frozenset({"super", "alt"}), "4")]  # the desktop's own shortcut: left alone
    problems = bind_hotkeys(keys, Config().general.hotkeys, tone_hotkeys(d.ai), system)
    assert problems == {"tone2": "conflict", "tone4": "system"}
    assert sorted(keys.grabbed()) == ["tone1", "tone3"]


def test_rich_editor_rule_uses_the_live_config(tmp_path):
    d = Daemon(tmp_path)
    text = "ping @sam about the release notes before the meeting today"
    d.backend.deltas = ("Hey [[GLANCE_E1]], a word about the release notes?",)
    d.focus(snap(text, wm_class="Slack", title="general (Channel) - Acme - Slack"))
    d.ai.apply_chip(1)
    assert d.capture.applies == []
    assert d.overlay.panel.copy_offer == "Hey @sam, a word about the release notes?"
    d.cfg = load_config(_write(tmp_path, "[apps.slack]\nrich = false\n"))
    d.cfg = Config(backend=BackendCfg(choice="claude"), apps=d.cfg.apps)
    # The user said Slack is plain text for them: the next rewrite there is typed.
    d.focus(snap(text, wm_class="Slack", title="general (Channel) - Acme - Slack",
                 field_id="f2"))
    d.ai.apply_chip(1)
    assert [r.edit.replacement for r in d.capture.applies] == [
        "Hey @sam, a word about the release notes?"]


def _write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_a_bad_profile_table_is_reported_and_the_builtins_are_used(tmp_path):
    d = Daemon(tmp_path, raw={"profile": [{"name": "x", "chips": ["nope"]}]})
    (err,) = d.profile_errors
    assert isinstance(err, ConfigError) and "chips" in err.key
    d.focus()
    assert [c.key for c in d.ai.tone_chips()][:2] == ["friendlier", "more_direct"]


def test_panel_tone_actions_reach_the_coordinator(tmp_path):
    d = Daemon(tmp_path)
    d.focus()
    d.c.open_panel()
    def act(action, **args):
        dispatch_panel_action(d.c, action, args, d.ai, d.overrides.set)

    act("tone_hover", key="shorter")
    assert d.overlay.panel.preview_state == "ready" and d.capture.applies == []
    act("apply_tone")
    assert len(d.capture.applies) == 1
    act("tone", key="friendlier")  # a click shows the preview first
    assert len(d.capture.applies) == 1 and [c.tone for c in d.rewrites()] == [
        "shorter", "friendlier"]
    act("tone", key="custom")
    assert [prompt for prompt, _cb in d.overlay.asked] == ["Rewrite as…"]
    act("profile", name="email")
    assert d.overlay.panel.profile_label == "Email"
    assert d.overrides.saved == {"gedit": "email"}
    assert d.ai.tone_chips()[0].key == "professional"
    for bad in (("tone", {}), ("profile", {"name": "no-such-profile"}), ("tone_hover", {})):
        dispatch_panel_action(d.c, bad[0], bad[1], d.ai, d.overrides.set)  # nothing raised
    assert d.overrides.saved == {"gedit": "email"}


def test_copy_action_prefers_the_rewrite_then_the_controllers_offer(tmp_path):
    d = Daemon(tmp_path)
    d.backend.deltas = ("Hey, a word about the notes?",)
    d.focus(snap("ping @sam about the release notes before the meeting today"))
    d.ai.apply_chip(1)  # the mention was lost: copy only
    dispatch_panel_action(d.c, "copy", {}, d.ai)
    assert d.overlay.clipboard == ["Hey, a word about the notes?"]
    calls = []
    stub = SimpleNamespace(copy_instead=lambda: calls.append("controller"))
    dispatch_panel_action(stub, "copy", {}, SimpleNamespace(copy_rewrite=lambda: False))
    dispatch_panel_action(stub, "copy", {})
    assert calls == ["controller", "controller"]
    dispatch_panel_action(stub, "tone", {"key": "shorter"})  # no coordinator: ignored


def test_fake_backend_needs_both_test_switches():
    assert fake_backend_wanted({"GLANCE_TEST": "1", "GLANCE_FAKE_BACKEND": "1"}) is True
    assert fake_backend_wanted({"GLANCE_FAKE_BACKEND": "1"}) is False
    assert fake_backend_wanted({"GLANCE_TEST": "1"}) is False
    assert fake_backend_wanted({"GLANCE_TEST": "0", "GLANCE_FAKE_BACKEND": "1"}) is False
    b = FakeAiBackend()
    assert "".join(b.rewrite("x", "shorter", None)) == "Thanks so much, will do!"
    assert b.health().signed_in is True and b.analyze("x", None).mismatch is False


def test_panel_escape_passes_ask_text_through():
    overlay = FakeOverlay()
    esc = PanelEscape(overlay, FakeHotkeys(), lambda: None)
    def done(text, back):
        pass

    esc.ask_text("Rewrite as…", done)
    assert overlay.asked == [("Rewrite as…", done)]


def test_test_panel_action_exists_only_with_glance_test():
    from glance.daemon.control import handle_test_panel_action

    seen = []
    on = {"GLANCE_TEST": "1"}
    click = {"action": "tone", "args": {"key": "custom"}}
    assert handle_test_panel_action("test_panel_action", click, lambda *a: seen.append(a),
                                    {}) is None
    assert handle_test_panel_action("status", {}, lambda *a: seen.append(a), on) is None
    assert seen == []
    assert handle_test_panel_action("test_panel_action", click, lambda *a: seen.append(a),
                                    on) == {"ok": True}
    assert seen == [("tone", {"key": "custom"})]
    bad = handle_test_panel_action("test_panel_action", {"action": 3}, lambda *a: 1 / 0, on)
    assert bad == {"ok": False, "error": "bad request"}


def test_strip_and_card_actions(tmp_path):
    d = Daemon(tmp_path)
    d.focus()

    def act(action, **args):
        dispatch_panel_action(d.c, action, args, d.ai, d.overrides.set)

    act("more")
    assert d.overlay.panel is not None
    act("more")
    assert d.overlay.panel is None
    act("tone_apply", key="shorter")  # a card click: made, then applied in one go
    assert len(d.capture.applies) == 1 and [c.tone for c in d.rewrites()] == ["shorter"]
    act("tone_apply", key="custom")  # "Rewrite as…" still asks first
    assert [prompt for prompt, _cb in d.overlay.asked] == ["Rewrite as…"]
    calls = []
    stub = SimpleNamespace(use_strip=lambda: calls.append("use"))
    dispatch_panel_action(stub, "use", {})
    assert calls == ["use"]
    act("tone_apply")  # malformed: nothing raised
