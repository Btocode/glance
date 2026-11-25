import pytest

from glance.backends.base import Analysis, Ctx
from glance.backends.codex import CodexBackend
from glance.backends.gemini import GeminiBackend
from glance.backends.proc import ProcRunner
from glance.backends.prompts import TEST_SENTENCE

CTX = Ctx("dm", "casual", "relaxed", "Slack")

pytestmark = pytest.mark.paid


def test_live_codex_analyze():
    # codex analyze is over the 8 s budget (S6.3); this only checks it parses.
    assert isinstance(CodexBackend(ProcRunner()).analyze(TEST_SENTENCE, CTX), Analysis)


def test_live_gemini_analyze():
    # Fails with BackendError("auth") on an account gemini no longer supports (S6.4).
    assert isinstance(GeminiBackend(ProcRunner()).analyze(TEST_SENTENCE, CTX), Analysis)
