"""The shot card's model: what every renderer draws after a region was picked.

One card beside the region, the same window from start to finish (nothing closes and reopens
somewhere else while the user steps through it):

    [logo] Glance Shot · 760 × 300          [Copy] [Save]   Esc Close
    [ Text ][ Explain ][ Translate ][ Ask… ]       <- the tabs: always this row, this place
    ┌──────────────────────────────────────┐
    │ the hint, the notice, or the result  │       <- the body: the only part that changes
    └──────────────────────────────────────┘
    [Copy] [Fix grammar]            note          <- the result's actions

Copy and Save act on the image and say so in the banner; a tab fills the body (the tab stays
selected while its result is on show). The one-time notice before an image goes to an AI is
drawn in the body too.
"""
from __future__ import annotations

from dataclasses import dataclass

TITLE = "Glance Shot"
HINT = "Pick what Glance should do with this shot."
HINT_SUB = ("Text and Copy stay on this computer. Explain and Ask send the image to your AI, "
            "only when you click them.")
NEEDS_VISION = "Needs a vision-capable AI — Text works locally"
NEEDS_AI = "Needs an AI — Text works locally"
IMAGES_OFF = "Sending images to an AI is off ([shot] ai_images = \"never\")"
OCR_OFF = "Reading text is off ([shot] ocr = \"off\")"
PRIVATE = "A private window is in this shot: it is not sent to an AI"
ASK_PROMPT = "Ask about this image"
ASK_PLACEHOLDER = "e.g. what does this error mean?"

# (key, label): the tabs, in order. The keys are the controller's actions.
ACTIONS: tuple[tuple[str, str], ...] = (
    ("extract", "Text"),
    ("explain", "Explain"),
    ("translate", "Translate"),
    ("ask", "Ask…"),
)
# (key, label): the image tools in the header.
TOOLS: tuple[tuple[str, str], ...] = (
    ("copy", "Copy image"),
    ("save", "Save"),
)
AI_ACTIONS = frozenset({"ask"})                     # the image goes to the AI
TEXT_AI_ACTIONS = frozenset({"translate"})          # local OCR, then the text goes
# Explain: the image with a vision AI, else the text read locally with any AI.
RESULT_TAB = {"Text": "extract", "Explanation": "explain", "Translation": "translate",
              "Answer": "ask"}


@dataclass(frozen=True)
class ShotButton:
    key: str
    label: str
    primary: bool = False
    muted: bool = False  # drawn quieter; a click says why (`tip`) instead of acting
    tip: str | None = None
    selected: bool = False  # a tab whose result is in the body (or is being worked on)


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
    buttons: tuple[ShotButton, ...]  # the tabs
    banner: str | None = None
    warn: bool = False  # the banner is a warning (amber), not a confirmation
    notice: ShotNotice | None = None
    result: ShotResult | None = None
    tools: tuple[ShotButton, ...] = ()  # Copy image, Save: in the header
    hint: str | None = HINT  # the body before any tab was picked


def notice_text(backend_label: str) -> str:
    return (f"This sends the image to {backend_label}. Anything visible in it is sent.")


def buttons(*, vision: bool, ai: bool, images: str, ocr: bool, private: bool,
            busy: str | None = None, shown: str | None = None) -> tuple[ShotButton, ...]:
    """The tabs. `vision`: a connected AI takes images; `ai`: any AI is connected;
    `images`: `[shot] ai_images`; `ocr`: reading text is not off; `private`: a private window
    is in the region; `busy`: the tab being worked on; `shown`: the tab whose result is in the
    body. The selected tab is `busy`, else `shown`."""
    out = []
    image_ok = vision and images != "never"
    for key, label in ACTIONS:
        tip = None
        if key in AI_ACTIONS:
            tip = (PRIVATE if private else IMAGES_OFF if images == "never"
                   else NEEDS_VISION if not vision else None)
        elif key == "explain":
            tip = (PRIVATE if private else None if image_ok or (ai and ocr)
                   else IMAGES_OFF if images == "never" and vision
                   else NEEDS_AI if not ai else OCR_OFF)
        elif key in TEXT_AI_ACTIONS:
            tip = PRIVATE if private else OCR_OFF if not ocr else NEEDS_AI if not ai else None
        elif key == "extract" and not ocr:
            tip = OCR_OFF
        out.append(ShotButton(key, label, selected=key == (busy or shown),
                              muted=tip is not None, tip=tip))
    return tuple(out)


def tools() -> tuple[ShotButton, ...]:
    return tuple(ShotButton(k, label) for k, label in TOOLS)


TEXT_ACTIONS: tuple[tuple[str, str], ...] = (
    ("text_copy", "Copy text"),
    ("text_fix", "Fix grammar"),
)


def text_actions(*, ai: bool = False, private: bool = False) -> tuple[ShotButton, ...]:
    """Under the text read from the shot: Copy text (primary) and Fix grammar (local).
    Translate and Explain are the tabs above."""
    return tuple(ShotButton(k, label, primary=k == "text_copy") for k, label in TEXT_ACTIONS)


ANSWER_ACTIONS = (ShotButton("text_copy", "Copy", primary=True),)
