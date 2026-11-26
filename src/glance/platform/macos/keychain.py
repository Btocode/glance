"""KeychainKeyStore: the KeyStore for API keys in the macOS login Keychain.

Generic passwords, service `io.github.btocode.glance.api-key`, account = provider, through
the Security framework (PyObjC). The key never goes on argv, to a file or to a log.
"""
from __future__ import annotations

from typing import Any

SERVICE = "io.github.btocode.glance.api-key"
_NOT_FOUND = -25300  # errSecItemNotFound


class KeychainKeyStore:
    def __init__(self, service: str = SERVICE) -> None:
        import Security

        self._sec = Security
        self._service = service

    def _query(self, provider: str) -> dict[Any, Any]:
        S = self._sec
        return {S.kSecClass: S.kSecClassGenericPassword, S.kSecAttrService: self._service,
                S.kSecAttrAccount: provider}

    def get(self, provider: str) -> str | None:
        S = self._sec
        q = {**self._query(provider), S.kSecReturnData: True, S.kSecMatchLimit: S.kSecMatchLimitOne}
        err, data = S.SecItemCopyMatching(q, None)
        if err == _NOT_FOUND or data is None:
            return None
        if err != 0:
            raise RuntimeError(f"keychain read failed ({err})")
        return bytes(data).decode("utf-8")

    def set(self, provider: str, key: str) -> None:
        from Foundation import NSData

        S = self._sec
        raw = key.encode("utf-8")
        data = NSData.dataWithBytes_length_(raw, len(raw))
        err = S.SecItemUpdate(self._query(provider), {S.kSecValueData: data})
        if err == _NOT_FOUND:
            item = {**self._query(provider), S.kSecValueData: data,
                    S.kSecAttrLabel: f"Glance API key ({provider})"}
            err, _ = S.SecItemAdd(item, None)
        if err != 0:
            raise RuntimeError(f"keychain write failed ({err})")

    def delete(self, provider: str) -> None:
        err = self._sec.SecItemDelete(self._query(provider))
        if err not in (0, _NOT_FOUND):
            raise RuntimeError(f"keychain delete failed ({err})")
