"""Helpers for safely renaming detected tag groups."""
from __future__ import annotations

from typing import Any


def normalised_original(tag: dict[str, Any]) -> str:
    """Case and whitespace insensitive key for an immutable original tag name."""
    return " ".join(str(tag.get("original_unit") or "").split()).casefold()


def propagate_rename(tags: list[dict[str, Any]], current: dict[str, Any],
                     new_name: str) -> None:
    """Update editable names for this list's records in the same original-name group."""
    group = normalised_original(current)
    if not group:
        current["unit"] = new_name
        return
    for tag in tags:
        if normalised_original(tag) == group:
            tag["unit"] = new_name
