"""Before an image goes to an AI: `[shot] ai_images` and the one-time notice per backend.

Images cannot be redacted, so the first time an image would go to a backend the user is told
"This sends the image to <backend>. Anything visible in it is sent." and picks Send (this
once), Always allow (remembered for that backend) or Cancel. What was allowed is a small
JSON file in Glance's state dir: backend keys only, never anything about an image.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from glance.log import log_event

ALLOW, ASK, REFUSE = "allow", "ask", "refuse"


class ImageConsent:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> set[str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        allowed = data.get("allowed") if isinstance(data, dict) else None
        return {a for a in allowed if isinstance(a, str)} if isinstance(allowed, list) else set()

    def allowed(self, backend_key: str) -> bool:
        return backend_key in self._load()

    def decide(self, mode: str, backend_key: str) -> str:
        """`allow` (send now), `ask` (show the notice first) or `refuse` (`never`)."""
        if mode == "never":
            return REFUSE
        if mode == "allow" or self.allowed(backend_key):
            return ALLOW
        return ASK

    def always_allow(self, backend_key: str) -> None:
        keys = self._load() | {backend_key}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".shot-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"allowed": sorted(keys)}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as e:
            log_event("shot_consent_write_failed", kind=type(e).__name__)

    def forget(self) -> None:
        try:
            self._path.unlink()
        except OSError:
            pass
