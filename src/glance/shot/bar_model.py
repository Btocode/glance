"""The shot card's model: what every renderer draws after a region was picked.

First a slim floating toolbar beside the region (the selection bar's look), nothing else:

    [logo] Text · Explain · Translate · Ask… │ Mark · Copy · Save │ ✕

`expanded` False. A tab that has something to show grows the card under that same toolbar,
which stays where it is (the same window from start to finish: nothing closes and reopens
somewhere else while the user steps through it):

    [logo] Text · Explain · Translate · Ask… │ Mark · Copy · Save │ ✕   <- never moves
    ┌──────────────────────────────────────┐
    │ the notice, the language choices,    │    <- the body: the only part that changes
    │ the question being typed, the result │
    └──────────────────────────────────────┘
    [Copy] [Fix grammar]            status        <- the result's actions

Copy, Save and Mark act on the image and say so in the status line (under the toolbar while
it is not expanded); a tab fills the body (the tab stays selected while its result is on
show). The one-time notice before an image goes to an AI is drawn in the body too.
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
# (key, label): the image tools, after the tabs.
TOOLS: tuple[tuple[str, str], ...] = (
    ("mark", "Mark"),
    ("copy", "Copy"),
    ("save", "Save"),
)
# Line glyphs (glance.ui.look.GLYPHS) for the toolbar's items, as the selection bar has them.
GLYPHS = {"extract": "lines", "explain": "spark", "translate": "globe", "ask": "ask",
          "mark": "pen", "copy": "copy", "save": "save", "close": "close"}
TRANSLATE_TO = "Translate to"
OTHER_LANGUAGE = "Other…"
OTHER_PROMPT = "Translate to which language?"
OTHER_PLACEHOLDER = "e.g. Italian, Korean, Tagalog"
MARK_UNAVAILABLE = "Marking up a shot is not available on this desktop yet"
EXACT_NOTE = "Read by {backend}, exactly as shown"
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
    # Chips in the body (state "choose"): the languages Translate offers ("lang:<name>").
    choices: tuple[ShotButton, ...] = ()


@dataclass(frozen=True)
class ShotBarView:
    size: str  # "640 × 480"
    buttons: tuple[ShotButton, ...]  # the tabs
    banner: str | None = None
    warn: bool = False  # the banner is a warning (amber), not a confirmation
    notice: ShotNotice | None = None
    result: ShotResult | None = None
    tools: tuple[ShotButton, ...] = ()  # Mark, Copy, Save: after the tabs
    hint: str | None = HINT  # (unused since the toolbar: kept for older renderers)
    expanded: bool = False  # the card's body is open (a result, the notice, a choice, Ask)
    asking: bool = False  # the body is the question's entry (Enter asks, Esc goes back)
    ask_prompt: str = ASK_PROMPT  # the entry's heading: Ask's, or "Translate to which language?"
    ask_placeholder: str = ASK_PLACEHOLDER
    ask_go: str = "ask"  # beside ↵


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
    ("text_exact", "Exact text (AI)"),
    ("text_fix", "Fix grammar"),
)


def text_actions(*, ai: bool = False, private: bool = False,
                 vision: bool = False) -> tuple[ShotButton, ...]:
    """Under the text read from the shot: Copy text (primary), Exact text (AI: the image is
    read by a vision AI, word for word, on this click) and Fix grammar (local). Translate
    and Explain are the tabs above."""
    out = []
    for k, label in TEXT_ACTIONS:
        tip = None
        if k == "text_exact":
            tip = PRIVATE if private else NEEDS_VISION if not vision else None
        out.append(ShotButton(k, label, primary=k == "text_copy", muted=tip is not None,
                              tip=tip))
    return tuple(out)


def languages(current: str) -> tuple[ShotButton, ...]:
    """Translate's choices: the popular languages (the last one used selected), Other…"""
    from glance.languages import choices

    out = [ShotButton(f"lang:{name}", name, selected=name.casefold() == current.casefold())
           for name in choices(current)]
    return (*out, ShotButton("lang_other", OTHER_LANGUAGE))


ANSWER_ACTIONS = (ShotButton("text_copy", "Copy", primary=True),)
TRANSLATION_ACTIONS = (ShotButton("text_copy", "Copy", primary=True),
                       ShotButton("translate_lang", "Change language"))
