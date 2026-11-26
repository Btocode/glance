"""Shared pytest configuration."""
import os

import pytest

os.environ.setdefault("GLANCE_STRICT_LOG", "1")

PAID_ENV = "GLANCE_PAID"


def pytest_collection_modifyitems(config, items):
    """A `paid` test makes real, billed calls: it runs only when GLANCE_PAID=1 is set, whatever
    `-m` expression selected it (`-m xvfb` once picked up paid tests that were also xvfb)."""
    if os.environ.get(PAID_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"paid: makes real API calls; set {PAID_ENV}=1 to run")
    for item in items:
        if item.get_closest_marker("paid") is not None:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _never_run_the_real_installer_or_network(monkeypatch):
    """No test may run the published installer or ask github.com for versions.

    A test that wants either must inject its own fake (popen= / run=)."""
    import glance.update as update

    def refuse(*_a, **_k):
        pytest.fail("a test tried to run the real installer or reach github.com; inject a fake")

    monkeypatch.setattr(update, "installer_argv", refuse)
    monkeypatch.setattr(update, "_git", refuse)
