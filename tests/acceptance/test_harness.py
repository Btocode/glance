"""The acceptance harness itself: no browser, no display."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import httpx

from tests.acceptance.harness import FixtureServer, edit_script, norm

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLES = ("prosemirror", "slate", "lexical", "quill")


def test_edit_script_is_deterministic():
    a, b = edit_script(7), edit_script(7)
    assert a == b and len(a) == 200
    assert edit_script(8) != a
    assert {op.kind for op in a} == {
        "type", "backspace", "arrow", "home_end", "set_caret", "paste", "select_type"}
    assert len(edit_script(3, n=50)) == 50


def test_edit_script_words_include_punctuation_and_emoji():
    typed = "".join(op.text for op in edit_script(1, n=2000) if op.kind == "type")
    assert any(ch in typed for ch in ",.;!?'")
    assert any(ord(ch) > 0xFFFF for ch in typed)


def test_norm():
    assert norm("é") == "é"            # NFC
    assert norm("a b") == "a b"              # NBSP
    assert norm("abc\n") == "abc" and norm("abc\n\n") == "abc\n"  # one trailing newline only
    assert norm("") == ""


def test_fixture_server_state_and_cmd(tmp_path):
    (tmp_path / "x.html").write_text("<p>hi</p>")
    server = FixtureServer(tmp_path)
    base = server.start()
    try:
        assert base.startswith("http://127.0.0.1:")
        assert httpx.get(f"{base}/x.html").text == "<p>hi</p>"
        assert server.state() == {}
        httpx.post(f"{base}/state", content=json.dumps({"text": "ab", "caret": 2, "seq": 1}))
        assert server.state() == {"text": "ab", "caret": 2, "seq": 1}
        assert server.wait_state(lambda s: s.get("seq") == 1, 1.0)["caret"] == 2

        got: list[dict] = []
        poller = threading.Thread(
            target=lambda: got.append(httpx.get(f"{base}/cmd", timeout=5).json()))
        poller.start()
        server.cmd("setCaret", n=1)
        poller.join(5)
        assert got == [{"op": "setCaret", "n": 1}]
    finally:
        server.stop()


def test_wait_state_times_out(tmp_path):
    server = FixtureServer(tmp_path)
    server.start()
    try:
        try:
            server.wait_state(lambda s: s.get("seq") == 99, 0.2)
        except TimeoutError:
            return
        raise AssertionError("expected TimeoutError")
    finally:
        server.stop()


def test_dist_bundles_exist_and_match_lockfile():
    lock = json.loads((FIXTURES / "package-lock.json").read_text())
    package = json.loads((FIXTURES / "package.json").read_text())
    for name, version in package["dependencies"].items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == version
    for name in BUNDLES:
        assert (FIXTURES / "dist" / f"{name}.js").stat().st_size > 1000, name
        assert (FIXTURES / f"{name}.html").exists()
    for page in ("textarea", "contenteditable", "password", "login/index"):
        assert (FIXTURES / f"{page}.html").exists()


def test_dist_is_built_from_the_current_sources():
    import hashlib

    names = sorted([*(p.relative_to(FIXTURES).as_posix() for p in (FIXTURES / "src").glob("*.js")),
                    "bridge.js", "package-lock.json"])
    h = hashlib.sha256()
    for name in names:
        h.update(name.encode() + b"\0" + (FIXTURES / name).read_bytes() + b"\0")
    assert (FIXTURES / "dist" / ".src-hash").read_text().strip() == h.hexdigest(), \
        "fixtures/src changed: run scripts/build-fixtures.sh"


def test_typist_clipboard_lives_on_the_private_display(session):
    from tests.acceptance.harness import Typist

    typist = Typist(session)
    try:
        clip = typist.clipboard()
        assert clip._owner._d.get_display_name() == session.display
        assert os.environ["DISPLAY"] == session.display  # the whole test process is private
    finally:
        typist.close()
