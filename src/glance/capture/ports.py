"""Accessibility ports: what the tracker and write-back need from the focused field.

Offsets everywhere in these protocols are Python `str` indices into `FieldRead.text`.
`FieldRead.text` is the field's text with filler characters removed (see `clean_text`);
the adapter converts to and from the toolkit's own offsets with `to_clean` / `to_raw`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from glance.privacy.gate import FieldMeta
from glance.protocol import Rect

# U+FEFF: Gecko pads every non-BMP character with one so that a Python index into the raw
# text equals the UTF-16 AT-SPI offset (findings S3.1); Slate editors start an empty line
# with one (S2.1). It never belongs to what the user wrote.
FILLER = "﻿"


class A11yTimeout(Exception):
    """The application did not answer in time, or the field is gone or unusable."""


@dataclass(frozen=True)
class FieldRead:
    text: str
    caret: int
    selection: tuple[int, int] | None
    caret_rect: Rect | None
    prev_char_rect: Rect | None
    field_rect: Rect | None
    window_rect: Rect | None


class A11yField(Protocol):
    """One focused text field. Every method may raise `A11yTimeout`."""

    key: str

    def meta(self) -> FieldMeta: ...

    def window_rect(self) -> Rect | None:
        """Geometry of the field's window; must not read the field's text."""
        ...

    def read(self) -> FieldRead: ...

    def set_selection(self, start: int, end: int) -> bool: ...

    def set_caret(self, offset: int) -> bool: ...


class A11yPort(Protocol):
    def start(
        self,
        on_focus: Callable[[A11yField | None], None],
        on_change: Callable[[A11yField], None],
    ) -> None: ...

    def focused(self) -> A11yField | None: ...


def clean_text(raw: str) -> str:
    """The raw accessible text without filler characters."""
    return raw.replace(FILLER, "")


def to_clean(raw: str, offset: int) -> int:
    """Map an offset into the raw text to the same position in `clean_text(raw)`."""
    offset = max(0, min(offset, len(raw)))
    return offset - raw.count(FILLER, 0, offset)


def to_raw(raw: str, offset: int) -> int:
    """Map an offset into `clean_text(raw)` back to the raw text.

    The result sits immediately before the character at `offset`, after any fillers that
    precede it. So a range end lands after the filler that pads a non-BMP character, and a
    replacement of that character takes its filler along.
    """
    target = max(0, offset)
    kept = 0
    for i, ch in enumerate(raw):
        if ch == FILLER:
            continue
        if kept == target:
            return i
        kept += 1
    return len(raw)


def is_password_input(attributes: Mapping[str, str] | None) -> bool:
    """True when the object's attributes mark it as a password input (findings S2.6).

    Browsers set `text-input-type: password`; this is a second check next to the role.
    """
    if not attributes:
        return False
    return str(attributes.get("text-input-type", "")).strip().casefold() == "password"
