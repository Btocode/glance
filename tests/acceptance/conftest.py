"""Fixtures for the real-browser acceptance tests (marker `apps`): one private Xvfb + D-Bus +
AT-SPI bus for the whole run."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.acceptance.harness import FIXTURES, FixtureServer, PrivateSession

CACHE = Path.home() / ".cache" / "glance-test"  # the Firefox snap cannot see /tmp


@pytest.fixture(scope="session")
def session():
    if shutil.which("Xvfb") is None or shutil.which("dbus-daemon") is None:
        pytest.skip("Xvfb / dbus-daemon are not installed")
    s = PrivateSession().start()
    # Nothing in the test process may fall back to the real display or buses.
    names = ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS", "XAUTHORITY",
             "WAYLAND_DISPLAY")
    saved = {n: os.environ.get(n) for n in names}
    os.environ.update(DISPLAY=s.display, XAUTHORITY=s.env["XAUTHORITY"],
                      DBUS_SESSION_BUS_ADDRESS=s.env["DBUS_SESSION_BUS_ADDRESS"],
                      AT_SPI_BUS_ADDRESS=s.a11y_address)
    os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        yield s
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        s.stop()


@pytest.fixture
def fixture_server():
    server = FixtureServer(FIXTURES)
    base = server.start()
    yield server, base
    server.stop()


@pytest.fixture(scope="session")
def profile_root():
    CACHE.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=CACHE))  # unique per run
    yield run
    shutil.rmtree(run, ignore_errors=True)


@pytest.fixture(scope="module")
def fixture_server_module():
    server = FixtureServer(FIXTURES)
    base = server.start()
    yield server, base
    server.stop()
