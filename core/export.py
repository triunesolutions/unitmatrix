from __future__ import annotations

import io
from collections import Counter
from typing import Any

import pymupdf as fitz
from openpyxl import Workbook
from openpyxl.styles import Font

# Bluebeam count markup, same shape as the manual takeoff (Polygon, subject FLOOR UNIT,
# contents "<unit>\r1"), so Bluebeam's markup summary counts the exported tags too.
MARKUP_SIZE = 27.0
MARKUP_SUBJECT = "FLOOR UNIT"
MARKUP_AUTHOR = "Unit Matrix"


def summarise(tags: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts = Counter(str(t["unit"]).strip() for t in tags if str(t.get("unit", "")).strip())
    return sorted(counts.items(), key=lambda kv: kv[0])


def _floor(tag: dict[str, Any]) -> str:
    return str(tag.get("floor_level") or "").strip() or "Not identified"


def build_xlsx(tags: list[dict[str, Any]]) -> bytes:
    """UNIT_MATRIX (final table) / TO (pivot) / DATA (one row per tag), as in UNIT_MATRIX.xlsx,
    plus UNIT FLOOR COUNT (unit x floor)."""
    summary = summarise(tags)
    bold = Font(bold=True)
    wb = Workbook()

    ws = wb.active
    ws.title = "UNIT_MATRIX"
    ws.append(["UNIT", "Total"])
    for unit, qty in summary:
        ws.append([unit, qty])
    ws.append(["Grand Total", f"=SUM(B2:B{len(summary) + 1})"])
    for cell in (*ws[1], *ws[ws.max_row]):
        cell.font = bold

    to = wb.create_sheet("TO")
    to.append(["Sum of QTY"])
    to.append(["UNIT", "Total"])
    for unit, qty in summary:
        to.append([unit, qty])
    to.append(["Grand Total", sum(q for _, q in summary)])
    for cell in (*to[2], *to[to.max_row]):
        cell.font = bold

    data = wb.create_sheet("DATA")
    data.append(["UNIT", "QTY", "Count"])
    for tag in sorted(tags, key=lambda t: (t["page"], t["y"], t["x"])):
        if str(tag.get("unit", "")).strip():
            data.append([str(tag["unit"]).strip(), 1, 1])
    for cell in data[1]:
        cell.font = bold

    floors = wb.create_sheet("UNIT FLOOR COUNT")
    floors.append(["UNIT", "Total", "FLOOR / LEVEL"])
    named = [t for t in tags if str(t.get("unit", "")).strip()]
    # Floors follow sheet order, so GROUND comes before SECOND and LEVEL 2 before LEVEL 10.
    first_page: dict[str, int] = {}
    for tag in named:
        floor = _floor(tag)
        first_page[floor] = min(first_page.get(floor, tag["page"]), tag["page"])
    counts = Counter((str(t["unit"]).strip(), _floor(t)) for t in named)
    for (unit, floor), qty in sorted(counts.items(), key=lambda kv: (kv[0][0], first_page[kv[0][1]])):
        floors.append([unit, qty, floor])
    floors.append(["Grand Total", f"=SUM(B2:B{len(counts) + 1})"])
    for cell in (*floors[1], *floors[floors.max_row]):
        cell.font = bold
    floors.column_dimensions["C"].width = 22

    for sheet in (ws, to, data, floors):
        sheet.column_dimensions["A"].width = 24
        sheet.column_dimensions["B"].width = 12

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_pdf(pdf_bytes: bytes, tags: list[dict[str, Any]]) -> bytes:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for tag in tags:
            unit = str(tag.get("unit", "")).strip()
            page_number = int(tag["page"])
            if not unit or not 1 <= page_number <= len(doc):
                continue
            page = doc[page_number - 1]
            cx = float(tag["x"]) + float(tag["w"]) / 2
            cy = float(tag["y"]) + float(tag["h"]) / 2
            half = MARKUP_SIZE / 2
            cx = min(max(cx, half), page.rect.width - half)
            cy = min(max(cy, half), page.rect.height - half)
            points = [(cx - half, cy - half), (cx + half, cy - half),
                      (cx + half, cy + half), (cx - half, cy + half), (cx - half, cy - half)]
            annot = page.add_polygon_annot(points)
            annot.set_colors(stroke=(1, 0, 0))
            annot.set_border(width=1.5)
            annot.set_info(title=MARKUP_AUTHOR, subject=MARKUP_SUBJECT, content=f"{unit}\r1")
            annot.update()
        return doc.tobytes(garbage=3, deflate=True)
