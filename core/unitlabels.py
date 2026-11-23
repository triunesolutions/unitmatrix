"""Unit labels on the unit-plan sheets.

A unit-plan sheet draws one enlarged plan per unit type. Each drawing is titled underneath,
with the scale printed on the line directly below it:

    UNIT MI0.1
    1/4" = 1'-0"

So the title line is "UNIT <code>", and the scale line beneath it confirms the line is a
drawing title rather than a note that happens to mention a unit. The code must contain a
digit, which keeps the sheet heading "UNIT PLANS" and notes such as "FAN COIL UNIT" out.

A title may carry a suffix ("UNIT MA1.1A - GRP 2"); it is kept, normalised to MA1.1A-GRP2,
because the suffix distinguishes two drawings of the same unit.
"""
from __future__ import annotations

import re
from typing import Any

import pymupdf as fitz

from .detect import _lines

TITLE_RE = re.compile(r"^UNIT\s+([A-Z]{0,4}\d[A-Z0-9.\-]*)\s*(.*)$", re.I)
# 1/4" = 1'-0",  3/8" = 1'-0",  1" = 1'
SCALE_RE = re.compile(r"=\s*\d+\s*'")
# How far below the title, and how far sideways, the scale line may sit.
SCALE_DY = 40.0
SCALE_DX = 40.0


# "UNIT A3b - MECHANICAL" names the discipline, not a second drawing of A3b, so the
# suffix is dropped. A suffix like "- GRP 2" is kept: it does distinguish two drawings.
DISCIPLINE_RE = re.compile(r"^(MECHANICAL|PLUMBING|ELECTRICAL|HVAC|M|P|E)$", re.I)


def _name(code: str, extra: str) -> str:
    """MA1.1A + "- GRP 2" -> MA1.1A-GRP2; A3B + "- MECHANICAL" -> A3B."""
    code = code.upper().strip(".-")
    extra = extra.upper().strip(" -")
    if DISCIPLINE_RE.match(extra):
        return code
    return f"{code}-{re.sub(r'\s+', '', extra)}" if extra else code


def detect_unit_labels(page: fitz.Page, page_number: int) -> list[dict[str, Any]]:
    """One tag per unit plan drawn on the sheet, placed on its title."""
    lines = _lines(page)
    scales = [l for l in lines if SCALE_RE.search(l["text"])]
    found: list[dict[str, Any]] = []
    for line in lines:
        match = TITLE_RE.match(line["text"].strip())
        if not match:
            continue
        box = line["bbox"]
        scaled = any(0 <= s["bbox"].y0 - box.y0 <= SCALE_DY
                     and abs(s["bbox"].x0 - box.x0) <= SCALE_DX for s in scales)
        found.append({
            "page": page_number, "unit": _name(match.group(1), match.group(2)),
            "original_unit": _name(match.group(1), match.group(2)),
            "tag_id": f"{page_number}:{round(box.x0, 1)}:{round(box.y0, 1)}:{round(box.width, 1)}:{round(box.height, 1)}",
            "unit_no": "", "matched_text": line["text"].strip(),
            "x": round(box.x0, 1), "y": round(box.y0, 1),
            "w": round(box.width, 1), "h": round(box.height, 1),
            "source": line["source"], "floor_level": "",
            "scale_confirmed": scaled,
            # A line that carried a suffix is the drawing's title, not a tag inside it.
            "titled": bool(match.group(2).strip(" -")),
        })
    # Prefer the titles the scale line confirms. If none on the sheet carries one (a set that
    # prints the scale elsewhere), keep them all rather than return nothing.
    confirmed = [f for f in found if f["scale_confirmed"]]
    found = confirmed or found

    # One drawing often carries its name twice: the title underneath ("UNIT A3b -
    # MECHANICAL") and the same name again as a room tag inside it ("UNIT A3b"). That is
    # one unit plan, not two, so keep a single entry per name per sheet — the title line,
    # which is the one a takeoff marks.
    best: dict[str, dict[str, Any]] = {}
    for entry in found:
        kept = best.get(entry["unit"])
        if kept is None or (entry["titled"], entry["scale_confirmed"]) > (kept["titled"],
                                                                         kept["scale_confirmed"]):
            best[entry["unit"]] = entry
    return list(best.values())


def suggest_unit_label_pages(doc: fitz.Document) -> dict[int, int]:
    """Sheets that look like unit-plan sheets -> how many unit plans they draw."""
    pages: dict[int, int] = {}
    for number, page in enumerate(doc, start=1):
        if not re.search(r"\bUNIT\s+[A-Z]{0,4}\d", page.get_text(), re.I):
            continue
        count = len(detect_unit_labels(page, number))
        if count:
            pages[number] = count
    return pages
