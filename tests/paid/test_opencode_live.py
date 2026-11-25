import subprocess

import pytest

from glance.backends.base import Analysis, Ctx
from glance.backends.opencode import OpencodeBackend
from glance.backends.proc import ProcRunner
from glance.backends.prompts import TEST_SENTENCE

CTX = Ctx("dm", "casual", "relaxed", "Slack")

pytestmark = pytest.mark.paid


def _sessions() -> str:
    return subprocess.run(["opencode", "session", "list"], capture_output=True, text=True,
                          check=False).stdout


def test_live_analyze_leaves_no_session():
    before = _sessions()
    b = OpencodeBackend(ProcRunner())
    try:
        assert isinstance(b.analyze(TEST_SENTENCE, CTX), Analysis)
    finally:
        b.stop()
    assert _sessions() == before
