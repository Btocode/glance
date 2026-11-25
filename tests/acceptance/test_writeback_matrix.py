"""Write-back matrix (spec §8.2): 500 scripted applies per target through the shipped glanced
(verified write-back path, ctl `test_apply`), judged against the editor's own text.

    .venv/bin/pytest -m apps tests/acceptance/test_writeback_matrix.py -v -rP

Private Xvfb, own D-Bus and AT-SPI bus, throwaway browser profiles, clipboard on the private
display; nothing touches the desktop in use.
"""
from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from tests.acceptance.harness import (
    BROWSERS,
    Browser,
    BrowserPage,
    ClipProbe,
    GlancedUnderTest,
    GtkApp,
    Typist,
    norm,
    wait_for,
)
from tests.acceptance.harness import run_writeback_matrix as run
from tests.acceptance.test_capture_matrix import NO_GO

pytestmark = pytest.mark.apps

PAGES = ("textarea", "contenteditable")
N = int(os.environ.get("GLANCE_WB_N", "500"))


def _need(kind: str) -> None:
    if kind in NO_GO:
        pytest.skip(NO_GO[kind])
    if not os.access(BROWSERS[kind], os.X_OK) and shutil.which(kind) is None:
        pytest.skip(f"{kind} is not installed")


@pytest.fixture
def typist(session):
    t = Typist(session)
    yield t
    t.close()


@pytest.fixture
def probe(session):
    p = ClipProbe(session.display)
    yield p
    p.close()


@pytest.fixture
def glanced(session, tmp_path):
    g = GlancedUnderTest(session, tmp_path, backend="fake")
    yield g
    g.stop()


def _record(name: str, r) -> None:
    line = (f"{name}\ttotal={r.total} verified={r.verified} reverted={r.reverted} "
            f"refused={r.refused} stale={r.stale} failed={r.failed} unverified={r.unverified} "
            f"no_result={r.no_result} not_synced={r.not_synced} corrupt={len(r.silent_corruptions)} "
            f"undo={r.undo_ok}/{r.undo_checked} clip_violations={r.clipboard_violations}")
    print(line)
    print("  not verified:", r.details)
    for note in r.notes[:5]:
        print("  note:", note)
    if os.environ.get("GLANCE_MATRIX_LOG"):
        with open(os.environ["GLANCE_MATRIX_LOG"], "a") as f:
            f.write(line + "\n")


def _check(r) -> None:
    assert r.silent_corruptions == [], r.silent_corruptions[:5]
    assert r.failed_dirty == 0
    assert r.undo_ok == r.undo_checked, r.undo_failures[:5]
    assert r.undo_checked > 0
    assert r.clipboard_violations == 0, r.notes[:5]
    assert r.verified / max(r.total, 1) >= 0.95, (r.verified, r.total, r.notes[:5])


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("kind", ["chrome", "brave", "firefox"])
def test_applies_never_corrupt(kind, page, session, typist, probe, glanced, fixture_server,
                               profile_root):
    _need(kind)
    server, base = fixture_server
    target = BrowserPage(session, typist, server,
                         Browser(kind, profile_root, session, server), f"{base}/{page}.html")
    target.start()
    glanced.start()
    try:
        wait_for(lambda: glanced.snapshot_text() is not None, 30.0)
        r = run(target, glanced, typist, probe, n=N)
        _record(f"{kind}/{page}", r)
        _check(r)
    finally:
        target.stop()


def test_applies_never_corrupt_gtk(session, typist, probe, glanced):
    # A GtkSourceView under gedit's class: a typed replace would be two undo steps there
    # (S4.4), so gedit's built-in default pastes and one Ctrl+Z restores.
    target = GtkApp(session, typist, undo=True, wm_class="gedit")
    target.start()
    glanced.start()
    try:
        wait_for(lambda: glanced.snapshot_text() is not None, 30.0)
        r = run(target, glanced, typist, probe, n=N)
        _record("gtk/sourceview", r)
        _check(r)
    finally:
        target.stop()


def test_apply_while_typing_never_corrupts(session, typist, probe, glanced, fixture_server,
                                           profile_root):
    _need("chrome")
    server, base = fixture_server
    target = BrowserPage(session, typist, server,
                         Browser("chrome", profile_root, session, server),
                         f"{base}/textarea.html")
    target.start()
    glanced.start()
    try:
        wait_for(lambda: glanced.snapshot_text() is not None, 30.0)
        typed = "".join(chr(ord("a") + i % 26) for i in range(150))
        done = threading.Event()
        typist2 = Typist(session)  # its own X connection: the thread must not share one

        def type_forever() -> None:
            for ch in typed:
                typist2.type(ch)
                time.sleep(0.03)
            done.set()

        th = threading.Thread(target=type_forever, daemon=True)
        th.start()
        counts: dict[str, int] = {}
        corrupt = []
        for k in range(100):
            probe.own(f"CLIP-SENTINEL-{k}")
            before = target.truth().get("text", "")
            got = glanced.request("test_apply", {"start": 0, "end": 0,
                                                 "replacement": "ZZ", "method": "auto"})
            aid = got.get("apply_id")
            status = "not_sent"
            if aid:
                try:
                    status = wait_for(lambda a=aid: (glanced.request(
                        "test_apply_result", {"apply_id": a}).get("result") or {}
                    ).get("status"), 20.0, 0.05)
                except TimeoutError:
                    status = "no_result"
            counts[status] = counts.get(status, 0) + 1
            after = target.truth().get("text", "")
            # While typing, the text legitimately grows; judge only: a non-verified apply must
            # not have inserted its replacement text.
            if status not in ("verified",) and after.count("ZZ") > before.count("ZZ"):
                corrupt.append((k, status))
            time.sleep(0.05)
            if done.is_set():
                break
        th.join(30)
        typist2.close()
        time.sleep(0.5)
        final = target.truth().get("text", "")
        print("apply-while-typing", counts)
        assert corrupt == [], corrupt
        assert counts.get("failed", 0) == 0, counts  # none started while the user typed
        # every typed character survives, in order (the ZZ edits only insert at 0)
        it = iter(norm(final))
        assert all(ch in it for ch in typed), (typed, final)
    finally:
        target.stop()
