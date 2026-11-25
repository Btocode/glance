import pytest

from glance.backends.keystore import MemoryKeyStore, SecretKeyStore


def test_memory_keystore_roundtrip():
    ks = MemoryKeyStore()
    assert ks.get("anthropic") is None
    ks.set("anthropic", "k1")
    assert ks.get("anthropic") == "k1"
    ks.set("anthropic", "k2")
    assert ks.get("anthropic") == "k2"
    assert ks.get("openai") is None
    ks.delete("anthropic")
    assert ks.get("anthropic") is None
    ks.delete("anthropic")  # idempotent


@pytest.mark.desktop
def test_secret_keystore_roundtrip():
    ks = SecretKeyStore()
    try:
        assert ks.get("glance-test") is None
        ks.set("glance-test", "not-a-real-key")
        assert ks.get("glance-test") == "not-a-real-key"
    finally:
        ks.delete("glance-test")
    assert ks.get("glance-test") is None
