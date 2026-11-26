"""macOS acceptance tests: they run only on a Mac, in the console session, with
GLANCE_MAC_TEST=1 (they start glanced and a test app, click the status icon and press ⌥⏎
in that app). Everywhere else every test here is skipped, whatever `-m` selects."""
import os
import sys

import pytest

MAC_ENV = "GLANCE_MAC_TEST"


def pytest_collection_modifyitems(config, items):
    if sys.platform == "darwin" and os.environ.get(MAC_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"macos: needs a Mac console session and {MAC_ENV}=1")
    for item in items:
        if item.get_closest_marker("macos") is not None:
            item.add_marker(skip)
