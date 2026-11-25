"""Wire protocol between glance-capture and glanced (newline-delimited JSON)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

SNAPSHOT = "snapshot"
FOCUS_LOST = "focus_lost"
APPLY = "apply"
REVERT = "revert"
PASTE = "paste"
RESET = "reset"  # glanced -> capture: the machine woke up, re-register and report again
APPLY_RESULT = "apply_result"
HELLO = "hello"
PING = "ping"

MAX_LINE = 4 * 1024 * 1024


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Rect:
        return cls(int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]))


def _rect(d: dict[str, Any] | None) -> Rect | None:
    return None if d is None else Rect.from_dict(d)


@dataclass(frozen=True)
class FieldSnapshot:
    app: str
    wm_class: str
    title: str
    url: str | None
    role: str
    text: str
    caret: int
    caret_rect: Rect | None
    prev_char_rect: Rect | None
    field_rect: Rect | None
    window_rect: Rect | None
    field_id: str
    capture_confidence: float
    seq: int
    pid: int
    gated: str | None  # when set, text == "" and caret == 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("caret_rect", "prev_char_rect", "field_rect", "window_rect"):
            v = getattr(self, k)
            d[k] = None if v is None else v.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldSnapshot:
        return cls(
            app=d["app"], wm_class=d["wm_class"], title=d["title"], url=d.get("url"),
            role=d["role"], text=d["text"], caret=int(d["caret"]),
            caret_rect=_rect(d.get("caret_rect")),
            prev_char_rect=_rect(d.get("prev_char_rect")),
            field_rect=_rect(d.get("field_rect")),
            window_rect=_rect(d.get("window_rect")),
            field_id=d["field_id"], capture_confidence=float(d["capture_confidence"]),
            seq=int(d["seq"]), pid=int(d["pid"]), gated=d.get("gated"),
        )


@dataclass(frozen=True)
class Edit:
    """Code-point offsets into the snapshot text."""

    start: int
    end: int
    replacement: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Edit:
        return cls(int(d["start"]), int(d["end"]), d["replacement"])


@dataclass(frozen=True)
class ApplyRequest:
    apply_id: str
    field_id: str
    expected_sha256: str
    edit: Edit
    method: str  # auto | type | paste

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["edit"] = self.edit.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApplyRequest:
        return cls(d["apply_id"], d["field_id"], d["expected_sha256"],
                   Edit.from_dict(d["edit"]), d["method"])


@dataclass(frozen=True)
class ApplyResult:
    apply_id: str
    status: str  # verified | reverted | stale | failed | refused | unverified
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApplyResult:
        return cls(d["apply_id"], d["status"], d.get("detail", ""))


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode(msg_type: str, payload: dict[str, Any]) -> bytes:
    line = json.dumps({"t": msg_type, "p": payload}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return line + b"\n"


def decode(line: bytes) -> tuple[str, dict[str, Any]]:
    if len(line) > MAX_LINE:
        raise ProtocolError("line too long")
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError("bad json") from e
    if not isinstance(obj, dict) or "t" not in obj or "p" not in obj:
        raise ProtocolError("missing keys")
    if not isinstance(obj["t"], str) or not isinstance(obj["p"], dict):
        raise ProtocolError("bad types")
    return obj["t"], obj["p"]
