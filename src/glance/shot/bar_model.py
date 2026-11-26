"""The shot bar's model: what every renderer draws after a region was picked.

A row of actions (Copy · Save · Extract text · Explain · Ask… · Translate · Close), a line for
what just happened, the one-time notice before an image goes to an AI, and, once there is
one, a result card in the Glance Assistant style (the extracted text, the explanation, the
translation) with its own actions.
"""
from __future__ import annotations

from dataclasses import dataclass

TITLE = "Glance Shot"
NEEDS_VISION = "Needs a vision-capable AI — Extract text works locally"
NEEDS_AI = "Needs an AI — Extract text works locally"
IMAGES_OFF = "Sending images to an AI is off ([shot] ai_images = \"never\")"
OCR_OFF = "Extract text is off ([shot] ocr = \"off\")"
PRIVATE = "A private window is in this shot: it is not sent to an AI"
ASK_PROMPT = "Ask about this image"
ASK_PLACEHOLDER = "e.g. what does this error mean?"

# (key, label): the order of the row.
ACTIONS: tuple[tuple[str, str], ...] = (
    ("copy", "Copy"),
    ("save", "Save"),
    ("extract", "Extract text"),
    ("explain", "Explain"),
    ("ask", "Ask…"),
    ("translate", "Translate"),
    ("close", "Close"),
)
AI_ACTIONS = frozenset({"explain", "ask"})          # the image goes to the AI
TEXT_AI_ACTIONS = frozenset({"translate"})          # local OCR, then the text goes


@dataclass(frozen=True)
class ShotButton:
    key: str
    label: str
    primary: bool = False
    muted: bool = False  # drawn quieter; a click says why (`tip`) instead of acting
    tip: str | None = None


@dataclass(frozen=True)
class ShotNotice:
    """Asked once per backend before an image goes to it ("Send" this time, "Always allow")."""

    text: str
    buttons: tuple[ShotButton, ...] = (
        ShotButton("notice_send", "Send", primary=True),
        ShotButton("notice_always", "Always allow"),
        ShotButton("notice_cancel", "Cancel"),
    )


@dataclass(frozen=True)
class ShotResult:
    """The result card: "Text" (OCR), "Explanation", "Answer", "Translation"."""

    title: str
    text: str | None
    state: str  # loading | ready | failed
    actions: tuple[ShotButton, ...] = ()
    note: str | None = None  # muted, under the text ("Read locally", "by claude")


@dataclass(frozen=True)
class ShotBarView:
    size: str  # "640 × 480"
    buttons: tuple[ShotButton, ...]
    banner: str | None = None
    warn: bool = False  # the banner is a warning (amber), not a confirmation
    notice: ShotNotice | None = None
    result: ShotResult | None = None


def notice_text(backend_label: str) -> str:
    return (f"This sends the image to {backend_label}. Anything visible in it is sent.")


def buttons(*, vision: bool, ai: bool, images: str, ocr: bool, private: bool,
            busy: str | None = None) -> tuple[ShotButton, ...]:
    """The action row. `vision`: a connected AI takes images; `ai`: any AI is connected;
    `images`: `[shot] ai_images`; `ocr`: Extract text is not off; `private`: a private
    window is in the region; `busy`: the action running now (drawn as the primary one)."""
    out = []
    for key, label in ACTIONS:
        tip = None
        if key in AI_ACTIONS:
            tip = (PRIVATE if private else IMAGES_OFF if images == "never"
                   else NEEDS_VISION if not vision else None)
        elif key in TEXT_AI_ACTIONS:
            tip = PRIVATE if private else OCR_OFF if not ocr else NEEDS_AI if not ai else None
        elif key == "extract" and not ocr:
            tip = OCR_OFF
        out.append(ShotButton(key, label, primary=key == (busy or "copy"),
                              muted=tip is not None, tip=tip))
    return tuple(out)


TEXT_ACTIONS: tuple[tuple[str, str], ...] = (
    ("text_copy", "Copy"),
    ("text_fix", "Fix grammar"),
    ("text_translate", "Translate"),
    ("text_explain", "Explain"),
)


def text_actions(*, ai: bool, private: bool) -> tuple[ShotButton, ...]:
    """Under extracted text: Copy (primary), Fix grammar (local), Translate and Explain (the
    text goes to the AI; not for a private region)."""
    out = []
    for key, label in TEXT_ACTIONS:
        tip = None
        if key in ("text_translate", "text_explain"):
            tip = PRIVATE if private else NEEDS_AI if not ai else None
        out.append(ShotButton(key, label, primary=key == "text_copy", muted=tip is not None,
                              tip=tip))
    return tuple(out)


ANSWER_ACTIONS = (ShotButton("text_copy", "Copy", primary=True),)
