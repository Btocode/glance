"""A `paid` test never runs without GLANCE_PAID=1, whatever `-m` selected it."""
import pytest

from tests import conftest


class _Item:
    def __init__(self, *markers: str) -> None:
        self._markers = set(markers)
        self.added: list = []

    def get_closest_marker(self, name: str):
        return name if name in self._markers else None

    def add_marker(self, mark) -> None:
        self.added.append(mark)


def _skipped(item: _Item) -> bool:
    return any(getattr(m, "name", None) == "skip" for m in item.added)


def test_paid_tests_are_skipped_without_the_env(monkeypatch):
    monkeypatch.delenv("GLANCE_PAID", raising=False)
    paid, paid_xvfb, plain = _Item("paid"), _Item("paid", "xvfb"), _Item("xvfb")
    conftest.pytest_collection_modifyitems(None, [paid, paid_xvfb, plain])
    assert _skipped(paid) and _skipped(paid_xvfb) and not _skipped(plain)


@pytest.mark.parametrize("value", ["0", "", "yes", "true"])
def test_only_exactly_one_opens_the_gate(monkeypatch, value):
    monkeypatch.setenv("GLANCE_PAID", value)
    item = _Item("paid")
    conftest.pytest_collection_modifyitems(None, [item])
    assert _skipped(item)


def test_glance_paid_1_lets_them_run(monkeypatch):
    monkeypatch.setenv("GLANCE_PAID", "1")
    item = _Item("paid")
    conftest.pytest_collection_modifyitems(None, [item])
    assert item.added == []
