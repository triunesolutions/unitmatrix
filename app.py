from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pymupdf as fitz
import streamlit as st
from PIL import Image, ImageDraw

from core.detect import detect_names, detect_unit_types, page_title, suggest_plan_pages
from core.export import build_pdf, build_xlsx, summarise
from core.schedule import compare, find_schedules
from core.sheets import sheet_index
from core.unitplans import merged_rows, suggest_unit_plan_pages, unit_plan_map

st.set_page_config(page_title="Unit Matrix", page_icon="🏢", layout="wide")

PREVIEW_ZOOM = 0.6
THUMB_ZOOM = 0.16
THUMB_COLUMNS = 5
COLUMNS = ["page", "unit", "floor_level", "unit_no", "matched_text", "x", "y", "w", "h", "source"]


@st.cache_data(show_spinner=False, max_entries=4)
def scan(file_hash: str, _pdf: bytes) -> tuple[int, dict[int, int], dict[int, str]]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return len(doc), suggest_plan_pages(doc), {i: page_title(p) for i, p in enumerate(doc, start=1)}


@st.cache_data(show_spinner=False, max_entries=256)
def detect_page(file_hash: str, _pdf: bytes, pn: int, mode: str, names: tuple[str, ...]) -> list[dict]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        page = doc[pn - 1]
        return detect_unit_types(page, pn) if mode == "auto" else detect_names(page, pn, list(names))


def detect(pdf: bytes, file_hash: str, pages: list[int], mode: str, names: tuple[str, ...]) -> list[dict]:
    tags = []
    progress = st.progress(0.0, text="Scanning pages…")
    for n, pn in enumerate(pages, start=1):
        tags += detect_page(file_hash, pdf, pn, mode, names)
        progress.progress(n / len(pages), text=f"Scanned page {pn} ({n} of {len(pages)})")
    progress.empty()
    return tags


@st.cache_data(show_spinner=False, max_entries=4)
def sheet_info(file_hash: str, _pdf: bytes) -> dict[int, dict]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return sheet_index(doc)


@st.cache_data(show_spinner=False, max_entries=4)
def unit_plan_sheets(file_hash: str, _pdf: bytes) -> dict[int, int]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return suggest_unit_plan_pages(doc)


@st.cache_data(show_spinner=False, max_entries=8)
def unit_plans_from(file_hash: str, _pdf: bytes, pages: tuple[int, ...],
                    labels: tuple[str, ...]) -> dict[str, dict]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return unit_plan_map(doc, list(pages), list(labels))


@st.cache_data(show_spinner=False, max_entries=4)
def schedules_on(file_hash: str, _pdf: bytes, pn: int) -> list[dict]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return find_schedules(doc[pn - 1])


@st.cache_data(show_spinner=False, max_entries=512)
def thumbnail(file_hash: str, _pdf: bytes, pn: int) -> Image.Image:
    """Small render of one page for the visual picker."""
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        pix = doc[pn - 1].get_pixmap(matrix=fitz.Matrix(THUMB_ZOOM, THUMB_ZOOM), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


@st.cache_data(show_spinner=False, max_entries=16)
def page_image(file_hash: str, _pdf: bytes, pn: int) -> Image.Image:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        pix = doc[pn - 1].get_pixmap(matrix=fitz.Matrix(PREVIEW_ZOOM, PREVIEW_ZOOM), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def clean(df: pd.DataFrame) -> list[dict]:
    """Rows from the editor -> tags; rows added by hand have no position, so park them top-left."""
    df = df.copy()
    df["unit"] = df["unit"].fillna("").astype(str).str.strip()
    df = df[(df["unit"] != "") & df["page"].notna()]
    df = df.fillna({"x": 40.0, "y": 40.0, "w": 10.0, "h": 10.0, "floor_level": "", "unit_no": "", "matched_text": "", "source": "manual"})
    df["page"] = df["page"].astype(int)
    return df.to_dict("records")


def preview(image: Image.Image, tags: list[dict]) -> Image.Image:
    image = image.copy()
    draw = ImageDraw.Draw(image)
    for t in tags:
        x0, y0 = t["x"] * PREVIEW_ZOOM, t["y"] * PREVIEW_ZOOM
        x1, y1 = (t["x"] + t["w"]) * PREVIEW_ZOOM, (t["y"] + t["h"]) * PREVIEW_ZOOM
        draw.rectangle((x0 - 3, y0 - 3, x1 + 3, y1 + 3), outline=(220, 0, 0), width=3)
        draw.text((x0, y0 - 14), t["unit"], fill=(220, 0, 0))
    return image


st.title("Unit Matrix")
st.caption("Count dwelling units on floor plans and export the unit matrix (Excel) and a Bluebeam-countable PDF.")

uploaded = st.file_uploader("Upload the drawing set (PDF)", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

pdf = uploaded.getvalue()
file_hash = hashlib.md5(pdf).hexdigest()
if st.session_state.get("file_hash") != file_hash:
    st.session_state.update(file_hash=file_hash, tags=None, detection_message=None,
                            schedule=None, tagged_pdf=None)

with st.spinner("Reading the PDF and looking for floor-plan unit labels…"):
    page_count, suggested, titles = scan(file_hash, pdf)
    sheets = sheet_info(file_hash, pdf)

# 1. Pages and detection mode
st.subheader("1. Choose floor-plan pages")
mode_label = st.radio(
    "How are units labelled on the plans?",
    ["Auto: unit labels on the plans (UNIT TYPE 2C + 3BD, UNIT A1.0, …)", "Match my list of unit names"],
    horizontal=True,
)
mode = "auto" if mode_label.startswith("Auto") else "names"
names: list[str] = []
if mode == "names":
    names = [n.strip() for n in st.text_area(
        "Unit names to find, one per line, exactly as written on the plans",
        placeholder="A1\nA1 TYPE A\nA2 END",
    ).splitlines() if n.strip()]


def page_label(pn: int) -> str:
    bits = [f"Page {pn}"]
    if sheets.get(pn, {}).get("sheet"):
        bits.append(sheets[pn]["sheet"])
    if titles.get(pn):
        bits.append(titles[pn])
    if pn in suggested:
        bits.append(f"{suggested[pn]} unit labels found")
    return " · ".join(bits)


mechanical = [p for p in range(1, page_count + 1) if sheets.get(p, {}).get("mechanical")]
only_mech = st.checkbox(
    f"Show mechanical sheets only ({len(mechanical)} of {page_count}: "
    f"{', '.join(sorted({sheets[p]['discipline'] for p in mechanical})) or 'none found'})",
    value=bool(mechanical),
    help="Mechanical and HVAC sheet numbers (M…, H…) read from each title block.",
)
st.caption(
    "Choose only the sheets that contain the floor plans you want to count. "
    "Leave out overall plans where enlarged plans cover the same units, plus roof, "
    "single-unit, RCP and schedule sheets, so each unit is counted once."
)

all_pages = mechanical if (only_mech and mechanical) else list(range(1, page_count + 1))


def pick_key(pn: int) -> str:
    return f"pick_{file_hash}_{pn}"


# The tick boxes are the single source of truth. The buttons below are rendered before
# them, so setting their state here happens before the widgets exist on this run.
select_col, clear_col, thumbs_col, count_col = st.columns([1, 1, 1.2, 1.6])
with select_col:
    select_all = st.button("Select all listed", use_container_width=True)
with clear_col:
    clear_all = st.button("Clear selection", use_container_width=True)
with thumbs_col:
    show_thumbs = st.toggle("Show page images", value=True)
for pn in all_pages:
    if select_all:
        st.session_state[pick_key(pn)] = True
    elif clear_all:
        st.session_state[pick_key(pn)] = False
with count_col:
    chosen = sum(1 for pn in all_pages if st.session_state.get(pick_key(pn)))
    st.caption(f"**{chosen} of {len(all_pages)} listed pages selected**")

if show_thumbs:
    with st.spinner("Rendering page images…"):
        for row_start in range(0, len(all_pages), THUMB_COLUMNS):
            row = all_pages[row_start:row_start + THUMB_COLUMNS]
            for column, pn in zip(st.columns(THUMB_COLUMNS), row):
                with column:
                    st.image(thumbnail(file_hash, pdf, pn), use_container_width=True)
                    found = suggested.get(pn)
                    st.checkbox(
                        f"**{pn}** · {sheets.get(pn, {}).get('sheet') or '—'}"
                        + (f" · {found} labels" if found else ""),
                        key=pick_key(pn),
                        help=page_label(pn),
                    )
else:
    for row_start in range(0, len(all_pages), 2):
        for column, pn in zip(st.columns(2), all_pages[row_start:row_start + 2]):
            with column:
                st.checkbox(page_label(pn), key=pick_key(pn))

pages = [pn for pn in all_pages if st.session_state.get(pick_key(pn))]
if pages:
    st.caption("Selected pages: " + ", ".join(str(p) for p in pages))

if st.button("Detect units", type="primary", disabled=not pages or (mode == "names" and not names)):
    detected = detect(pdf, file_hash, sorted(pages), mode, tuple(names))
    if detected:
        st.session_state.tags = pd.DataFrame(detected, columns=COLUMNS)
        st.session_state.detection_message = f"Detected {len(detected)} unit labels across {len(pages)} selected page(s)."
    else:
        # Do not let an empty detection run replace valid prior results or create a blank workbook.
        st.session_state.tags = None
        st.session_state.detection_message = (
            f"No units were detected on selected pages: {', '.join(map(str, sorted(pages)))}. "
            "Check that these are floor-plan pages and that the selected detection mode matches "
            "the labels on the drawings. Try 'Match my list of unit names' if needed."
        )

if st.session_state.get("detection_message"):
    if st.session_state.get("tags") is None:
        st.warning(st.session_state.detection_message)
    else:
        st.success(st.session_state.detection_message)

if st.session_state.get("tags") is None:
    st.stop()

# 2. Review
st.subheader("2. Review units")
st.caption("Fix names, delete false hits (select rows → delete), or add missed units at the bottom of the table.")
left, right = st.columns([2, 3])
with left:
    edited = st.data_editor(
        st.session_state.tags, key=f"editor_{file_hash}", num_rows="dynamic", use_container_width=True,
        height=520, column_order=["page", "unit", "floor_level", "unit_no", "matched_text"],
        column_config={
            "page": st.column_config.NumberColumn("Page", min_value=1, max_value=page_count, step=1, required=True),
            "unit": st.column_config.TextColumn("Unit", required=True),
            "floor_level": st.column_config.TextColumn("Floor / Level"),
            "unit_no": st.column_config.TextColumn("Unit no.", disabled=True),
            "matched_text": st.column_config.TextColumn("Text on plan", disabled=True),
        },
    )
tags = clean(edited)
with right:
    tag_pages = sorted({t["page"] for t in tags}) or sorted(pages)
    shown = st.selectbox("Preview page", tag_pages, format_func=page_label)
    st.image(preview(page_image(file_hash, pdf, shown), [t for t in tags if t["page"] == shown]),
             use_container_width=True)

# 3. Unit plans -> merged pivot
st.subheader("3. Unit plans (merged pivot)")
st.caption("Each unit plan sheet draws one enlarged plan per unit type: its title sits underneath "
           "(“UNIT MI1.2A”) and the greyed room tag inside it names the floor-plan label it was drawn "
           "from. Pairing the two says which label bills against which unit plan. A label with no unit plan "
           "drawn for it bills against itself.")
summary = summarise(tags)
plan_pages = st.multiselect(
    "Unit plan sheets", range(1, page_count + 1),
    default=[p for p in unit_plan_sheets(file_hash, pdf)], format_func=page_label,
    help="Sheets whose drawings are titled UNIT <code>. Pre-selected from the title blocks.")

derived = unit_plans_from(file_hash, pdf, tuple(sorted(plan_pages)),
                          tuple(u for u, _ in summary)) if plan_pages else {}
st.caption(f"Read {len(derived)} label→unit-plan pairs off {len(plan_pages)} sheet(s). "
           "Correct any row below; blank means the label bills against itself.")

map_df = st.data_editor(
    pd.DataFrame([{"Label": u, "UNIT PLAN": derived.get(u, {}).get("plan", ""),
                   "from sheet": derived.get(u, {}).get("page", "")} for u, _ in summary]),
    key=f"plan_map_{file_hash}_{tuple(sorted(plan_pages))}", use_container_width=True, height=300,
    column_config={
        "Label": st.column_config.TextColumn("Label", disabled=True),
        "UNIT PLAN": st.column_config.TextColumn("UNIT PLAN"),
        "from sheet": st.column_config.NumberColumn("from sheet", disabled=True),
    },
)
plan_map = {str(r["Label"]): str(r["UNIT PLAN"] or "").strip()
            for r in map_df.to_dict("records") if str(r.get("Label") or "").strip()}
pivot_rows = merged_rows(summary, plan_map)
st.dataframe(pd.DataFrame(pivot_rows + [{"Label": "GRAND TOTAL OF UNITS", "UNIT PLAN": "",
                                         "Total": sum(r["Total"] for r in pivot_rows),
                                         "MERG QTY": sum(r["MERG QTY"] for r in pivot_rows
                                                         if r["MERG QTY"] != "")}]),
             hide_index=True, use_container_width=True)

# 4. Benchmark table
st.subheader("4. The benchmark count table")
st.caption("Pick the sheet holding the benchmark unit table, if the set has one (often a cover or general sheet; mechanical-only sets usually have none). "
           "Correct anything it misreads, or type the table in yourself.")
sched_col, pick_col = st.columns([1, 2])
with sched_col:
    sched_page = st.selectbox("Sheet with the table", range(1, page_count + 1),
                              format_func=page_label, key=f"sched_page_{file_hash}")
found = schedules_on(file_hash, pdf, sched_page)
with pick_col:
    if found:
        choice = st.radio(
            "Table on this sheet", range(len(found)), horizontal=True,
            format_func=lambda i: f"{found[i]['title']} — {found[i]['total']} units, {len(found[i]['rows'])} rows",
            key=f"sched_choice_{file_hash}_{sched_page}")
    else:
        choice = None
        st.info("No unit table recognised on this sheet. Choose another sheet, or type the rows in below.")

parsed = found[choice]["rows"] if found and choice is not None else []
schedule_df = st.data_editor(
    pd.DataFrame(parsed or [{"unit": "", "level": "", "qty": 0}]),
    key=f"sched_editor_{file_hash}_{sched_page}_{choice}", num_rows="dynamic",
    use_container_width=True, height=240,
    column_config={
        "unit": st.column_config.TextColumn("Unit", required=True),
        "level": st.column_config.TextColumn("Level", help="Leave blank if the table gives no levels"),
        "qty": st.column_config.NumberColumn("Architect qty", min_value=0, step=1),
    },
)
schedule_rows = [
    {"unit": str(r["unit"]).strip(), "level": str(r.get("level") or "").strip(), "qty": int(r["qty"] or 0)}
    for r in schedule_df.to_dict("records")
    if str(r.get("unit") or "").strip() and int(r.get("qty") or 0) > 0
]

# 4. Comparison + export
st.subheader("5. Comparison and export")
result = compare(schedule_rows, tags) if schedule_rows else None

if result:
    gap = result["counted_total"] - result["architect_total"]
    headline = (f"Architect {result['architect_total']} · counted {result['counted_total']} · "
                f"variance {gap:+d}")
    mismatched = [r for r in result["by_unit"] if r["CHECK"] != "OK"]
    if gap == 0 and not mismatched:
        st.success(headline + " — every unit type matches.")
    else:
        st.warning(headline + f" — {len(mismatched)} unit type(s) differ.")
    left_tab, right_tab = st.tabs(["By unit type", "By unit and level"])
    with left_tab:
        st.dataframe(pd.DataFrame(result["by_unit"]), hide_index=True, use_container_width=True)
    with right_tab:
        if result["by_level"]:
            st.dataframe(pd.DataFrame(result["by_level"]), hide_index=True, use_container_width=True)
        else:
            st.caption("The architect's table gives no levels, so there is nothing to compare per level.")
else:
    st.caption("Fill in the architect's table above to see the comparison.")

by_floor: dict[str, int] = {}
for t in sorted(tags, key=lambda t: t["page"]):
    floor = str(t.get("floor_level") or "").strip() or "Not identified"
    by_floor[floor] = by_floor.get(floor, 0) + 1
c1, c2, c3 = st.columns([1, 1, 1])
with c1:
    st.caption("Our count by unit type")
    st.dataframe(pd.DataFrame(summary + [("Grand Total", sum(q for _, q in summary))],
                              columns=["UNIT", "Total"]), hide_index=True, use_container_width=True)
with c2:
    st.caption("Our count by floor")
    st.dataframe(pd.DataFrame(list(by_floor.items()), columns=["FLOOR / LEVEL", "Total"]),
                 hide_index=True, use_container_width=True)
with c3:
    stem = Path(uploaded.name).stem
    st.download_button("Download Excel", build_xlsx(tags, result, plan_map),
                       file_name=f"{stem}_UNIT_MATRIX.xlsx", type="primary",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if st.button("Build tagged PDF"):
        with st.spinner("Adding Bluebeam count markups…"):
            st.session_state.tagged_pdf = build_pdf(pdf, tags)
    if st.session_state.get("tagged_pdf"):
        st.download_button("Download tagged PDF", st.session_state.tagged_pdf,
                           file_name=f"{stem}_tagged.pdf", mime="application/pdf")
    st.caption("PDF tags are Polygon markups with subject FLOOR UNIT and contents “<unit>⏎1”, the same "
               "as a manual Bluebeam count, so Bluebeam's markup summary gives the same totals.")
