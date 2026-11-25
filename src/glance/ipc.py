"""Newline-delimited JSON over unix sockets. Callbacks run on reader threads."""
from __future__ import annotations

import itertools
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from glance.protocol import MAX_LINE, ProtocolError, decode, encode

log = logging.getLogger(__name__)


class Conn:
    """One connected peer; thread-safe send."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._lock = threading.Lock()
        self._closed = threading.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        data = encode(msg_type, payload)
        with self._lock:
            try:
                self._sock.sendall(data)
            except OSError as e:
                self.close()
                raise ConnectionError("peer gone") from e

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def read_loop(self, on_line: Callable[[str, dict[str, Any]], None]) -> None:
        """Read until EOF, error or a bad line; always closes the connection."""
        try:
            with self._sock.makefile("rb") as f:
                while True:
                    line = f.readline(MAX_LINE + 1)
                    if not line:
                        break
                    try:
                        msg_type, payload = decode(line.rstrip(b"\n"))
                    except ProtocolError:
                        break
                    try:
                        on_line(msg_type, payload)
                    except Exception:
                        # A callback bug must not leak the connection; drop it.
                        log.exception("ipc callback failed for message type %r", msg_type)
                        break
        except (OSError, ValueError):
            pass
        finally:
            self.close()


class JsonLineServer:
    def __init__(
        self,
        path: Path,
        on_message: Callable[[Conn, str, dict], None],
        on_disconnect: Callable[[Conn], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._listener: socket.socket | None = None
        self._conns: set[Conn] = set()
        self._lock = threading.Lock()
        self._stopping = False

    def start(self) -> None:
        d = self.path.parent
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if d.stat().st_uid == os.getuid():
            os.chmod(d, 0o700)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        os.chmod(self.path, 0o600)
        s.listen(8)
        self._listener = s
        with self._lock:
            self._stopping = False
        threading.Thread(target=self._accept_loop, args=(s,), daemon=True).start()

    def _accept_loop(self, listener: socket.socket) -> None:
        while True:
            try:
                sock, _ = listener.accept()
            except OSError:
                return
            conn = Conn(sock)
            with self._lock:
                if self._stopping:
                    conn.close()
                    return
                self._conns.add(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: Conn) -> None:
        try:
            conn.read_loop(lambda t, p: self._on_message(conn, t, p))
        finally:
            conn.close()
            with self._lock:
                self._conns.discard(conn)
            if self._on_disconnect is not None and not self._stopping:
                try:
                    self._on_disconnect(conn)
                except Exception:
                    log.exception("ipc on_disconnect failed")

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
        if self._listener is not None:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            c.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class _Waiter:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: dict | None = None


class JsonLineClient:
    def __init__(
        self,
        path: Path,
        on_message: Callable[[str, dict], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._conn: Conn | None = None
        self._ids = itertools.count(1)
        self._waiters: dict[int, _Waiter] = {}
        self._wlock = threading.Lock()

    def connect(self, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(self.path))
                break
            except OSError as e:
                sock.close()
                if time.monotonic() + 0.2 > deadline:
                    raise ConnectionError(f"cannot connect to {self.path}") from e
                time.sleep(0.2)
        conn = Conn(sock)
        self._conn = conn
        threading.Thread(target=self._run, args=(conn,), daemon=True).start()

    def _run(self, conn: Conn) -> None:
        try:
            conn.read_loop(self._dispatch)
        finally:
            conn.close()
            with self._wlock:
                waiters = list(self._waiters.values())
            for w in waiters:
                w.event.set()  # reply stays None -> request raises ConnectionError
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect()
                except Exception:
                    log.exception("ipc on_disconnect failed")

    def _dispatch(self, msg_type: str, payload: dict) -> None:
        rid = payload.get("id")
        if isinstance(rid, int) and not isinstance(rid, bool):
            with self._wlock:
                w = self._waiters.get(rid)
            if w is not None:
                w.reply = payload
                w.event.set()
                return
        if self._on_message is not None:
            self._on_message(msg_type, payload)

    def send(self, msg_type: str, payload: dict) -> None:
        if self._conn is None:
            raise ConnectionError("not connected")
        self._conn.send(msg_type, payload)

    def request(self, msg_type: str, payload: dict,
                timeout_s: float | None = 3.0) -> dict:
        rid = next(self._ids)
        waiter = _Waiter()
        with self._wlock:
            self._waiters[rid] = waiter
        try:
            if self._conn is None or self._conn.closed:
                raise ConnectionError("not connected")
            self._conn.send(msg_type, {**payload, "id": rid})
            if not waiter.event.wait(timeout_s):
                raise TimeoutError(f"no reply to {msg_type}")
            if waiter.reply is None:
                raise ConnectionError("peer closed while waiting for reply")
            return waiter.reply
        finally:
            with self._wlock:
                self._waiters.pop(rid, None)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
