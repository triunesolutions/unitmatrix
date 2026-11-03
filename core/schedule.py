from __future__ import annotations

import re
import statistics
from typing import Any

import pymupdf as fitz

# The architect's own count table. Three layouts appear in practice:
#   grouped  - "LEVEL 1" row, then NAME / COUNT rows beneath it (Parkvue, Deer Valley)
#   wide     - one row per unit with a column per level: L1 QTY … L7 QTY, TOTAL (Edge on Main)
#   flat     - NAME / COUNT with no levels at all
# Sheets often carry two such tables side by side, so each is scoped to its own column band.
TITLE_RE = re.compile(r"\bUNIT\s+(?:MIX|MATRIX|SCHEDULE)\b|\bSCHEDULE\s+OF\s+UNITS\b", re.I)
NAME_HEADER_RE = re.compile(r"^(?:UNIT\s+)?NAMES?$|^UNIT$|^UNIT\s+PLAN$|^TYPE\s+NAME$", re.I)
QTY_HEADER_RE = re.compile(r"^(?:COUNT|QTY|QUANTITY|TOTAL|NO\.?|#)$", re.I)
LEVEL_HEADER_RE = re.compile(r"^L(?:EVEL|VL)?\.?\s*(\d{1,2})\s*(?:QTY|COUNT)?$", re.I)
LEVEL_ROW_RE = re.compile(
    r"^(?:LEVEL|FLOOR|LVL)\.?\s*([0-9]{1,2}[A-Z]?)$"
    r"|^(GROUND|BASEMENT|PODIUM|MEZZANINE|ROOF|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH)"
    r"(?:\s+(?:FLOOR|LEVEL))?$", re.I)
TOTAL_ROW_RE = re.compile(r"\b(?:GRAND\s+TOTAL|TOTALS?|SUBTOTAL)\b", re.I)
INT_RE = re.compile(r"^\d{1,4}$")
ROW_TOLERANCE = 3.5
COLUMN_TOLERANCE = 26.0
# A table's header row sits just below its title and within its column band. Without these
# bounds a legend's "Name" or a stray "TOTAL" elsewhere on the sheet joins the wrong table.
TITLE_GAP = 70.0
TITLE_SPAN = 320.0


def _cells(page: fitz.Page) -> list[dict[str, Any]]:
    cells = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = re.sub(r"\s+", " ", " ".join(s.get("text", "") for s in spans)).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            cells.append({"text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                          "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2})
    return cells


def _rows(cells: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group cells into visual rows by vertical centre."""
    rows: list[list[dict[str, Any]]] = []
    for cell in sorted(cells, key=lambda c: (c["cy"], c["x0"])):
        if rows and abs(rows[-1][0]["cy"] - cell["cy"]) <= ROW_TOLERANCE:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    for row in rows:
        row.sort(key=lambda c: c["x0"])
    return rows


def _nearest(cells: list[dict[str, Any]], centre: float) -> dict[str, Any] | None:
    best, best_distance = None, COLUMN_TOLERANCE
    for cell in cells:
        distance = abs(cell["cx"] - centre)
        if distance <= best_distance:
            best, best_distance = cell, distance
    return best


def find_schedules(page: fitz.Page) -> list[dict[str, Any]]:
    """Every architect count table on the page, parsed.

    Several candidates are normal (a by-level table beside a unit-area table), so each is
    returned with its own rows and total for the user to choose between.
    """
    cells = _cells(page)
    headers = [c for c in cells
               if NAME_HEADER_RE.match(c["text"]) or QTY_HEADER_RE.match(c["text"])
               or LEVEL_HEADER_RE.match(c["text"])]
    if not headers:
        return []
    titles = [c for c in cells if TITLE_RE.search(c["text"]) and len(c["text"]) <= 60]

    # Group header cells into tables: by nearest title when the sheet labels its tables,
    # otherwise by header row.
    groups: dict[Any, list[dict[str, Any]]] = {}
    for header in headers:
        if titles:
            near = [t for t in titles
                    if abs(t["cx"] - header["cx"]) <= TITLE_SPAN
                    and -ROW_TOLERANCE <= header["cy"] - t["cy"] <= TITLE_GAP]
            if not near:
                continue
            anchor = min(near, key=lambda t: abs(t["cx"] - header["cx"]))
            groups.setdefault((anchor["text"].upper(), round(anchor["cx"])), []).append(header)
        else:
            groups.setdefault(round(header["cy"] / 6), []).append(header)

    schedules = []
    for key, group in groups.items():
        parsed = _parse_table(cells, group, titles)
        if parsed and parsed["rows"]:
            parsed["title"] = key[0].title() if isinstance(key, tuple) else "Unlabelled table"
            schedules.append(parsed)

    # The same table split into side-by-side column blocks shares one title: merge them.
    merged: dict[str, dict[str, Any]] = {}
    for schedule in schedules:
        existing = merged.get(schedule["title"])
        if existing:
            existing["rows"].extend(schedule["rows"])
            existing["layout"] = existing["layout"] or schedule["layout"]
        else:
            merged[schedule["title"]] = schedule
    for schedule in merged.values():
        schedule["total"] = sum(r["qty"] for r in schedule["rows"])
    return sorted(merged.values(), key=lambda s: -s["total"])


def _parse_table(cells: list[dict[str, Any]], headers: list[dict[str, Any]],
                 titles: list[dict[str, Any]]) -> dict[str, Any] | None:
    name_cols = [h for h in headers if NAME_HEADER_RE.match(h["text"])]
    if not name_cols:
        return None
    # Columns are only the cells on the name header's own row. "LEVEL 1" on a row of its own
    # is a group heading inside the table body, not a column of a wide matrix.
    row_y = name_cols[0]["cy"]
    same_row = [h for h in headers if abs(h["cy"] - row_y) <= ROW_TOLERANCE]
    level_cols = [h for h in same_row if LEVEL_HEADER_RE.match(h["text"])]
    if len(level_cols) < 2:
        level_cols = []
    qty_cols = [h for h in same_row if QTY_HEADER_RE.match(h["text"])]
    if not (level_cols or qty_cols):
        return None
    headers = same_row or headers

    header_y = statistics.median([h["cy"] for h in headers])
    left = min(h["x0"] for h in headers) - 40
    right = max(h["x1"] for h in headers) + 30
    name_centre = name_cols[0]["cx"]
    # A wide table's TOTAL column is a check, not a level, so it is not counted.
    qty_centre = min((q for q in qty_cols if not level_cols),
                     key=lambda q: q["cx"], default=None)
    qty_centre = qty_centre["cx"] if qty_centre else None

    body = [c for c in cells if c["cy"] > header_y + ROW_TOLERANCE and left <= c["cx"] <= right]
    rows: list[dict[str, Any]] = []
    level = ""
    blanks = 0
    for row in _rows(body):
        texts = [c["text"] for c in row]
        if any(TOTAL_ROW_RE.search(t) for t in texts) and not any(INT_RE.match(t) is None for t in texts[1:2]):
            pass  # a totals row ends the table only once rows have been read
        level_cell = next((c for c in row if LEVEL_ROW_RE.match(c["text"])), None)
        if level_cell and len(row) <= 3:
            match = LEVEL_ROW_RE.match(level_cell["text"])
            level = (match.group(1) or match.group(2)).upper()
            blanks = 0
            continue
        name_cell = _nearest([c for c in row if c is not level_cell], name_centre)
        if name_cell is None or TOTAL_ROW_RE.search(name_cell["text"]) or INT_RE.match(name_cell["text"]):
            blanks += 1
            if rows and blanks >= 4:
                break
            continue
        unit = re.sub(r"^UNIT\s+", "", name_cell["text"], flags=re.I).strip()
        if not unit or len(unit) > 24:
            blanks += 1
            continue
        numbers = [c for c in row if INT_RE.match(c["text"])]
        # Tight rows render the count flush against the name, so PyMuPDF returns one cell
        # ("Unit 3-0 Type A 1"). Split the trailing integer off only when the cell really
        # overruns into the count column, so names ending in a digit survive.
        merged = re.match(r"^(.*[A-Za-z].*?)\s+(\d{1,4})$", unit)
        if (merged and not numbers and qty_centre is not None
                and name_cell["x1"] >= qty_centre - COLUMN_TOLERANCE):
            unit = merged.group(1).strip()
            numbers = [{"text": merged.group(2), "cx": qty_centre}]
        found = False
        if level_cols:
            for column in level_cols:
                cell = _nearest(numbers, column["cx"])
                quantity = int(cell["text"]) if cell else 0
                if quantity:
                    rows.append({"unit": unit, "level": LEVEL_HEADER_RE.match(column["text"]).group(1),
                                 "qty": quantity})
                    found = True
        elif qty_centre is not None:
            cell = _nearest(numbers, qty_centre)
            if cell and int(cell["text"]):
                rows.append({"unit": unit, "level": level, "qty": int(cell["text"])})
                found = True
        blanks = 0 if found else blanks + 1
        if rows and blanks >= 4:
            break

    layout = "wide" if level_cols else ("grouped" if any(r["level"] for r in rows) else "flat")
    return {"rows": rows, "layout": layout, "total": sum(r["qty"] for r in rows)}


def level_key(value: str) -> str:
    """Normalise a level label so the architect's and ours can be compared.

    'LEVEL 3', 'Level 3', '3' and 'L3' all become '3'; named floors keep their word.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    digits = re.search(r"\d{1,2}", text)
    if digits:
        return digits.group(0).lstrip("0") or "0"
    for word in ("GROUND", "BASEMENT", "PODIUM", "MEZZANINE", "ROOF"):
        if word in text:
            return word
    return text


def unit_key(value: str) -> str:
    """Normalise a unit name for matching: the architect writes "3-0 Type A", plans "3-0 TYPE A"."""
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def compare(schedule_rows: list[dict[str, Any]], tags: list[dict[str, Any]]) -> dict[str, Any]:
    """Our counted tags against the architect's table, by unit type and by unit x level."""
    label: dict[str, str] = {}
    counted: dict[str, int] = {}
    counted_level: dict[tuple[str, str], int] = {}
    for tag in tags:
        unit = str(tag.get("unit", "")).strip()
        if not unit:
            continue
        key = unit_key(unit)
        label.setdefault(key, unit)
        counted[key] = counted.get(key, 0) + 1
        pair = (key, level_key(tag.get("floor_level", "")))
        counted_level[pair] = counted_level.get(pair, 0) + 1

    scheduled: dict[str, int] = {}
    scheduled_level: dict[tuple[str, str], int] = {}
    for row in schedule_rows:
        unit = str(row.get("unit", "")).strip()
        if not unit:
            continue
        key = unit_key(unit)
        label[key] = unit  # the architect's spelling is the one to show
        quantity = int(row.get("qty", 0) or 0)
        scheduled[key] = scheduled.get(key, 0) + quantity
        pair = (key, level_key(row.get("level", "")))
        scheduled_level[pair] = scheduled_level.get(pair, 0) + quantity

    by_unit = []
    for unit in sorted(set(scheduled) | set(counted), key=str.casefold):
        architect, ours = scheduled.get(unit, 0), counted.get(unit, 0)
        by_unit.append({"UNIT": label.get(unit, unit), "ARCHITECT": architect, "COUNTED": ours,
                        "VARIANCE": ours - architect,
                        "CHECK": "OK" if ours == architect else ("MISSING" if ours < architect else "EXTRA")})

    has_levels = any(key[1] for key in scheduled_level)
    by_level = []
    if has_levels:
        for key in sorted(set(scheduled_level) | set(counted_level),
                          key=lambda k: (k[0].casefold(), _level_sort(k[1]))):
            architect, ours = scheduled_level.get(key, 0), counted_level.get(key, 0)
            by_level.append({"UNIT": label.get(key[0], key[0]), "LEVEL": key[1] or "Not identified",
                             "ARCHITECT": architect, "COUNTED": ours, "VARIANCE": ours - architect,
                             "CHECK": "OK" if ours == architect else ("MISSING" if ours < architect else "EXTRA")})

    return {
        "by_unit": by_unit, "by_level": by_level,
        "architect_total": sum(scheduled.values()), "counted_total": sum(counted.values()),
    }


def _level_sort(level: str) -> tuple[int, str]:
    return (0, f"{int(level):03d}") if level.isdigit() else (1, level)
