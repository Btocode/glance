"""Glance Shot's bar and controller: the actions (Copy, Save, Extract text, Explain, Ask, Translate), the one-time notice before an image goes out, the privacy check, with fakes."""
from __future__ import annotations

import dataclasses

from glance.backends.supervisor import AiUnavailable
from glance.config import Config, ShotCfg
from glance.protocol import Rect
from glance.shot import bar_model as bm
from glance.shot import ocr
from glance.shot.consent import ImageConsent
from glance.shot.controller import (
    COPIED,
    ESCAPE,
    PickResult,
    ShotController,
    apply_fixes,
)
from glance.shot.image import Shot, encode_png
from glance.shot.privacy import private_reason
from glance.ui.ports import WindowInfo
from tests.fakes import FakeHotkeys, FakeNotifier
from tests.shot_fakes import FakeEngine, VisionFake

# ------------------------------------------------------------------ the bar


def test_bar_buttons_order_and_tips():
    row = bm.buttons(vision=False, ai=True, images="ask", ocr=True, private=False)
    assert [b.label for b in row] == ["Copy", "Save", "Extract text", "Explain", "Ask…",
                                      "Translate", "Close"]
    assert row[0].primary
    tips = {b.key: b.tip for b in row if b.muted}
    assert tips == {"explain": bm.NEEDS_VISION, "ask": bm.NEEDS_VISION}
    row = bm.buttons(vision=True, ai=True, images="never", ocr=False, private=False)
    tips = {b.key: b.tip for b in row if b.muted}
    assert tips["explain"] == bm.IMAGES_OFF and tips["extract"] == bm.OCR_OFF
    assert tips["translate"] == bm.OCR_OFF
    row = bm.buttons(vision=True, ai=True, images="allow", ocr=True, private=True)
    assert {b.key for b in row if b.muted} == {"explain", "ask", "translate"}
    assert "Anything visible in it is sent." in bm.notice_text("Anthropic (api.anthropic.com)")


# ------------------------------------------------------------------ privacy


def win(cls: str, title: str, rect: Rect | None) -> WindowInfo:
    return WindowInfo(cls, title, 1, 1, rect)


def test_private_windows_in_the_region():
    cfg = Config()
    region = Rect(100, 100, 300, 200)
    assert private_reason(region, [win("gedit", "notes", Rect(0, 0, 800, 600))], cfg) is None
    assert private_reason(region, [win("KeePassXC", "db", Rect(350, 250, 400, 400))],
                          cfg) == "app_denylist"
    assert private_reason(region, [win("KeePassXC", "db", Rect(900, 900, 50, 50))], cfg) is None
    assert private_reason(region, [win("firefox", "Mozilla Firefox Private Browsing",
                                       Rect(0, 0, 500, 500))], cfg) == "incognito"
    assert private_reason(region, [win("google-chrome", "My bank - Chrome",
                                       Rect(0, 0, 500, 500))], cfg) == "url_denylist"
    assert private_reason(region, [], cfg, password=Rect(390, 290, 100, 20)) == "password"
    assert private_reason(region, [win("keepassxc", "x", None)], cfg) is None


# ------------------------------------------------------------------ the controller


class FakeUi:
    def __init__(self) -> None:
        self.on_done = None
        self.bars: list[tuple[bm.ShotBarView, Rect]] = []
        self.hidden = 0
        self.copied: list[bytes] = []
        self.cancelled = 0
        self.copy_ok = True

    def pick(self, on_done):
        self.on_done = on_done

    def cancel_pick(self):
        self.cancelled += 1

    def show_bar(self, view, region):
        self.bars.append((view, region))

    def hide_bar(self):
        self.hidden += 1

    def copy_image(self, png):
        self.copied.append(png)
        return self.copy_ok

    @property
    def view(self) -> bm.ShotBarView:
        return self.bars[-1][0]


class FakeOverlay:
    def __init__(self) -> None:
        self.copied: list[str] = []
        self.asked = None

    def copy_to_clipboard(self, text):
        self.copied.append(text)

    def ask_text(self, prompt, on_done):
        self.asked = (prompt, on_done)


class FakeSup:
    def __init__(self, vision=True, connected=True, out="It is an error dialog."):
        self.vision_on, self.connected, self.out = vision, connected, out
        self.images: list[tuple[str, bytes, str, str]] = []
        self.texts: list[tuple[str, str]] = []
        self.cancels = 0
        self.raise_ = None

    def active(self):
        return "api" if self.connected else None

    def vision_backend(self):
        if not (self.vision_on and self.connected):
            return None
        b = VisionFake("api", True)
        b.vision_key = lambda: "api:anthropic:api.anthropic.com"
        b.vision_label = lambda: "Anthropic (api.anthropic.com)"
        return "api", b

    def see(self, name, png, system, user):
        if self.raise_:
            raise self.raise_
        self.images.append((name, png, system, user))
        return iter([self.out])

    def answer_text(self, system, user):
        self.texts.append((system, user))
        return iter(["Hola mundo"])

    def cancel_all(self):
        self.cancels += 1


def run_now(work, done):
    done(work())


class Grammar:
    alive = True

    def check(self, text):
        from glance.grammar.issues import Issue

        i = text.find("teh")
        if i < 0:
            return []
        return [Issue("h:1", "harper", "SpellCheck", "typo", i, i + 3, ("the",), True)]


class Rig:
    def __init__(self, tmp_path, cfg: Config | None = None, sup=None, engines=None,
                 text_panel=None, password=None):
        self.cfg = cfg or Config()
        self.ui, self.overlay, self.sup = FakeUi(), FakeOverlay(), sup or FakeSup()
        self.hotkeys, self.notifier = FakeHotkeys(), FakeNotifier()
        self.consent = ImageConsent(tmp_path / "consent.json")
        self.engines = engines if engines is not None else [FakeEngine("ocrs", "Hello teh world")]
        self.c = ShotController(
            ui=self.ui, cfg=lambda: self.cfg, supervisor=self.sup, overlay=self.overlay,
            notifier=self.notifier, hotkeys=self.hotkeys, run_bg=run_now, consent=self.consent,
            grammar=Grammar(), password_rect=lambda: password,
            ocr_engines=lambda mode: [] if mode == "off" else self.engines,
            text_panel=text_panel, now=lambda: 1_790_000_000.0)

    def shoot(self, rect=None, windows=()):
        rect = rect or Rect(100, 100, 400, 300)
        assert self.c.trigger()
        png = encode_png(b"\x00" * 12, 2, 2)
        self.ui.on_done(PickResult(Shot(png, 2, 2, rect), tuple(windows)))
        return png


def test_trigger_picks_and_shows_the_bar(tmp_path):
    r = Rig(tmp_path)
    r.shoot()
    v = r.ui.view
    assert v.size == "400 × 300" and v.buttons[0].label == "Copy"
    assert r.ui.bars[-1][1] == Rect(100, 100, 400, 300)
    assert ESCAPE in r.hotkeys.grabs
    r.hotkeys.press(ESCAPE)
    assert not r.c.is_open and r.ui.hidden == 1 and ESCAPE not in r.hotkeys.grabs


def test_disabled_shot_does_nothing(tmp_path):
    r = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(enabled=False)))
    assert r.c.trigger() is False and r.ui.on_done is None


def test_cancelled_pick_shows_nothing(tmp_path):
    r = Rig(tmp_path)
    r.c.trigger()
    r.ui.on_done(None)
    assert not r.c.is_open and r.ui.bars == []


def test_copy_and_save(tmp_path, monkeypatch):
    save_dir = tmp_path / "Pictures" / "Glance"
    cfg = dataclasses.replace(Config(), shot=ShotCfg(save_dir=str(save_dir)))
    r = Rig(tmp_path, cfg=cfg)
    png = r.shoot()
    r.c.action("copy")
    assert r.ui.copied == [png] and r.ui.view.banner == COPIED
    assert not save_dir.exists()  # nothing on disk until Save
    r.c.action("save")
    files = list(save_dir.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == png
    assert files[0].name.startswith("Glance Shot ") and files[0].suffix == ".png"
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert r.ui.view.banner.startswith("Saved to ")
    r.c.action("save")
    assert len(list(save_dir.iterdir())) == 2  # never overwrites


def test_extract_text_shows_the_text_card(tmp_path):
    r = Rig(tmp_path)
    r.shoot()
    r.c.action("extract")
    res = r.ui.view.result
    assert res.title == "Text" and res.text == "Hello teh world" and res.state == "ready"
    assert [a.key for a in res.actions] == ["text_copy", "text_fix", "text_translate",
                                            "text_explain"]
    r.c.action("text_fix")
    assert r.ui.view.result.text == "Hello the world"
    r.c.action("text_copy")
    assert r.overlay.copied == ["Hello the world"]
    r.c.action("text_translate")
    assert r.ui.view.result.title == "Translation" and r.ui.view.result.text == "Hola mundo"
    assert "English" in r.sup.texts[0][0] and "Hello the world" in r.sup.texts[0][1]
    assert r.sup.images == []  # text actions never send the image


def test_extract_text_hands_over_to_the_text_panel_seam(tmp_path):
    got = []
    r = Rig(tmp_path, text_panel=lambda text, region, private: got.append(
        (text, region, private)))
    r.shoot()
    r.c.action("extract")
    ((text, region, private),) = got
    assert text == "Hello teh world" and not private and not r.c.is_open
    assert region == Rect(100, 100, 400, 300)


def test_extract_text_failure_says_why(tmp_path):
    r = Rig(tmp_path, engines=[])
    r.shoot()
    r.c.action("extract")
    assert r.ui.view.result.state == "failed" and r.ui.view.result.note == ocr.NO_ENGINE


def test_ocr_off_mutes_extract(tmp_path):
    r = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(ocr="off")))
    r.shoot()
    r.c.action("extract")
    assert r.ui.view.banner == bm.OCR_OFF and r.ui.view.result is None


def test_explain_asks_once_then_sends(tmp_path):
    r = Rig(tmp_path)
    png = r.shoot()
    r.c.action("explain")
    v = r.ui.view
    assert v.notice is not None and r.sup.images == []
    assert v.notice.text == ("This sends the image to Anthropic (api.anthropic.com). "
                             "Anything visible in it is sent.")
    r.c.action("notice_send")
    assert r.sup.images[0][1] == png and r.ui.view.result.text == "It is an error dialog."
    assert r.ui.view.notice is None
    r.c.action("explain")
    assert r.ui.view.notice is not None  # "Send" was for this once
    r.c.action("notice_always")
    assert len(r.sup.images) == 2
    r.c.close()
    r.shoot()
    r.c.action("explain")
    assert r.ui.view.notice is None and len(r.sup.images) == 3  # remembered


def test_notice_cancel_sends_nothing(tmp_path):
    r = Rig(tmp_path)
    r.shoot()
    r.c.action("explain")
    r.c.action("notice_cancel")
    assert r.sup.images == [] and r.ui.view.notice is None


def test_ask_takes_a_question_first(tmp_path):
    r = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(ai_images="allow")))
    r.shoot()
    r.c.action("ask")
    prompt, done = r.overlay.asked
    assert prompt == bm.ASK_PROMPT and r.sup.images == []
    done("what is the error?", True)
    assert "what is the error?" in r.sup.images[0][3] and r.ui.view.result.title == "Answer"


def test_never_and_no_vision_and_private_send_nothing(tmp_path):
    r = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(ai_images="never")))
    r.shoot()
    r.c.action("explain")
    assert r.sup.images == [] and r.ui.view.banner == bm.IMAGES_OFF
    r = Rig(tmp_path, sup=FakeSup(vision=False))
    r.shoot()
    r.c.action("explain")
    assert r.ui.view.banner == bm.NEEDS_VISION and r.ui.view.notice is None


def test_any_window_may_be_shot_and_sent_on_click(tmp_path):
    # the user's own capture is never blocked: a password manager in the region is fine
    r = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(ai_images="allow")))
    r.shoot(windows=[WindowInfo("bitwarden", "Vault", 1, 2, Rect(0, 0, 300, 300))])
    assert r.ui.view.banner != bm.PRIVATE and not r.ui.view.warn
    r.c.action("explain")
    assert len(r.sup.images) == 1


def test_password_field_under_the_region_does_not_block(tmp_path):
    r = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(ai_images="allow")),
            password=Rect(150, 150, 200, 30))
    r.shoot()
    r.c.action("explain")
    assert len(r.sup.images) == 1


def test_ai_unavailable_and_late_results(tmp_path):
    sup = FakeSup()
    sup.raise_ = AiUnavailable("limit")
    r = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(ai_images="allow")),
            sup=sup)
    r.shoot()
    r.c.action("explain")
    assert r.ui.view.result.state == "failed" and "limit" in r.ui.view.result.note

    held = []
    r2 = Rig(tmp_path, cfg=dataclasses.replace(Config(), shot=ShotCfg(ai_images="allow")))
    r2.c._run_bg = lambda work, done: held.append((work, done))
    r2.shoot()
    r2.c.action("explain")
    r2.c.close()
    assert r2.sup.cancels == 1
    work, done = held[0]
    done(work())  # arrives after the bar closed: dropped
    assert not r2.c.is_open


def test_retrigger_closes_the_open_bar(tmp_path):
    r = Rig(tmp_path)
    r.shoot()
    r.c.trigger()
    assert not r.c.is_open and r.c.picking and r.ui.hidden == 1


def test_apply_fixes_skips_overlaps():
    from glance.grammar.issues import Issue

    issues = [Issue("1", "harper", "R", "m", 0, 3, ("The",), True),
              Issue("2", "harper", "R", "m", 1, 4, ("X",), True),
              Issue("3", "harper", "R", "m", 8, 9, (), True)]
    out, n = apply_fixes("teh cat a", issues)
    assert (out, n) == ("tXcat a", 1)  # the later of two overlapping fixes wins


def test_refresh_draws_the_bar_again(tmp_path):
    r = Rig(tmp_path)
    r.c.refresh()  # nothing open: nothing drawn
    assert r.ui.bars == []
    r.shoot()
    n = len(r.ui.bars)
    r.c.refresh()
    assert len(r.ui.bars) == n + 1
