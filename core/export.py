from __future__ import annotations

import io
from collections import Counter
from typing import Any

import pymupdf as fitz
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

# Bluebeam count markup. The annotation Subject carries the tag name itself, so the
# Markups List identifies every tag instead of showing a generic "FLOOR UNIT" on every
# row. Contents stay "<label>\r1" so Bluebeam's count summary still totals them.
MARKUP_SIZE = 27.0
MARKUP_AUTHOR = "Unit Matrix"
UNNAMED = "?"
# A floor tag the manager placed under a unit plan by hand, marked the same way on screen.
MANUAL_FONT = Font(color="C62828", bold=True)


def label_of(tag: dict[str, Any]) -> str:
    """The reported name of a tag: its type code, else its unit number, else '?'."""
    for key in ("unit", "unit_no"):
        value = str(tag.get(key) or "").strip()
        if value:
            return value
    return UNNAMED


def summarise(tags: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return sorted(Counter(label_of(t) for t in tags).items(), key=lambda kv: kv[0])


def _floor(tag: dict[str, Any]) -> str:
    return str(tag.get("floor_level") or "").strip() or "Not identified"


def build_xlsx(tags: list[dict[str, Any]], units: list[dict[str, Any]] | None = None,
               merged: list[dict[str, Any]] | None = None,
               sheet_of: dict[int, str] | None = None) -> bytes:
    """Four tabs: Unit Tags, Floor Tags, Floor Tags by Level & Sheet, Merged.

    `units` are the labels read off the unit-plan sheets, `merged` the grouped table from
    the matcher, and `sheet_of` maps a page number to its sheet number for tab three.
    """
    bold = Font(bold=True)
    sheet_of = sheet_of or {}
    wb = Workbook()
    written = []

    # 1. the unit plans drawn on the unit-plan sheets
    unit_tab = wb.active
    unit_tab.title = "Unit Tags"
    unit_tab.append(["Unit", "Count"])
    unit_counts = Counter(label_of(u) for u in (units or []))
    for name in sorted(unit_counts):
        unit_tab.append([name, unit_counts[name]])
    unit_tab.append(["Grand Total", sum(unit_counts.values())])
    written.append(unit_tab)

    # 2. the floor tags counted on the floor plans
    floor_tab = wb.create_sheet("Floor Tags")
    floor_tab.append(["Floor Tag", "Qty"])
    summary = summarise(tags)
    for label, qty in summary:
        floor_tab.append([label, qty])
    floor_tab.append(["Grand Total", sum(q for _, q in summary)])
    written.append(floor_tab)

    # 3. the same floor tags, broken out by the level and the sheet they were found on
    detail = wb.create_sheet("Floor Tags by Level & Sheet")
    detail.append(["Floor Tag", "Level", "Sheet", "Page", "Qty"])
    rows = Counter((label_of(t), _floor(t), sheet_of.get(int(t["page"]), "") or "—",
                    int(t["page"])) for t in tags)
    for (label, floor, sheet, page), qty in sorted(rows.items()):
        detail.append([label, floor, sheet, page, qty])
    detail.append(["Grand Total", "", "", "", sum(rows.values())])
    detail.column_dimensions["B"].width = 20
    written.append(detail)

    # 4. the floor tags grouped under their unit plan
    merge_tab = wb.create_sheet("Merged")
    merge_tab.append(["Floor Tag", "Unit Plan", "Qty", "Merge Qty"])
    for row in (merged or []):
        merge_tab.append([row.get("tag", ""), row.get("unit", ""),
                          row.get("qty", ""), row.get("merged", "")])
        if row.get("manual"):
            merge_tab.cell(row=merge_tab.max_row, column=1).font = MANUAL_FONT
    # Merge the unit plan and its merged quantity down each group, as the manual takeoffs
    # do: a group starts at a row that names a plan and runs to the row before the next one.
    rows_out = list(merged or [])
    starts = [i for i, row in enumerate(rows_out) if str(row.get("unit") or "")]
    for position, start in enumerate(starts):
        end = starts[position + 1] - 1 if position + 1 < len(starts) else len(rows_out) - 1
        if end > start:
            for column in ("B", "D"):
                merge_tab.merge_cells(f"{column}{start + 2}:{column}{end + 2}")
    centre = Alignment(vertical="center")
    for row in merge_tab.iter_rows(min_row=2, max_row=merge_tab.max_row):
        for cell in row:
            cell.alignment = centre

    merge_tab.append(["Grand Total", "",
                      sum(r["qty"] for r in rows_out if isinstance(r.get("qty"), int)),
                      sum(r["merged"] for r in rows_out if isinstance(r.get("merged"), int))])
    written.append(merge_tab)

    for sheet in written:
        for cell in (*sheet[1], *sheet[sheet.max_row]):
            cell.font = bold
        sheet.column_dimensions["A"].width = 22
        sheet.freeze_panes = "A2"
    for column in ("B", "C", "D"):
        merge_tab.column_dimensions[column].width = 14

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_pdf(pdf_bytes: bytes, tags: list[dict[str, Any]],
              units: list[dict[str, Any]] | None = None) -> bytes:
    """A red Polygon count markup on every tag, its Subject set to the tag name.

    The unit labels from the unit-plan sheets get the same markup, on their drawing title.
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for tag in list(tags) + list(units or []):
            page_number = int(tag["page"])
            if not 1 <= page_number <= len(doc):
                continue
            name = label_of(tag)
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
            annot.set_info(title=MARKUP_AUTHOR, subject=name, content=f"{name}\r1")
            annot.update()
        return doc.tobytes(garbage=3, deflate=True)
