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
    from glance import update

    def refuse(*_a, **_k):
        pytest.fail("a test tried to run the real installer or reach github.com; inject a fake")

    monkeypatch.setattr(update, "installer_argv", refuse)
    monkeypatch.setattr(update, "_git", refuse)
    monkeypatch.setattr(update, "_http_get", refuse)


@pytest.fixture(autouse=True)
def _never_run_the_users_login_shell(monkeypatch):
    """The CLI PATH resolver must not start the developer's real login shell (rc files, 3 s):
    tests see the current PATH plus the usual dirs. Tests of the resolver pass a fake."""
    from glance.backends import cli_path

    monkeypatch.setattr(cli_path, "run_shell", lambda argv, timeout_s: None)
    cli_path.refresh()
    yield
    cli_path.refresh()


@pytest.fixture(params=["rust", "python"])
def privacy_impl(request, monkeypatch):
    """Run a privacy test against both implementations: glance_core's Rust twin and the
    Python reference (what GLANCE_PURE_PYTHON=1 selects)."""
    from glance.privacy import _native

    if request.param == "python":
        monkeypatch.setattr(_native, "core", None)
    elif _native.core is None:
        pytest.skip("glance_core (>= 0.2) is not built")
    return request.param
