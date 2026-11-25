"""Word-level diff between a text and its rewrite, for the preview pane. Pure."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from xml.sax.saxutils import escape

# A token is a word, a whitespace run or a punctuation run: a diff never splits a word.
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]+")
_SPAN = {
    "del": '<span strikethrough="true" foreground="#b3261e">',
    "ins": '<span background="#d3f2d9" foreground="#0b3d17">',
}


@dataclass(frozen=True)
class DiffOp:
    kind: str  # eq | del | ins
    text: str


def word_diff(old: str, new: str) -> tuple[DiffOp, ...]:
    """What turns `old` into `new`: the `eq` and `del` texts join to `old`, `eq` and `ins`
    to `new`. A replacement is a `del` followed by an `ins`; adjacent ops of one kind merge."""
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    parts: list[tuple[str, str]] = []
    # Whitespace is junk to the matcher: it anchors on words, so an added word comes out as
    # "please " after "ok ", not as " please" wedged between two halves of the old spacing.
    matcher = SequenceMatcher(str.isspace, a, b, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(("eq", "".join(a[a0:a1])))
            continue
        if a1 > a0:
            parts.append(("del", "".join(a[a0:a1])))
        if b1 > b0:
            parts.append(("ins", "".join(b[b0:b1])))
    ops: list[DiffOp] = []
    for kind, text in parts:
        if ops and ops[-1].kind == kind:
            ops[-1] = DiffOp(kind, ops[-1].text + text)
        else:
            ops.append(DiffOp(kind, text))
    return tuple(ops)


def diff_markup(ops: tuple[DiffOp, ...]) -> str:
    """Pango markup for a diff. The text is the user's: every piece of it is escaped."""
    out = []
    for op in ops:
        text = escape(op.text)
        out.append(text if op.kind == "eq" else f"{_SPAN[op.kind]}{text}</span>")
    return "".join(out)
