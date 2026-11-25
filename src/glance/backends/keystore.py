"""API-key storage. The key lives only in libsecret (Secret service), never in config or logs."""
from __future__ import annotations

from typing import Protocol

SCHEMA_NAME = "dev.glance.ApiKey"


class KeyStore(Protocol):
    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, key: str) -> None: ...

    def delete(self, provider: str) -> None: ...


class MemoryKeyStore:
    """In-memory store for tests."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def get(self, provider: str) -> str | None:
        return self._keys.get(provider)

    def set(self, provider: str, key: str) -> None:
        self._keys[provider] = key

    def delete(self, provider: str) -> None:
        self._keys.pop(provider, None)


class NoKeyStore:
    """Where no keyring is reachable: nothing is stored, and saving a key says so."""

    def get(self, provider: str) -> str | None:
        return None

    def set(self, provider: str, key: str) -> None:
        raise RuntimeError("no system keyring")

    def delete(self, provider: str) -> None:
        pass


class SecretKeyStore:
    """libsecret-backed store (gi.repository.Secret), one entry per provider."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        self._secret = Secret
        self._schema = Secret.Schema.new(
            SCHEMA_NAME,
            Secret.SchemaFlags.NONE,
            {"provider": Secret.SchemaAttributeType.STRING},
        )

    def get(self, provider: str) -> str | None:
        return self._secret.password_lookup_sync(self._schema, {"provider": provider}, None)

    def set(self, provider: str, key: str) -> None:
        self._secret.password_store_sync(
            self._schema,
            {"provider": provider},
            self._secret.COLLECTION_DEFAULT,
            f"Glance API key ({provider})",
            key,
            None,
        )

    def delete(self, provider: str) -> None:
        self._secret.password_clear_sync(self._schema, {"provider": provider}, None)
