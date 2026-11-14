from __future__ import annotations

import re
from typing import Any

import pymupdf as fitz

from .detect import _lines

# A unit-plan sheet draws one enlarged plan per unit type. Each drawing carries its title
# underneath ("UNIT MI1.2A", sometimes with a suffix such as "- GRP 2") and, inside the
# drawing, the greyed room tag of the unit it is drawn from ("MI1.2 / 612 SF / 1 BED").
# Pairing the tag with the drawing it sits in says which floor-plan label uses which plan.
# The code must contain a digit, so "UNIT PLANS" and "UNIT HEATER" are not read as one.
TITLE_RE = re.compile(r"^UNIT\s+([A-Z]{0,4}\d[A-Z0-9.\-]*)\s*(.*)$", re.I)
TITLE_ROW_TOLERANCE = 25.0
# A drawing's title starts at or just left of anything inside it.
TITLE_LEFT_SLACK = 60.0


def _titles(page: fitz.Page) -> list[dict[str, Any]]:
    found = []
    for line in _lines(page):
        match = TITLE_RE.match(line["text"].strip())
        if match:
            found.append({"plan": match.group(1).upper().strip(".-"),
                          "extra": match.group(2).strip(),
                          "x": line["bbox"].x0, "y": line["bbox"].y0})
    return found


def suggest_unit_plan_pages(doc: fitz.Document) -> dict[int, int]:
    """Sheets that look like unit-plan sheets -> how many unit plans they draw."""
    pages = {}
    for number, page in enumerate(doc, start=1):
        if not re.search(r"\bUNIT\s+[A-Z]{0,4}\d", page.get_text(), re.I):
            continue
        count = len(_titles(page))
        if count:
            pages[number] = count
    return pages


def _owner(tag: dict[str, Any], titles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The drawing a room tag sits inside: nearest title row below it, starting at or left."""
    below = [t for t in titles if t["y"] > tag["y"]]
    if not below:
        return None
    row_y = min(t["y"] for t in below)
    row = [t for t in below if abs(t["y"] - row_y) <= TITLE_ROW_TOLERANCE]
    left = [t for t in row if t["x"] <= tag["x"] + TITLE_LEFT_SLACK]
    return max(left, key=lambda t: t["x"]) if left else min(row, key=lambda t: abs(t["x"] - tag["x"]))


def unit_plan_map(doc: fitz.Document, pages: list[int], labels: list[str]) -> dict[str, dict[str, Any]]:
    """{label: {plan, extra, page}} read off the unit-plan sheets.

    Only lines that are exactly a known floor-plan label count as room tags, so dimensions
    and equipment tags on the same sheet cannot be mistaken for one.
    """
    wanted = {str(l).strip().upper() for l in labels if str(l).strip()}
    found: dict[str, dict[str, Any]] = {}
    for number in pages:
        if not 1 <= number <= len(doc):
            continue
        page = doc[number - 1]
        titles = _titles(page)
        if not titles:
            continue
        for line in _lines(page):
            text = line["text"].strip().upper()
            if text not in wanted or TITLE_RE.match(text):
                continue
            owner = _owner({"x": line["bbox"].x0, "y": line["bbox"].y0}, titles)
            if owner:
                found.setdefault(text, {"plan": owner["plan"], "extra": owner["extra"], "page": number})
    return found


def merged_rows(counts: list[tuple[str, int]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Label / UNIT PLAN / Total / MERG QTY, grouped so only a group's first row is filled in.

    A label with no unit plan drawn for it bills against itself, as in the manual takeoffs.
    """
    plan_of = {label: (mapping.get(label) or label) for label, _ in counts}
    # Keep each plan's labels together, ordered by where the group first appears.
    first: dict[str, str] = {}
    for label, _ in sorted(counts):
        first.setdefault(plan_of[label], label)
    ordered = sorted(counts, key=lambda c: (first[plan_of[c[0]]], c[0]))

    rows: list[dict[str, Any]] = []
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and plan_of[ordered[end + 1][0]] == plan_of[ordered[index][0]]:
            end += 1
        group = ordered[index:end + 1]
        merged = sum(total for _, total in group)
        for position, (label, total) in enumerate(group):
            rows.append({
                "Label": label,
                "UNIT PLAN": plan_of[label] if position == 0 else "",
                "Total": total,
                "MERG QTY": merged if position == 0 else "",
            })
        index = end + 1
    return rows
