"""How glanced wires the features on top of the Controller: autocomplete, send-guard,
select-anything, glance-edit, the backend requests, onboarding, status. No GTK."""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from glance.backends.base import Health
from glance.config import BackendCfg, Config
from glance.daemon.assemble import attach_features
from glance.daemon.control import CtlRouter, handle_control
from glance.daemon.controller import Controller
from glance.daemon.links import CaptureLink, FieldPanelOverlay
from glance.daemon.main import FakeAiBackend, build_ai, build_issue_filter
from glance.daemon.panel_actions import PanelRouter
from glance.daemon.wiring import AiSetup, hotkeys_status, session_problem
from glance.features.sendguard import GUARD_LABELS
from glance.protocol import PASTE, ApplyResult, FieldSnapshot, Rect
from glance.ui.chip_model import ChipState
from tests.fakes import (
    FakeBackend,
    FakeBg,
    FakeCapture,
    FakeClock,
    FakeGhost,
    FakeHarper,
    FakeHotkeys,
    FakeNotifier,
    FakeOverlay,
    FakeSelection,
    FakeTray,
    win,
)
from tests.test_daemon_ai_wiring import NoOverrides

TEXT = "I will send the report to the whole team this afternoon"


def snap(text=TEXT, wm_class="gedit", title="notes", field_id="f1", caret=None, pid=100):
    return FieldSnapshot(
        app=wm_class, wm_class=wm_class, title=title, url=None, role="text", text=text,
        caret=len(text) if caret is None else caret, caret_rect=Rect(100, 200, 1, 18),
        prev_char_rect=None, field_rect=Rect(90, 190, 400, 300),
        window_rect=Rect(50, 50, 800, 600), field_id=field_id, capture_confidence=1.0,
        seq=1, pid=pid, gated=None)


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock()
        self.cfg = Config(backend=BackendCfg(choice="claude"))
        self.backend, self.spec = FakeBackend("claude"), FakeBackend("claude")
        self.screen, self.capture = FakeOverlay(), FakeCapture()
        self.bg = FakeBg(self.clock, self.backend, self.spec)
        self.filter, self.detector = build_issue_filter(tmp_path / "dictionary.txt",
                                                        tmp_path / "ignored.json")
        self.holder = []
        overlay = FieldPanelOverlay(
            self.screen, lambda: bool(self.holder) and self.holder[0].selection.is_open,
            lambda: self.holder[0].selection.close())
        self.c = Controller(self.clock, lambda: self.cfg, FakeHarper({}), self.filter,
                            overlay, self.capture, FakeNotifier(), FakeTray(), self.bg)
        self.ai, self.sup = build_ai(
            self.c, self.clock, lambda: self.cfg, self.bg, self.screen, self.detector,
            {"claude": self.backend}, {"claude": self.spec}, tmp_path / "usage.json",
            NoOverrides(), lambda: False, lambda e: None)
        self.ghost, self.hotkeys, self.sel = FakeGhost(), FakeHotkeys(), FakeSelection()
        self.notifier = FakeNotifier()
        self.f = attach_features(
            controller=self.c, ai=self.ai, supervisor=self.sup, cfg=lambda: self.cfg,
            grammar=FakeHarper({}), issue_filter=self.filter, ghost=self.ghost,
            hotkeys=self.hotkeys, clock=self.clock, run_ai=self.bg,
            language=self.detector.is_mixed, selection_port=self.sel, overlay=self.screen,
            capture=self.capture, notifier=self.notifier)
        self.holder.append(self.f)
        self.router = PanelRouter(self.c, self.ai, None, self.f.selection, self.f.guard)

    def focus(self, s=None):
        self.c.handle_snapshot(s or snap())
        self.clock.advance(1200)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ------------------------------------------------------------------ autocomplete


def test_complete_hotkey_shows_the_ghost_and_tab_types_it(rig):
    rig.focus(snap("Let us meet "))
    assert "complete" in rig.f.actions
    rig.f.actions["complete"]()
    assert rig.ghost.shown is not None and rig.ghost.shown.text == "tomorrow morning."
    assert rig.c.status()["autocomplete_visible"] is True
    rig.hotkeys.press("ac_tab")
    (req,) = rig.capture.applies
    assert req.edit.replacement == "tomorrow morning." and rig.ghost.shown is None
    assert "ac_tab" not in rig.hotkeys.grabbed()  # Tab is the app's again


def test_no_ghost_means_no_tab_grab(rig):
    rig.focus(snap("Let us meet "))
    assert "ac_tab" not in rig.hotkeys.grabbed()


def test_live_completions_run_only_on_the_speculative_instance(rig):
    rig.cfg = replace(rig.cfg, backend=replace(rig.cfg.backend, live_autocomplete=True))
    rig.focus(snap("Let us meet at the cafe"))
    rig.focus(snap("Let us meet at the cafe "))  # typed, then a pause
    assert [c.kind for c in rig.spec.calls] == ["complete"]
    assert "complete" not in [c.kind for c in rig.backend.calls]


def test_fake_test_backend_completes():
    assert "".join(FakeAiBackend().complete("Let us meet ", None)) == " tomorrow morning."


# ------------------------------------------------------------------ send-guard


def test_guard_chip_and_banner_for_a_missing_attachment_in_chat(rig):
    rig.focus(snap("hey, see attached for the numbers we talked about", wm_class="Slack",
                   title="general (Channel) - Acme - Slack"))
    assert rig.screen.chip.state is ChipState.GUARD
    rig.c.open_panel()
    assert rig.screen.panel.guard == GUARD_LABELS["missing_attachment"]
    rig.router.action("guard_silence", {"scope": "app"})
    assert rig.screen.chip.state is not ChipState.GUARD
    assert rig.screen.panel.guard is None


def test_guard_silence_rejects_a_bad_scope(rig):
    rig.focus(snap("see attached please, thanks a lot", wm_class="Slack"))
    rig.router.action("guard_silence", {"scope": "everywhere"})
    assert rig.screen.chip.state is ChipState.GUARD


# ------------------------------------------------------------------ select-anything


def _select(rig, text="teh plan is good, i think we shoud go", wm_class="Mousepad"):
    rig.sel.set(text, win(wm_class, xid=7))
    rig.f.actions["selection"]()


def test_selection_hotkey_opens_its_panel_and_routes_its_actions(rig):
    rig.focus()
    _select(rig)
    assert rig.screen.panel is not None and rig.screen.panel.mode == "selection"
    rig.focus(snap(TEXT))  # a redraw of the field must not take the selection's panel away
    assert rig.screen.panel is not None and rig.screen.panel.mode == "selection"
    rig.router.action("tone", {"key": "friendlier"})
    assert [c.kind for c in rig.backend.calls if c.kind == "rewrite"] == ["rewrite"]
    rig.router.action("sel_copy", {})
    assert rig.screen.clipboard == ["Thanks so much, will do!"]
    assert not rig.f.selection.is_open and rig.screen.panel is None


def test_selection_escape_closes_the_selection_not_the_field(rig):
    rig.focus()
    _select(rig)
    rig.router.escape()
    assert not rig.f.selection.is_open and rig.screen.panel is None
    rig.c.open_panel()
    rig.router.escape()
    assert rig.screen.panel is None


def test_opening_the_field_panel_closes_the_selection(rig):
    rig.focus()
    _select(rig)
    rig.c.open_panel()
    assert not rig.f.selection.is_open and rig.screen.panel.mode == "field"


def test_selection_paste_goes_to_capture_and_its_result_comes_back(rig):
    rig.focus()
    _select(rig)
    rig.router.action("sel_paste", {})
    assert rig.capture.pastes == ["Thanks so much, will do!"]
    assert rig.capture.paste_targets == [(7, "Mousepad")]
    rig.c.handle_apply_result(ApplyResult("paste", "unverified", ""))
    assert not rig.f.selection.is_open


def test_capture_link_sends_paste():
    sent = []
    link = CaptureLink(lambda fn: fn())
    conn = SimpleNamespace(send=lambda t, p: sent.append((t, p)), close=lambda: None)
    link.on_message(conn, "hello", {})
    link.send_paste("new text", 42, "Slack")
    assert sent == [(PASTE, {"text": "new text", "window": 42, "wm_class": "Slack"})]


# ------------------------------------------------------------------ ctl


class Recorder:
    def __init__(self, handles=()):
        self.got, self._handles = [], set(handles)

    def handles(self, t):
        return t in self._handles

    def handle(self, *args):
        self.got.append(args)


def test_ctl_edit_session_goes_to_the_edit_host_with_the_id(rig):
    sent = []

    class Host:
        def handle(self, payload, reply):
            self.reply = reply

    host = Host()
    router = CtlRouter(rig.c, rig.router.action, edit_host=host)
    router.handle("edit_session", {"id": 5, "text": "x"}, sent.append)
    assert sent == []  # the answer comes when the user is done
    host.reply({"action": "apply", "text": "y"})
    assert sent == [{"action": "apply", "text": "y", "id": 5}]


def test_ctl_backend_requests_go_to_backend_control(rig):
    bc, sent = Recorder({"backend_list"}), []
    router = CtlRouter(rig.c, rig.router.action, backend_control=bc)
    router.handle("backend_list", {"id": 1}, sent.append)
    (_type, _payload, reply), = bc.got
    reply({"ok": True, "choice": None, "backends": {}})
    assert sent == [{"ok": True, "choice": None, "backends": {}, "id": 1}]
    router.handle("status", {"id": 2}, sent.append)
    assert sent[-1]["ok"] and sent[-1]["id"] == 2


def test_ctl_a_client_that_left_does_not_raise(rig):
    def gone(_msg):
        raise ConnectionError

    CtlRouter(rig.c, rig.router.action).handle("ping", {"id": 1}, gone)


def test_test_hooks_answer_only_with_glance_test(rig, monkeypatch):
    rig.focus()
    monkeypatch.delenv("GLANCE_TEST", raising=False)
    assert handle_control(rig.c, "test_state", {})["error"] == "unknown request"
    monkeypatch.setenv("GLANCE_TEST", "1")
    st = handle_control(rig.c, "test_state", {})
    assert st["ok"] and st["snapshot"]["text"] == TEXT


# ------------------------------------------------------------------ status, session


def test_hotkeys_status_lists_configured_names_only():
    st = hotkeys_status({"panel": "x", "pause": "y", "ac_tab": "Tab", "escape": "Escape"},
                        Config().general.hotkeys)
    assert st == {"hotkeys_grabbed": "panel,pause"}


def test_session_problem():
    assert session_problem({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}) is None
    assert session_problem({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"})
    assert session_problem({"XDG_SESSION_TYPE": "x11"})


# ------------------------------------------------------------------ onboarding / tray


class FakeWindow:
    def __init__(self, **kw):
        self.kw, self.shown = kw, 0

    def show(self):
        self.shown += 1


class Sup:
    def __init__(self):
        self.used, self.refreshed = [], 0

    def healths(self, refresh=False):
        self.refreshed += refresh
        return {"claude": Health(True, True, "signed in"), "codex": Health(False, None, "")}

    def use(self, name):
        self.used.append(name)

    def test(self, name):
        return True, "ok"


def _setup(choice=None):
    cfg = Config(backend=BackendCfg(choice=choice))
    posted, windows, tray = [], [], SimpleNamespace(rows=None)
    tray.set_backends = lambda rows: setattr(tray, "rows", rows)
    keys = SimpleNamespace(saved={})
    keys.set = lambda p, k: keys.saved.update({p: k})
    setup = AiSetup(Sup(), lambda: cfg, keys, lambda w, d: d(w()), posted.append,
                    lambda **kw: windows.append(FakeWindow(**kw)) or windows[-1], tray,
                    lambda *a: None, lambda: None)
    return setup, posted, windows, tray, keys


def test_onboarding_opens_once_when_no_backend_is_chosen():
    setup, _posted, windows, _tray, _keys = _setup()
    setup.offer_on_start()
    setup.offer_on_start()
    assert len(windows) == 1 and windows[0].shown == 1
    setup.open()  # the tray's "Set up..." or the "Connect an AI" chip
    assert len(windows) == 1 and windows[0].shown == 2


def test_onboarding_not_offered_with_a_backend_chosen():
    setup, _posted, windows, _tray, _keys = _setup("claude")
    setup.offer_on_start()
    assert windows == []


def test_onboarding_callbacks_post_to_the_main_loop():
    setup, posted, _windows, tray, keys = _setup()
    setup.use("claude")  # on the window's worker thread
    assert setup._sup.used == ["claude"] and len(posted) == 1 and tray.rows is None
    posted.pop()()
    assert [r.name for r in tray.rows][:1] == ["claude"]
    setup.set_key("anthropic", "sk-test")
    assert keys.saved == {"anthropic": "sk-test"} and len(posted) == 1
    got = []
    setup.test("claude", lambda ok, detail: got.append((ok, detail)))
    assert got == [(True, "ok")]


def test_tray_rows_use_cached_health_the_window_and_key_and_test_refresh():
    setup, posted, windows, tray, _keys = _setup()
    setup.update_tray()
    assert setup._sup.refreshed == 0 and tray.rows is not None
    setup.open()
    windows[0].kw["rows_provider"]()
    assert setup._sup.refreshed == 1  # Re-check in the window is an explicit ask
    setup.set_key("anthropic", "sk-test")
    setup.test("claude", lambda ok, detail: None)
    assert setup._sup.refreshed == 3 and len(posted) == 2


def test_api_settings_reach_the_live_api_backends(tmp_path):
    """`backend_api` / the window write config; the reload must reach both api instances,
    so a key saved for the new provider shows as connected without a restart."""
    from glance.backends.api import ApiBackend
    from glance.backends.budget import Budget
    from glance.backends.keystore import MemoryKeyStore
    from glance.backends.supervisor import BackendSupervisor
    from glance.daemon.wiring import configure_api

    keys = MemoryKeyStore()
    main, spec = {"api": ApiBackend(keys, "anthropic")}, {"api": ApiBackend(keys, "anthropic")}
    cfg = BackendCfg(choice="api")
    clock = FakeClock()
    sup = BackendSupervisor(main, lambda: cfg, Budget(clock, lambda: cfg, tmp_path / "u.json"),
                            clock, lambda: False, lambda st: None, speculative=spec)
    keys.set("openai", "sk-o")
    assert sup.healths(True)["api"].signed_in is False
    cfg = replace(cfg, api_provider="openai", api_model="gpt-x",
                  api_base_url="https://llm.example/v1")
    assert configure_api((main, spec), cfg) is True
    assert sup.healths(True)["api"].signed_in is True
    assert spec["api"]._prepare()[0] == "https://llm.example/v1/chat/completions"
    assert configure_api((main, spec), cfg) is False  # nothing new: no re-check needed
    assert configure_api((main, spec), replace(cfg, api_provider="bogus")) is False


def test_selection_read_never_blocks_the_main_loop(tmp_path):
    """An owner that never answers PRIMARY: trigger returns at once (the read is on its own
    thread) and the panel opens when the read is posted back to the main loop."""
    import threading

    rig = Rig(tmp_path)
    assert rig.f.selection._run_read is not rig.bg  # its own thread, not the AI pool
    posted, release = [], threading.Event()

    class Stuck(FakeSelection):
        def primary_text(self, max_bytes):
            release.wait(5)
            return "teh plan is good, i think we shoud go"

    sel = Stuck()
    sel.set("x", win("Mousepad", xid=7))
    rig.f.selection._selection = sel
    from glance.daemon.wiring import thread_runner

    rig.f.selection._run_read = thread_runner(posted.append, "test-read")
    rig.focus()
    t0 = time.monotonic()
    rig.f.actions["selection"]()
    assert time.monotonic() - t0 < 0.2 and not rig.f.selection.is_open
    rig.clock.advance(50)  # the main loop keeps ticking meanwhile
    release.set()
    deadline = time.monotonic() + 5
    while not posted and time.monotonic() < deadline:
        time.sleep(0.01)
    posted.pop()()
    assert rig.f.selection.is_open and rig.screen.panel.mode == "selection"


def test_a_selection_read_that_lands_after_a_close_is_dropped(tmp_path):
    held = []
    rig = Rig(tmp_path)
    rig.f.selection._run_read = lambda work, done: held.append((work, done))
    rig.focus()
    _select(rig)
    rig.f.selection.close()  # e.g. a pause, before the owner answered
    work, done = held.pop()
    done(work())
    assert not rig.f.selection.is_open and rig.screen.panel is None
