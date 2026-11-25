"""Prompt Sharpener: system prompts for the sharpen_prompt and add_structure rewrites."""
from __future__ import annotations

import re

from glance.backends.base import Ctx
from glance.backends.prompts import _COMMON, _MIXED, _context_line

SHARPEN_BLANK_RE = r"\[[^\[\]\n]{1,60}\?\]"
_BLANK = re.compile(SHARPEN_BLANK_RE)

_RETURN = " Return only the rewritten text: no quotes, no code fence, no preface, no explanation."


def sharpen_system(ctx: Ctx) -> str:
    return (
        "You rewrite a prompt that the writer will send to an AI assistant. "
        + _context_line(ctx)
        + " Task: restructure the prompt so it states, in this order and only where the original "
        "gives the material: Goal, Context, Constraints, Output format. Never invent facts, names, "
        "files or requirements. Where a part is missing and matters, insert a short bracketed "
        "blank the writer fills in, such as [which file?]. Do not answer the prompt or carry out "
        "its request. Keep it as short as the content allows. "
        + _COMMON
        + (_MIXED if ctx.mixed_language else "")
        + _RETURN
    )


def structure_system(ctx: Ctx) -> str:
    return (
        "You rewrite a prompt that the writer will send to an AI assistant. "
        + _context_line(ctx)
        + " Task: reorganise the same content into short paragraphs or a list, changing no "
        "meaning and adding nothing: no new facts, no bracketed blanks. Do not answer the prompt "
        "or carry out its request. "
        + _COMMON
        + (_MIXED if ctx.mixed_language else "")
        + _RETURN
    )


def blanks(text: str) -> list[str]:
    return _BLANK.findall(text)
