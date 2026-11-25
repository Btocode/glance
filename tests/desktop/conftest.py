"""Desktop tests share one process, and with it one Wnck screen and one AT-SPI cache.

Neither follows the desktop unless the GLib main context is dispatched, and no test runs a
main loop. Without this, the window list still holds the previous test's closed window when
the next test (in this file or the next one) looks for its own.
"""
import os
import time

import pytest


def pump(seconds: float) -> None:
    """Dispatch the default main context for `seconds`."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while True:
        while context.pending():
            context.iteration(False)
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _desktop_state_follows_the_desktop():
    if not os.environ.get("DISPLAY"):
        yield
        return
    pump(0.1)
    yield
    pump(0.4)  # after the test's own teardown: its windows are closed by now
