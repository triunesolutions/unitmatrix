from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pymupdf as fitz
import streamlit as st
from PIL import Image, ImageDraw

from core.detect import detect_names, detect_unit_types, page_title, suggest_plan_pages
from core.export import build_pdf, build_xlsx, summarise

st.set_page_config(page_title="Unit Matrix", page_icon="🏢", layout="wide")

PREVIEW_ZOOM = 0.6
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
    st.session_state.update(file_hash=file_hash, tags=None)

with st.spinner("Reading the PDF and looking for floor-plan unit labels…"):
    page_count, suggested, titles = scan(file_hash, pdf)

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
    if titles.get(pn):
        bits.append(titles[pn])
    if pn in suggested:
        bits.append(f"{suggested[pn]} unit labels found")
    return " · ".join(bits)


st.caption("Tick the floor-plan sheets to count. Leave out unit-plan, enlarged-plan, RCP and schedule sheets "
           "so each unit is counted once.")
pages = st.multiselect("Pages to scan", range(1, page_count + 1), default=[], format_func=page_label)

if st.button("Detect units", type="primary", disabled=not pages or (mode == "names" and not names)):
    st.session_state.tags = pd.DataFrame(
        detect(pdf, file_hash, sorted(pages), mode, tuple(names)), columns=COLUMNS)

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

# 3. Unit matrix + export
st.subheader("3. Unit matrix")
summary = summarise(tags)
matrix = pd.DataFrame(summary + [("Grand Total", sum(q for _, q in summary))], columns=["UNIT", "Total"])
by_floor = {}
for t in sorted(tags, key=lambda t: t["page"]):
    floor = str(t.get("floor_level") or "").strip() or "Not identified"
    by_floor[floor] = by_floor.get(floor, 0) + 1
c1, c2, c3 = st.columns([1, 1, 1])
with c1:
    st.dataframe(matrix, hide_index=True, use_container_width=True)
with c2:
    st.dataframe(pd.DataFrame(list(by_floor.items()), columns=["FLOOR / LEVEL", "Total"]),
                 hide_index=True, use_container_width=True)
with c3:
    stem = Path(uploaded.name).stem
    st.download_button("Download Excel", build_xlsx(tags),
                       file_name=f"{stem}_UNIT_MATRIX.xlsx", type="primary",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if st.button("Build tagged PDF"):
        with st.spinner("Adding Bluebeam count markups…"):
            st.session_state.tagged_pdf = build_pdf(pdf, tags)
    if st.session_state.get("tagged_pdf"):
        st.download_button("Download tagged PDF", st.session_state.tagged_pdf,
                           file_name=f"{stem}_tagged.pdf", mime="application/pdf")
    st.caption("PDF tags are Polygon markups with subject FLOOR UNIT and contents “<unit>⏎1”, the same as a "
               "manual Bluebeam count, so Bluebeam's markup summary gives the same totals.")
