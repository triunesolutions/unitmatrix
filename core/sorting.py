"""Shared natural ordering for labels and worksheet rows."""
from __future__ import annotations

import re
from typing import Any


def natural_key(value: Any) -> tuple:
    """Case-insensitive natural sort key (so A2 sorts before A10)."""
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", str(value or "")))
