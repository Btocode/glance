import socket
import threading
import time

import pytest

from glance.ipc import JsonLineClient, JsonLineServer


def wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "t.sock"


def echo_server(path, **kw):
    srv = JsonLineServer(path, lambda c, t, p: c.send(t, p), **kw)
    srv.start()
    return srv


def test_round_trip_both_directions(sock_path):
    srv = echo_server(sock_path)
    got = []
    cli = JsonLineClient(sock_path, on_message=lambda t, p: got.append((t, p)))
    try:
        cli.connect(timeout_s=2)
        cli.send("hello", {"x": "héllo 😀"})
        assert wait_for(lambda: got)
        assert got[0] == ("hello", {"x": "héllo 😀"})
    finally:
        cli.close()
        srv.stop()


def test_socket_mode_is_0600(sock_path):
    srv = echo_server(sock_path)
    try:
        assert sock_path.stat().st_mode & 0o777 == 0o600
    finally:
        srv.stop()


def test_request_matches_reply_by_id(sock_path):
    # Server replies out of order: holds the first request until the second arrives.
    held = []
    lock = threading.Lock()

    def on_msg(conn, t, p):
        with lock:
            held.append((conn, t, p))
            if len(held) == 2:
                for c, tt, pp in reversed(held):
                    c.send(tt, {"id": pp["id"], "who": pp["who"]})

    srv = JsonLineServer(sock_path, on_msg)
    srv.start()
    cli = JsonLineClient(sock_path)
    try:
        cli.connect(timeout_s=2)
        out = {}

        def call(who):
            out[who] = cli.request("q", {"who": who}, timeout_s=3)

        ts = [threading.Thread(target=call, args=(w,)) for w in ("a", "b")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert out["a"]["who"] == "a"
        assert out["b"]["who"] == "b"
    finally:
        cli.close()
        srv.stop()


def test_bad_line_closes_only_that_connection(sock_path):
    srv = echo_server(sock_path)
    got = []
    good = JsonLineClient(sock_path, on_message=lambda t, p: got.append(t))
    try:
        good.connect(timeout_s=2)
        bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bad.connect(str(sock_path))
        bad.sendall(b"this is not json\n")
        bad.settimeout(2)
        assert bad.recv(10) == b""  # server closed it
        bad.close()
        good.send("ping", {})
        assert wait_for(lambda: got == ["ping"])
    finally:
        good.close()
        srv.stop()


def test_server_start_replaces_stale_socket_file(sock_path):
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()
    assert sock_path.exists()
    srv = echo_server(sock_path)
    cli = JsonLineClient(sock_path)
    try:
        cli.connect(timeout_s=2)
    finally:
        cli.close()
        srv.stop()


def test_client_connect_times_out(sock_path):
    cli = JsonLineClient(sock_path)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        cli.connect(timeout_s=0.5)
    assert time.monotonic() - t0 < 1.0


def test_on_disconnect_called_when_peer_dies(sock_path):
    server_side = []
    srv = JsonLineServer(sock_path, lambda c, t, p: None,
                         on_disconnect=lambda c: server_side.append(c))
    srv.start()
    client_side = []
    cli = JsonLineClient(sock_path, on_disconnect=lambda: client_side.append(1))
    try:
        cli.connect(timeout_s=2)
        cli.send("ping", {})
        cli.close()
        assert wait_for(lambda: len(server_side) == 1)
        cli2 = JsonLineClient(sock_path, on_disconnect=lambda: client_side.append(2))
        cli2.connect(timeout_s=2)
        srv.stop()
        assert wait_for(lambda: 2 in client_side)
    finally:
        srv.stop()


def test_request_raises_when_peer_closes(sock_path):
    srv = JsonLineServer(sock_path, lambda c, t, p: c.close())
    srv.start()
    cli = JsonLineClient(sock_path)
    try:
        cli.connect(timeout_s=2)
        t0 = time.monotonic()
        with pytest.raises(ConnectionError):
            cli.request("q", {}, timeout_s=None)
        assert time.monotonic() - t0 < 2
    finally:
        cli.close()
        srv.stop()


def test_server_callback_exception_drops_conn_and_fires_disconnect(sock_path):
    dropped = []

    def on_msg(conn, t, p):
        if t == "boom":
            raise KeyError("bad payload")
        conn.send(t, p)

    srv = JsonLineServer(sock_path, on_msg, on_disconnect=dropped.append)
    srv.start()
    bad = JsonLineClient(sock_path)
    good_got = []
    good = JsonLineClient(sock_path, on_message=lambda t, p: good_got.append(t))
    try:
        bad.connect(timeout_s=2)
        good.connect(timeout_s=2)
        bad.send("boom", {})
        assert wait_for(lambda: len(dropped) == 1)
        assert wait_for(lambda: len(srv._conns) == 1)
        good.send("ping", {})
        assert wait_for(lambda: good_got == ["ping"])
    finally:
        bad.close()
        good.close()
        srv.stop()


def test_client_callback_exception_does_not_hang_request(sock_path):
    srv = JsonLineServer(sock_path, lambda c, t, p: (c.send("other", {}), c.send(t, {"id": p["id"]})))
    srv.start()

    def boom(t, p):
        raise TypeError("x")

    gone = []
    cli = JsonLineClient(sock_path, on_message=boom, on_disconnect=lambda: gone.append(1))
    try:
        cli.connect(timeout_s=2)
        with pytest.raises(ConnectionError):
            cli.request("q", {}, timeout_s=3)
        assert wait_for(lambda: gone == [1])
    finally:
        cli.close()
        srv.stop()


def test_socket_dir_is_0700(tmp_path):
    d = tmp_path / "new"
    srv = echo_server(d / "s.sock")
    srv.stop()
    assert d.stat().st_mode & 0o777 == 0o700
    e = tmp_path / "existing"
    e.mkdir(mode=0o755)
    e.chmod(0o755)
    srv = echo_server(e / "s.sock")
    srv.stop()
    assert e.stat().st_mode & 0o777 == 0o700


def test_oversize_line_closes_connection(sock_path):
    from glance.protocol import MAX_LINE

    srv = echo_server(sock_path)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sock_path))
        s.settimeout(3)
        try:
            s.sendall(b"x" * (MAX_LINE + 10) + b"\n")
        except OSError:
            pass
        try:
            assert s.recv(10) == b""
        except ConnectionResetError:
            pass
        s.close()
    finally:
        srv.stop()
