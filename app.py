from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import pandas as pd
import pymupdf as fitz
import streamlit as st
from PIL import Image, ImageDraw

from core.detect import (detect_names, detect_unit_tags, detect_unit_types, page_title,
                         suggest_plan_pages)
from core.export import build_pdf, build_xlsx, summarise
from core.schedule import compare, find_schedules
from core.sheets import sheet_index
from core.unitplans import merged_rows, suggest_unit_plan_pages, unit_plan_map

st.set_page_config(page_title="Unit Matrix", page_icon="🏢", layout="wide")

PREVIEW_ZOOM = 0.6
THUMB_ZOOM = 0.16
THUMB_COLUMNS = 5
# One room tag is one unit; a tag found by its bedroom line and one found by its type code
# sit a line or two apart in the same stack, so anything this close is the same unit.
SAME_UNIT = 90.0
COLUMNS = ["page", "unit", "floor_level", "unit_no", "matched_text", "x", "y", "w", "h", "source"]


@st.cache_data(show_spinner=False, max_entries=4)
def scan(file_hash: str, _pdf: bytes) -> tuple[int, dict[int, int], dict[int, str]]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return len(doc), suggest_plan_pages(doc), {i: page_title(p) for i, p in enumerate(doc, start=1)}


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


@st.cache_data(show_spinner=False, max_entries=2)
def pages_with_tables(file_hash: str, _pdf: bytes) -> dict[int, list[str]]:
    """Every sheet carrying something that parses as a unit count table."""
    found = {}
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        for number, page in enumerate(doc, start=1):
            tables = find_schedules(page)
            if tables:
                found[number] = [f"{t['title']} ({t['total']})" for t in tables]
    return found


@st.cache_data(show_spinner=False, max_entries=256)
def detect_page(file_hash: str, _pdf: bytes, pn: int, mode: str, names: tuple[str, ...],
                include_unnamed: bool) -> list[dict]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        page = doc[pn - 1]
        found = detect_unit_types(page, pn) if mode == "auto" else detect_names(page, pn, list(names))
        if include_unnamed:
            known = list(names) or [t["unit"] for t in found]
            for tag in detect_unit_tags(page, pn, known):
                if not any(_same_unit(tag, other) for other in found):
                    found.append(tag)
        return found


def _same_unit(a: dict, b: dict) -> bool:
    return math.hypot(a["x"] + a["w"] / 2 - b["x"] - b["w"] / 2,
                      a["y"] + a["h"] / 2 - b["y"] - b["h"] / 2) < SAME_UNIT


def detect(pdf: bytes, file_hash: str, pages: list[int], mode: str, names: tuple[str, ...],
           include_unnamed: bool) -> list[dict]:
    tags = []
    progress = st.progress(0.0, text="Scanning pages…")
    for n, pn in enumerate(pages, start=1):
        tags += detect_page(file_hash, pdf, pn, mode, names, include_unnamed)
        progress.progress(n / len(pages), text=f"Scanned page {pn} ({n} of {len(pages)})")
    progress.empty()
    return tags


@st.cache_data(show_spinner=False, max_entries=512)
def thumbnail(file_hash: str, _pdf: bytes, pn: int) -> Image.Image:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        pix = doc[pn - 1].get_pixmap(matrix=fitz.Matrix(THUMB_ZOOM, THUMB_ZOOM), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


@st.cache_data(show_spinner=False, max_entries=16)
def page_image(file_hash: str, _pdf: bytes, pn: int) -> Image.Image:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        pix = doc[pn - 1].get_pixmap(matrix=fitz.Matrix(PREVIEW_ZOOM, PREVIEW_ZOOM), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def clean(df: pd.DataFrame) -> list[dict]:
    """Rows from the editor -> tags. A unit whose type code is not printed keeps a blank
    name, so it is still counted and still visible as one to name."""
    df = df.copy()
    df["unit"] = df["unit"].fillna("").astype(str).str.strip()
    df = df[df["page"].notna()]
    df = df.fillna({"x": 40.0, "y": 40.0, "w": 10.0, "h": 10.0, "floor_level": "",
                    "unit_no": "", "matched_text": "", "source": "manual"})
    df["page"] = df["page"].astype(int)
    return df.to_dict("records")


def preview(image: Image.Image, tags: list[dict]) -> Image.Image:
    image = image.copy()
    draw = ImageDraw.Draw(image)
    for t in tags:
        x0, y0 = t["x"] * PREVIEW_ZOOM, t["y"] * PREVIEW_ZOOM
        x1, y1 = (t["x"] + t["w"]) * PREVIEW_ZOOM, (t["y"] + t["h"]) * PREVIEW_ZOOM
        colour = (220, 0, 0) if str(t.get("unit") or "").strip() else (230, 140, 0)
        draw.rectangle((x0 - 3, y0 - 3, x1 + 3, y1 + 3), outline=colour, width=3)
        draw.text((x0, y0 - 14), str(t.get("unit") or "?"), fill=colour)
    return image


st.title("Unit Matrix")
st.caption("Read the benchmark table, count the units on the mechanical floor plans, map them to the "
           "unit plans, and export the matrix with a Bluebeam-countable PDF.")

uploaded = st.file_uploader("Upload the drawing set (PDF)", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

pdf = uploaded.getvalue()
file_hash = hashlib.md5(pdf).hexdigest()
if st.session_state.get("file_hash") != file_hash:
    st.session_state.update(file_hash=file_hash, tags=None, detection_message=None, tagged_pdf=None)

with st.spinner("Reading the PDF…"):
    page_count, suggested, titles = scan(file_hash, pdf)
    sheets = sheet_info(file_hash, pdf)

ALL = list(range(1, page_count + 1))


def page_label(pn: int) -> str:
    bits = [f"Page {pn}"]
    if sheets.get(pn, {}).get("sheet"):
        bits.append(sheets[pn]["sheet"])
    if titles.get(pn):
        bits.append(titles[pn])
    return " · ".join(bits)


def page_grid(prefix: str, pages: list[int], note=None) -> list[int]:
    """A tick box under a picture of every page. The boxes are the single source of truth,
    so the buttons are drawn first and set their state before the boxes exist this run."""
    def key(pn: int) -> str:
        return f"{prefix}_{file_hash}_{pn}"

    select_col, clear_col, thumbs_col, count_col = st.columns([1, 1, 1.2, 1.6])
    with select_col:
        select_all = st.button("Select all listed", key=f"{prefix}_all", use_container_width=True)
    with clear_col:
        clear_all = st.button("Clear selection", key=f"{prefix}_clear", use_container_width=True)
    with thumbs_col:
        show_thumbs = st.toggle("Show page images", value=True, key=f"{prefix}_thumbs")
    for pn in pages:
        if select_all:
            st.session_state[key(pn)] = True
        elif clear_all:
            st.session_state[key(pn)] = False
    with count_col:
        chosen = sum(1 for pn in pages if st.session_state.get(key(pn)))
        st.caption(f"**{chosen} of {len(pages)} listed pages selected**")

    columns_per_row = THUMB_COLUMNS if show_thumbs else 2
    with st.spinner("Rendering page images…" if show_thumbs else "Listing pages…"):
        for start in range(0, len(pages), columns_per_row):
            for column, pn in zip(st.columns(columns_per_row), pages[start:start + columns_per_row]):
                with column:
                    if show_thumbs:
                        st.image(thumbnail(file_hash, pdf, pn), use_container_width=True)
                        extra = note(pn) if note else ""
                        st.checkbox(f"**{pn}** · {sheets.get(pn, {}).get('sheet') or '—'}" + extra,
                                    key=key(pn), help=page_label(pn))
                    else:
                        st.checkbox(page_label(pn) + (note(pn) if note else ""), key=key(pn))
    return [pn for pn in pages if st.session_state.get(key(pn))]


# ---------------------------------------------------------------- 1. benchmark table
st.subheader("1. The benchmark count table")
st.caption("The table you are checking against. Mechanical-only sets usually carry none, so paste it "
           "from your own takeoff instead, or skip it and just count.")

source = st.radio("Where is it?",
                  ["Read it from a sheet", "Paste it in", "Skip — no benchmark table"],
                  horizontal=True, key=f"bench_source_{file_hash}")

bench_rows: list[dict] = []
if source == "Read it from a sheet":
    with st.expander("Which sheets have a unit table?"):
        if st.button("Scan every page for tables"):
            st.session_state.table_scan = pages_with_tables(file_hash, pdf)
        scanned = st.session_state.get("table_scan")
        if scanned:
            st.write({f"page {p}": v for p, v in scanned.items()})
        elif scanned == {}:
            st.info("No unit table was recognised on any page of this set. Paste it in instead.")

    pick_col, thumb_col = st.columns([2, 1])
    with pick_col:
        bench_page = st.selectbox("Sheet with the table", ALL, format_func=page_label,
                                  key=f"bench_page_{file_hash}")
        found = schedules_on(file_hash, pdf, bench_page)
        if found:
            choice = st.radio("Table on this sheet", range(len(found)),
                              format_func=lambda i: f"{found[i]['title']} — {found[i]['total']} units, "
                                                    f"{len(found[i]['rows'])} rows",
                              key=f"bench_choice_{file_hash}_{bench_page}")
        else:
            choice = None
            st.info("No unit table on this sheet. Pick another, or switch to “Paste it in”.")
    with thumb_col:
        st.image(thumbnail(file_hash, pdf, bench_page), use_container_width=True)

    parsed = found[choice]["rows"] if found and choice is not None else []
    bench_df = st.data_editor(
        pd.DataFrame(parsed or [{"unit": "", "level": "", "qty": 0}]),
        key=f"bench_editor_{file_hash}_{bench_page}_{choice}", num_rows="dynamic",
        use_container_width=True, height=240,
        column_config={
            "unit": st.column_config.TextColumn("Unit", required=True),
            "level": st.column_config.TextColumn("Level", help="Blank if the table gives no levels"),
            "qty": st.column_config.NumberColumn("Benchmark qty", min_value=0, step=1),
        },
    )
    bench_rows = [{"unit": str(r["unit"]).strip(), "level": str(r.get("level") or "").strip(),
                   "qty": int(r["qty"] or 0)}
                  for r in bench_df.to_dict("records")
                  if str(r.get("unit") or "").strip() and int(r.get("qty") or 0) > 0]

elif source == "Paste it in":
    st.caption("One unit per line: name then quantity, separated by a tab, comma or spaces. "
               "Paste straight from Excel. A middle column, if present, is read as the level.")
    pasted = st.text_area("Benchmark rows", height=220, key=f"bench_paste_{file_hash}",
                          placeholder="MA0.1\t2\nMA1.1\t1\nMA1.1A\t1")
    bad = 0
    for line in pasted.splitlines():
        raw = line.strip()
        if not raw:
            continue
        # Split on the clearest separator present, so a name with a space ("PLAN 1A")
        # survives: only fall back to the final run of spaces before the quantity.
        if "\t" in raw:
            parts = raw.split("\t")
        elif "," in raw:
            parts = raw.split(",")
        elif re.search(r"\s{2,}", raw):
            parts = re.split(r"\s{2,}", raw)
        else:
            trailing = re.match(r"^(.*\S)\s+(\d+)$", raw)
            parts = [trailing.group(1), trailing.group(2)] if trailing else [raw]
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 2:
            bad += 1
            continue
        qty = parts[-1].replace(",", "")
        if not qty.isdigit():
            bad += 1
            continue
        bench_rows.append({"unit": " ".join(parts[:-2]) if len(parts) > 2 else parts[0],
                           "level": parts[-2] if len(parts) > 2 else "",
                           "qty": int(qty)})
    if bad:
        st.warning(f"{bad} line(s) had no quantity and were ignored.")

bench_names = sorted({r["unit"] for r in bench_rows})
if bench_names:
    st.success(f"{len(bench_rows)} rows · {sum(r['qty'] for r in bench_rows)} units · "
               f"{len(bench_names)} unit names, used to seed the search below.")
elif source != "Skip — no benchmark table":
    st.caption("No rows yet — the comparison in step 5 needs them, but you can count without.")

# ---------------------------------------------------------------- 2. floor plan pages
st.subheader("2. Floor plan pages")
mode_label = st.radio("How are units labelled on the plans?",
                      ["Auto: unit labels on the plans (UNIT TYPE 2C + 3BD, UNIT A1.0, …)",
                       "Match my list of unit names"], horizontal=True)
mode = "auto" if mode_label.startswith("Auto") else "names"
names: list[str] = []
if mode == "names":
    names = [n.strip() for n in st.text_area(
        "Unit names to find, one per line, exactly as written on the plans",
        value="\n".join(bench_names), placeholder="A1\nA1 TYPE A\nA2 END",
        key=f"names_{file_hash}").splitlines() if n.strip()]

include_unnamed = st.checkbox(
    "Also count units whose type code is not printed", value=True,
    help="Some sheets tag a unit only as “2 BR / MA-501”. These are counted and left blank "
         "for you to name in the review table.")

mechanical = [p for p in ALL if sheets.get(p, {}).get("mechanical")]
only_mech = st.checkbox(
    f"Show mechanical sheets only ({len(mechanical)} of {page_count}: "
    f"{', '.join(sorted({sheets[p]['discipline'] for p in mechanical})) or 'none found'})",
    value=bool(mechanical), help="Mechanical and HVAC sheet numbers (M…, H…) from each title block.")
st.caption("Tick only the floor plans to count. Where overall and enlarged plans cover the same units "
           "take one family, and leave out roof, single-unit, RCP and schedule sheets.")

floor_pages = page_grid("floor", mechanical if (only_mech and mechanical) else ALL,
                        note=lambda pn: f" · {suggested[pn]} labels" if pn in suggested else "")
if floor_pages:
    st.caption("Selected: " + ", ".join(str(p) for p in floor_pages))

if st.button("Count units", type="primary",
             disabled=not floor_pages or (mode == "names" and not names)):
    detected = detect(pdf, file_hash, sorted(floor_pages), mode, tuple(names), include_unnamed)
    if detected:
        st.session_state.tags = pd.DataFrame(detected, columns=COLUMNS)
        blank = sum(1 for t in detected if not str(t.get("unit") or "").strip())
        st.session_state.detection_message = (
            f"Counted {len(detected)} units across {len(floor_pages)} sheet(s)."
            + (f" {blank} have no type code printed and need naming." if blank else ""))
    else:
        st.session_state.tags = None
        st.session_state.detection_message = (
            f"No units found on pages {', '.join(map(str, sorted(floor_pages)))}. Check these are floor "
            "plans and that the mode matches how the drawings label units.")

if st.session_state.get("detection_message"):
    (st.warning if st.session_state.get("tags") is None else st.success)(st.session_state.detection_message)
if st.session_state.get("tags") is None:
    st.stop()

# ---------------------------------------------------------------- 3. review
st.subheader("3. Review units")
st.caption("Name any blank unit, fix names, delete false hits (select rows → delete), or add missed "
           "units at the bottom. Orange boxes on the preview are units with no type code printed.")
left, right = st.columns([2, 3])
with left:
    edited = st.data_editor(
        st.session_state.tags, key=f"editor_{file_hash}", num_rows="dynamic", use_container_width=True,
        height=520, column_order=["page", "unit", "unit_no", "floor_level", "matched_text"],
        column_config={
            "page": st.column_config.NumberColumn("Page", min_value=1, max_value=page_count, step=1, required=True),
            "unit": st.column_config.TextColumn("Unit"),
            "unit_no": st.column_config.TextColumn("Unit no.", disabled=True),
            "floor_level": st.column_config.TextColumn("Floor / Level"),
            "matched_text": st.column_config.TextColumn("Text on plan", disabled=True),
        },
    )
tags = clean(edited)
named = [t for t in tags if str(t.get("unit") or "").strip()]
with right:
    tag_pages = sorted({t["page"] for t in tags}) or sorted(floor_pages)
    shown = st.selectbox("Preview page", tag_pages, format_func=page_label)
    st.image(preview(page_image(file_hash, pdf, shown), [t for t in tags if t["page"] == shown]),
             use_container_width=True)
blank = len(tags) - len(named)
if blank:
    st.warning(f"{blank} unit(s) still have no name. They count towards the total but cannot be "
               "matched to the benchmark or to a unit plan until you name them.")

# ---------------------------------------------------------------- 4. unit plans
st.subheader("4. Unit plan sheets (merged pivot)")
st.caption("A unit plan sheet draws one enlarged plan per unit type: the title sits underneath "
           "(“UNIT MI1.2A”) and the greyed room tag inside names the floor-plan label it was drawn "
           "from. Pairing the two says which label bills against which unit plan. A label the sheets do "
           "not name takes the plan drawn for the same base unit; one with none bills against itself.")
summary = summarise(named)
plan_suggested = unit_plan_sheets(file_hash, pdf)
plan_pages = page_grid("uplan", ALL, note=lambda pn: f" · {plan_suggested[pn]} plans"
                       if pn in plan_suggested else "")

derived = unit_plans_from(file_hash, pdf, tuple(sorted(plan_pages)),
                          tuple(u for u, _ in summary)) if plan_pages else {}
st.caption(f"Read {len(derived)} label→unit-plan pairs off {len(plan_pages)} sheet(s). "
           "Correct any row; blank means the label bills against itself.")

map_df = st.data_editor(
    pd.DataFrame([{"Label": u, "UNIT PLAN": derived.get(u, {}).get("plan", ""),
                   "read from": derived.get(u, {}).get("source", ""),
                   "sheet": derived.get(u, {}).get("page", "")} for u, _ in summary]),
    key=f"plan_map_{file_hash}_{tuple(sorted(plan_pages))}", use_container_width=True, height=300,
    column_config={
        "Label": st.column_config.TextColumn("Label", disabled=True),
        "UNIT PLAN": st.column_config.TextColumn("UNIT PLAN"),
        "read from": st.column_config.TextColumn("read from", disabled=True),
        "sheet": st.column_config.NumberColumn("sheet", disabled=True),
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

# ---------------------------------------------------------------- 5. comparison + export
st.subheader("5. Comparison and export")
result = compare(bench_rows, named) if bench_rows else None

if result:
    gap = result["counted_total"] - result["architect_total"]
    headline = (f"Benchmark {result['architect_total']} · counted {result['counted_total']}"
                + (f" (+{blank} unnamed)" if blank else "") + f" · variance {gap:+d}")
    mismatched = [r for r in result["by_unit"] if r["CHECK"] != "OK"]
    if gap == 0 and not mismatched:
        st.success(headline + " — every unit type matches.")
    else:
        st.warning(headline + f" — {len(mismatched)} unit type(s) differ.")
    by_type, by_level = st.tabs(["By unit type", "By unit and level"])
    with by_type:
        st.dataframe(pd.DataFrame(result["by_unit"]), hide_index=True, use_container_width=True)
    with by_level:
        if result["by_level"]:
            st.dataframe(pd.DataFrame(result["by_level"]), hide_index=True, use_container_width=True)
        else:
            st.caption("The benchmark table gives no levels, so there is nothing to compare per level.")
else:
    st.caption("Fill in the benchmark table in step 1 to see the comparison.")

by_floor: dict[str, int] = {}
for t in sorted(tags, key=lambda t: t["page"]):
    by_floor[str(t.get("floor_level") or "").strip() or "Not identified"] = \
        by_floor.get(str(t.get("floor_level") or "").strip() or "Not identified", 0) + 1
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
    st.download_button("Download Excel", build_xlsx(named, result, plan_map),
                       file_name=f"{stem}_UNIT_MATRIX.xlsx", type="primary",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if st.button("Build tagged PDF"):
        with st.spinner("Adding Bluebeam count markups…"):
            st.session_state.tagged_pdf = build_pdf(pdf, named)
    if st.session_state.get("tagged_pdf"):
        st.download_button("Download tagged PDF", st.session_state.tagged_pdf,
                           file_name=f"{stem}_tagged.pdf", mime="application/pdf")
    st.caption("PDF tags are Polygon markups with subject FLOOR UNIT and contents “<unit>⏎1”, the same "
               "as a manual Bluebeam count, so Bluebeam's markup summary gives the same totals.")
