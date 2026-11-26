"""The parts of the X adapters that need no X server."""
from glance.writeback.clipboard_xlib import CHUNK_BYTES, same_client
from glance.writeback.clipboard_xlib import chunks as incr_chunks
from glance.writeback.keys_xlib import char_keysym

MASK = 0x1FFFFF  # resource-id-mask of the X servers in use: the client is the bits above


def test_same_client_compares_the_resource_id_base():
    assert same_client(0x3A00007, 0x3A1F2C4, MASK)       # a window and a helper window
    assert not same_client(0x3A00007, 0x3C00007, MASK)   # another client
    assert not same_client(0x1200003, 0x3A00007, MASK)


def test_no_active_window_matches_nobody():
    assert not same_client(0, 0, MASK)
    assert not same_client(0x3A00007, 0, MASK)
    assert not same_client(0, 0x3A00007, MASK)


def test_incr_chunks_end_with_the_empty_piece():
    data = bytes(range(256)) * 2000  # 512 000 bytes
    pieces = incr_chunks(data)
    assert pieces[-1] == b"" and b"".join(pieces) == data
    assert all(0 < len(p) <= CHUNK_BYTES for p in pieces[:-1])
    assert CHUNK_BYTES % 4 == 0  # format-32 data is cut on item boundaries
    assert incr_chunks(b"") == [b""]


def test_char_keysym():
    assert char_keysym("a") == 0x61 and char_keysym("é") == 0xE9
    assert char_keysym("—") == 0x01002014 and char_keysym("😀") == 0x0101F600


def test_control_characters_have_no_keysym():
    # Return would send a chat message, Tab would move the focus.
    for ch in ("\n", "\r", "\t", "\x1b", "\x7f", "\x85"):
        assert char_keysym(ch) is None


class _StubDisplay:
    def sync(self) -> None:
        pass


def test_type_text_checks_the_target_before_every_key(monkeypatch):
    import glance.writeback.keys_xlib as kx
    from glance.writeback.ports import KeysAborted

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(kx.xtest, "fake_input", lambda d, kind, code: sent.append((kind, code)))
    keys = object.__new__(kx.XlibKeys)  # no X connection
    keys._d = _StubDisplay()
    keys._down = []
    keys._key = lambda ch: (ord(ch), False)
    keys._keycode = lambda name: 50
    focus = iter([7, 7, 9, 9, 9, 9])  # the window changes before the third key
    keys._focus_window = lambda: next(focus)
    try:
        keys.type_text("abcdef", 0, window=7)
    except KeysAborted:
        pass
    else:
        raise AssertionError("typing went on in another window")
    presses = [code for kind, code in sent if kind == kx.X.KeyPress]
    releases = [code for kind, code in sent if kind == kx.X.KeyRelease]
    assert presses == [ord("a"), ord("b")] and releases == presses  # nothing left down


def test_type_text_sends_nothing_without_a_target(monkeypatch):
    import glance.writeback.keys_xlib as kx
    from glance.writeback.ports import KeysAborted

    sent: list[object] = []
    monkeypatch.setattr(kx.xtest, "fake_input", lambda *a: sent.append(a))
    keys = object.__new__(kx.XlibKeys)
    keys._d = _StubDisplay()
    keys._down = []
    keys._key = lambda ch: (ord(ch), False)
    keys._keycode = lambda name: 50
    keys._focus_window = lambda: 0  # the shell has the keyboard
    try:
        keys.type_text("abc", 0)
    except KeysAborted:
        pass
    assert sent == []


def test_close_releases_any_key_still_down(monkeypatch):
    """A key pressed through XTEST stays down after its client is gone: on the way out
    (glance-capture stopped mid-chord), everything still held comes up."""
    import glance.writeback.keys_xlib as kx

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(kx.xtest, "fake_input", lambda d, kind, code: sent.append((kind, code)))
    keys = object.__new__(kx.XlibKeys)
    display = _StubDisplay()
    display.close = lambda: sent.append(("closed", 0))  # type: ignore[attr-defined]
    keys._d = display
    keys._down = []
    keys._keycode = lambda name: {"Control_L": 37, "v": 55}[name]

    def interrupted(d, kind, code):  # the chord's key press is where it stops
        sent.append((kind, code))
        if kind == kx.X.KeyPress and code == 55:
            raise KeyboardInterrupt

    monkeypatch.setattr(kx.xtest, "fake_input", interrupted)
    try:
        keys.chord("ctrl", "v")
    except KeyboardInterrupt:
        pass
    assert keys._down == []  # the chord's finally released both
    keys._down = [37]  # as if stopped between press and release
    monkeypatch.setattr(kx.xtest, "fake_input", lambda d, kind, code: sent.append((kind, code)))
    sent.clear()
    keys.close()
    assert sent == [(kx.X.KeyRelease, 37), ("closed", 0)]
