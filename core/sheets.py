from __future__ import annotations

import re
from typing import Any

import pymupdf as fitz

# A sheet number in the title block: G0.00B, H1.01B, M111, M2.01A, A113, P100.
SHEET_NUMBER_RE = re.compile(r"^([A-Z]{1,2})[\s-]?(\d{1,3}(?:\.\d{1,2})?)([A-Z])?$")
# Mechanical disciplines: M = mechanical, H = HVAC (Emma's Court numbers its HVAC set H0.01-H3.01B).
MECHANICAL_PREFIXES = ("M", "H")


def sheet_number(page: fitz.Page) -> str:
    """The sheet number from the title block: the largest text on the page shaped like one.

    Title blocks print the sheet number far larger than anything else around it, so size
    picks it out without having to know where each office puts its title block.
    """
    best = (0.0, "")
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = " ".join(s["text"] for s in spans).strip()
            if spans and SHEET_NUMBER_RE.match(text):
                size = max(s["size"] for s in spans)
                if size > best[0]:
                    best = (size, text)
    return best[1]


def discipline(sheet: str) -> str:
    """The leading letters of a sheet number: M, H, A, G, P, E…"""
    match = SHEET_NUMBER_RE.match(sheet or "")
    return match.group(1).upper() if match else ""


def sheet_series(sheet: str) -> str:
    """Sheet family: M2.01A -> M2, M111 -> M1, H1.01B -> H1.

    Within one discipline the first digit separates the families — notes and legends,
    overall plans, enlarged plans, details, schedules — so it groups the page picker.
    """
    match = SHEET_NUMBER_RE.match(sheet or "")
    if not match:
        return ""
    return f"{match.group(1).upper()}{match.group(2)[0]}"


def is_mechanical(sheet: str) -> bool:
    return discipline(sheet) in MECHANICAL_PREFIXES


def sheet_index(doc: fitz.Document) -> dict[int, dict[str, Any]]:
    """Sheet number, discipline and series for every page."""
    index = {}
    for number, page in enumerate(doc, start=1):
        sheet = sheet_number(page)
        index[number] = {
            "sheet": sheet,
            "discipline": discipline(sheet),
            "series": sheet_series(sheet),
            "mechanical": is_mechanical(sheet),
        }
    return index
