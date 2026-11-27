"""The languages Translate offers as one-click choices (anything else: "Other…", any name the
AI model knows). The most-used languages first, then this project's own users' (Bangla)."""
from __future__ import annotations

POPULAR: tuple[str, ...] = (
    "English", "Bangla", "Hindi", "Spanish", "French", "German", "Arabic", "Chinese",
    "Japanese", "Portuguese", "Russian", "Urdu",
)


def choices(current: str) -> tuple[str, ...]:
    """POPULAR, with `current` (the last one used) first when it is not one of them."""
    current = current.strip()
    if current and current.casefold() not in {x.casefold() for x in POPULAR}:
        return (current, *POPULAR)
    return POPULAR
