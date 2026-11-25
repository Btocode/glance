import time

import pytest

from glance.backends.base import Analysis, Ctx
from glance.backends.claude import ClaudeBackend
from glance.backends.proc import ProcRunner
from glance.backends.prompts import TEST_SENTENCE

CTX = Ctx("dm", "casual", "relaxed", "Slack")

pytestmark = pytest.mark.paid


def test_live_analyze():
    a = ClaudeBackend(ProcRunner()).analyze(TEST_SENTENCE, CTX)
    assert isinstance(a, Analysis)


def test_live_rewrite_first_delta_under_3_5s():
    t0 = time.monotonic()
    it = ClaudeBackend(ProcRunner()).rewrite(TEST_SENTENCE, "friendlier", CTX)
    first = next(it)
    assert first and time.monotonic() - t0 < 3.5
    list(it)
