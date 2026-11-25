"""Nothing typed reaches the disk (spec §8.5): one scripted session, then a search of every place
Glance writes: its XDG config/data/runtime/state/cache directories, its stderr (the log), the
empty directory backends run in, and /tmp/glance*.

The session types 40 marker sentences `ZQXJ-<n> ...` into a GTK app and a Chrome page, accepts
fixes, asks for a rewrite, accepts an autocomplete suggestion and uses select-anything, with
the fake CLI backend as the backend (so the whole shield -> subprocess path runs). The marker
is never added to the dictionary here (a word added by the user is stored by design; see
`test_dictionary_add_is_the_only_text_write`).

    .venv/bin/pytest -m apps tests/acceptance/test_disk_audit.py -v
"""
from __future__ import annotations

import glob
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.acceptance.harness import (
    Browser,
    BrowserPage,
    GlancedUnderTest,
    GtkApp,
    Typist,
    wait_for,
)
from tests.acceptance.test_capture_matrix import NO_GO

pytestmark = pytest.mark.apps

EXPECTED_DATA = {"dictionary.txt", "ignored.json", "overrides.json", "usage.json",
                 "desktop-overrides.json", "install-manifest.json", "bin"}
SENTENCES = 40


def grep(needle: str, paths: list[Path | str]) -> list[str]:
    done = subprocess.run(["grep", "-r", "-l", "-a", needle, *map(str, paths)],
                          capture_output=True, text=True, check=False)
    assert done.returncode in (0, 1), done.stderr
    return done.stdout.split()


def files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink())


def places(g: GlancedUnderTest) -> list[Path | str]:
    """Everything Glance may write: its directories, its log, the backends' empty cwd (it
    lives under the runtime dir), /tmp/glance*, and the real home's Glance directories."""
    home = Path.home()
    real = [home / ".local/share/glance", home / ".config/glance", home / ".cache/glance",
            home / ".local/state/glance"]
    return [*g.dirs.values(), g.stderr_path, *glob.glob("/tmp/glance*"),
            *[p for p in real if p.exists()]]


class Scripted:
    """What the scripted session left behind."""

    glanced: GlancedUnderTest
    sentences: int = 0
    counts: dict[str, int]


@pytest.fixture(scope="module")
def audited(session, tmp_path_factory, fixture_server_module, profile_root):
    if "chrome" in NO_GO:
        pytest.skip(NO_GO["chrome"])
    server, base = fixture_server_module
    root = tmp_path_factory.mktemp("audit")
    typist = Typist(session)
    g = GlancedUnderTest(session, root, backend="fake_cli").start()
    app = GtkApp(session, typist, undo=True, wm_class="Slack")
    page = BrowserPage(session, typist, server,
                       Browser("chrome", profile_root, session, server),
                       f"{base}/textarea.html")
    out = Scripted()
    out.glanced, out.counts = g, {"fixes": 0, "rewrites": 0, "completions": 0, "selections": 0}
    started = time.monotonic()
    try:
        app.start()
        wait_for(lambda: g.status().get("field"), 30.0)
        n = 0
        for target in (app, page):
            if target is page:
                page.start()
                time.sleep(1.0)
            for _ in range(SENTENCES // 2):
                n += 1
                typist.chord("Control_L", "a")
                typist.type(f"ZQXJ-{n} Teh quick brown fox jump over the lazy dog today.")
                time.sleep(1.2)
                if n % 5 == 0:  # accept the first fix
                    issues = g.state().get("issues") or []
                    if issues:
                        g.request("test_panel_action",
                                  {"action": "accept", "args": {"issue_id": issues[0]}})
                        out.counts["fixes"] += 1
                        time.sleep(0.8)
                if n % 10 == 0:  # a rewrite in the first tone
                    typist.chord("Super_L", "Alt_L", "1")
                    out.counts["rewrites"] += 1
                    time.sleep(2.0)
                if n % 10 == 5:  # an autocomplete suggestion, accepted
                    typist.chord("Control_L", "End")
                    typist.chord("Super_L", "Alt_L", "Return")
                    time.sleep(2.0)
                    if g.status().get("autocomplete_visible"):
                        typist.key("Tab")
                        out.counts["completions"] += 1
                        time.sleep(0.5)
                if n % 10 == 7:  # select-anything, then Copy
                    typist.chord("Control_L", "a")
                    time.sleep(0.3)
                    typist.chord("Super_L", "Alt_L", "a")
                    time.sleep(1.5)
                    if g.status().get("selection_open"):
                        g.request("test_panel_action", {"action": "sel_copy", "args": {}})
                        out.counts["selections"] += 1
                    time.sleep(0.5)
            if target is page:
                page.stop()
        out.sentences = n
        out.seconds = time.monotonic() - started
        assert "ZQXJ" in g.stdin_log.read_text(), "the fake backend never saw the session"
        g.stop()  # then stop Glance and look
        app.stop()
        yield out
    finally:
        g.stop()
        app.stop()
        page.stop()
        typist.close()


def test_session_really_exercised_glance(audited):
    c = audited.counts
    print("session", audited.sentences, "sentences", round(audited.seconds), "s", c)
    assert audited.sentences == SENTENCES
    # the backend is rate limited (10 calls a minute) and accepted fixes rewrite the marker
    # word, so only some sentences reach it; some must have, or the audit proves nothing
    assert audited.glanced.stdin_log.read_text().count("ZQXJ") >= 3


def test_no_typed_text_on_disk_or_in_logs(audited):
    g = audited.glanced
    hits = grep("ZQXJ", places(g))
    assert hits == [], hits
    # the directory every backend subprocess runs in (`backends.proc.empty_dir()` of that daemon)
    empty = g.dirs["rt"] / "glance" / "empty"
    assert empty.is_dir() and os.listdir(empty) == []
    # and the text around the marker
    assert grep("quick brown fox", places(g)) == []
    assert "Traceback" not in g.stderr_path.read_text()


def test_only_expected_files_exist(audited):
    g = audited.glanced
    data = g.dirs["data"] / "glance"
    present = {p.name for p in data.iterdir()}
    assert present <= EXPECTED_DATA, present - EXPECTED_DATA
    listing = {n: [str(p.relative_to(d)) for p in files(d)] for n, d in g.dirs.items()}
    print("files Glance left:", listing)
    assert (g.dirs["cfg"] / "glance").exists()


def test_dictionary_add_is_the_only_text_write(session, tmp_path):
    typist = Typist(session)
    # the backend errors, so the only issues are Harper's (the fake analysis would add its own)
    g = GlancedUnderTest(session, tmp_path, backend="fake_cli", fake_mode="error").start()
    app = GtkApp(session, typist, undo=True, wm_class="Slack",
                 text="Please look at Zqxjword and tell me what you think of it today.")
    app.start()
    try:
        wait_for(lambda: g.status().get("field"), 30.0)
        # the issue on the unknown word: its id is source:rule:start:end, start = 15
        try:
            want = wait_for(lambda: [i for i in (g.state().get("issues") or [])
                                     if i.endswith(":15:23")], 20.0)
        except TimeoutError:
            raise AssertionError(g.state()) from None
        g.request("test_panel_action", {"action": "add_dict", "args": {"issue_id": want[0]}})
        dictionary = g.dirs["data"] / "glance" / "dictionary.txt"
        assert wait_for(lambda: dictionary.exists() and "Zqxjword" in dictionary.read_text(),
                        5.0)
        time.sleep(1.0)
        g.stop()
        hits = grep("Zqxjword", places(g))
        assert hits == [str(dictionary)], hits
        assert grep("look at", places(g)) == []  # the sentence itself went nowhere
    finally:
        g.stop()
        app.stop()
        typist.close()
