"""Unit Matrix — Review build.

Upload a drawing set, pick the floor-plan sheets, count the unit tags, then walk the
manager through every detection one key-press at a time: Enter keeps it, the cross
deletes it. Anything the program missed is added by clicking straight onto the sheet.

Speed notes: the page picker is rendered only until the count is run, and the review and
viewer are fragments, so a key-press reruns one block instead of the whole script.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pandas as pd
import pymupdf as fitz
import streamlit as st
from PIL import Image, ImageDraw

import streamlit.components.v1 as components

from core.detect import detect_unit_tags, detect_unit_types, page_title
from core.export import build_pdf, build_xlsx, label_of, summarise
from core.match import merged_rows, parents
from core.sheets import sheet_index
from core.unitlabels import detect_unit_labels, suggest_unit_label_pages

# Drag-and-drop matcher. A plain HTML/JS component served from the project, so it needs
# no extra package: it speaks Streamlit's postMessage protocol directly.
matcher = components.declare_component(
    "unit_plan_matcher", path=str(Path(__file__).parent / "components" / "matcher"))

try:
    from streamlit_image_coordinates import streamlit_image_coordinates as image_click
except ImportError:  # click-to-place needs this component
    image_click = None

st.set_page_config(page_title="Unit Matrix — Review", page_icon="✅", layout="wide")

THUMB_ZOOM = 0.16
THUMB_COLUMNS = 5
# The navigator is sized to a fixed width, whatever the sheet: these are 42"x30" drawings,
# so a fixed zoom would give a 600px-wide "thumbnail".
NAV_WIDTH = 230.0
# The review crop: how much of the sheet to show around the tag, in PDF points.
WINDOW = 260.0
TAG_BOX = 26.0
# The viewer pane, in screen pixels. Zoom changes how much of the sheet fits in it.
VIEW_W, VIEW_H = 980, 660


# ----------------------------------------------------------------- rendering helpers
@st.cache_data(show_spinner=False, max_entries=512)
def thumbnail(file_hash: str, _pdf: bytes, pn: int, zoom: float = THUMB_ZOOM) -> Image.Image:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        pix = doc[pn - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


@st.cache_data(show_spinner=False, max_entries=8)
def scan(file_hash: str, _pdf: bytes) -> tuple[int, dict[int, str], dict[int, dict],
                                               dict[int, tuple[float, float]]]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return (len(doc),
                {i: page_title(p) for i, p in enumerate(doc, start=1)},
                sheet_index(doc),
                {i: (p.rect.width, p.rect.height) for i, p in enumerate(doc, start=1)})


# One room tag is one unit; a tag found by its bedroom line and one found by its
# "UNIT TYPE" label sit a line or two apart in the same stack, so anything this close is
# the same unit and must not be counted twice.
SAME_UNIT = 90.0


def count_page(page: fitz.Page, pn: int) -> list[dict]:
    """Every dwelling tag on one sheet, from both label styles.

    Some sets stack a bedroom count in the room tag ("MA1.2 / 470 SF / 1 BED"); others
    print a plain label ("UNIT TYPE A3a") and no bedroom line at all. Running only the
    first found nothing on a set of the second kind, so both run and the results merge.
    """
    found = detect_unit_types(page, pn)
    for tag in detect_unit_tags(page, pn, [t["unit"] for t in found]):
        cx, cy = tag["x"] + tag["w"] / 2, tag["y"] + tag["h"] / 2
        if not any(math.hypot(o["x"] + o["w"] / 2 - cx, o["y"] + o["h"] / 2 - cy) < SAME_UNIT
                   for o in found):
            found.append(tag)
    return found


@st.cache_data(show_spinner=False, max_entries=512)
def tag_counts(file_hash: str, _pdf: bytes, pn: int) -> int:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return len(count_page(doc[pn - 1], pn))


@st.cache_data(show_spinner=False, max_entries=8)
def rotations(file_hash: str, _pdf: bytes) -> dict[int, tuple[tuple, tuple]]:
    """Per page, the matrices between PDF space and what the viewer sees.

    These sheets are saved rotated (/Rotate 270). Text is extracted in the page's own
    unrotated space, while the rendered picture is upright, so a tag read at y=2366 on a
    portrait 2160x3024 page has to be mapped before it can be drawn on a landscape
    3024x2160 image — without it the boxes land in the wrong place, and a crop near the
    far edge falls off the page entirely and renders as an empty image.
    """
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return {i: (tuple(p.rotation_matrix), tuple(p.derotation_matrix))
                for i, p in enumerate(doc, start=1)}


def to_view(rot: dict, tag: dict) -> dict:
    """The tag with its box mapped into the coordinates of the rendered page."""
    forward = fitz.Matrix(*rot[int(tag["page"])][0])
    box = (fitz.Rect(float(tag["x"]), float(tag["y"]),
                     float(tag["x"]) + float(tag["w"]),
                     float(tag["y"]) + float(tag["h"])) * forward).normalize()
    return dict(tag, x=box.x0, y=box.y0, w=box.width, h=box.height)


def from_view(rot: dict, pn: int, x: float, y: float) -> tuple[float, float]:
    """A point clicked on the rendered page, back in the coordinates tags are stored in."""
    point = fitz.Point(x, y) * fitz.Matrix(*rot[pn][1])
    return point.x, point.y


@st.cache_data(show_spinner=False, max_entries=256)
def crop(file_hash: str, _pdf: bytes, pn: int, cx: float, cy: float,
         width: float, height: float, zoom: float) -> tuple[Image.Image, tuple[float, float]]:
    """A window of the sheet centred on (cx, cy), and the PDF point of its top-left corner."""
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        page = doc[pn - 1]
        clip = fitz.Rect(cx - width / 2, cy - height / 2,
                         cx + width / 2, cy + height / 2) & page.rect
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), (clip.x0, clip.y0)


def draw_tags(image: Image.Image, tags: list[dict], zoom: float,
              origin: tuple[float, float] = (0.0, 0.0), highlight: int | None = None) -> Image.Image:
    """Mark tags on a rendered image. `origin` is the PDF point of its top-left corner."""
    image = image.copy()
    pen = ImageDraw.Draw(image)
    ox, oy = origin
    for tag in tags:
        cx = (float(tag["x"]) + float(tag["w"]) / 2 - ox) * zoom
        cy = (float(tag["y"]) + float(tag["h"]) / 2 - oy) * zoom
        half = TAG_BOX / 2 * zoom
        current = highlight is not None and tag.get("_i") == highlight
        colour = (220, 0, 0) if current else (40, 120, 215)
        pen.rectangle((cx - half, cy - half, cx + half, cy + half),
                      outline=colour, width=4 if current else 2)
        pen.text((cx - half, cy - half - 12), label_of(tag), fill=colour)
    return image


def reindex(store: str = "tags") -> list[dict]:
    tags = st.session_state.get(store) or []
    for i, tag in enumerate(tags):
        tag["_i"] = i
    return tags


@st.cache_data(show_spinner=False, max_entries=8)
def unit_label_pages(file_hash: str, _pdf: bytes) -> dict[int, int]:
    with fitz.open(stream=_pdf, filetype="pdf") as doc:
        return suggest_unit_label_pages(doc)


def new_tag(page: int, x: float, y: float, name: str, floor: str) -> dict:
    return {"page": page, "unit": name, "unit_no": "", "matched_text": "added by hand",
            "x": x - TAG_BOX / 2, "y": y - TAG_BOX / 2, "w": TAG_BOX, "h": TAG_BOX,
            "source": "manual", "floor_level": floor}


def fresh_click(key: str, click) -> tuple[float, float] | None:
    """The click coordinates, but only the first time this exact click is seen.

    The component replays its last value on every rerun, so without this a single click
    would keep re-firing.
    """
    if not click:
        return None
    seen = st.session_state.setdefault("clicks", {})
    point = (click["x"], click["y"])
    if seen.get(key) == point:
        return None
    seen[key] = point
    return point


# ----------------------------------------------------------------- 0. upload
st.title("Unit Matrix — count and review")
st.caption("Count the unit tags on the floor plans, then check every one: Enter keeps it, the "
           "cross deletes it, and a click on the sheet adds one the program missed.")

uploaded = st.file_uploader("Upload the drawing set (PDF)", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

pdf = uploaded.getvalue()
file_hash = hashlib.md5(pdf).hexdigest()
if st.session_state.get("file_hash") != file_hash:
    st.session_state.update(file_hash=file_hash, tags=None, pages=None, cursor=0,
                            tagged_pdf=None, clicks={}, centre={}, pending=None,
                            pick_pages=True, units=None, unit_pages=None, units_cursor=0)

with st.spinner("Reading the PDF…"):
    page_count, titles, sheets, page_sizes = scan(file_hash, pdf)
    ROT = rotations(file_hash, pdf)
ALL = list(range(1, page_count + 1))


def page_label(pn: int) -> str:
    bits = [f"Page {pn}"]
    if sheets.get(pn, {}).get("sheet"):
        bits.append(sheets[pn]["sheet"])
    if titles.get(pn):
        bits.append(titles[pn])
    return " · ".join(bits)


def key_of(pn: int) -> str:
    return f"floor_{file_hash}_{pn}"


# ----------------------------------------------------------------- 1. pick the pages
# Rendered only while choosing. Once the count is in, the thumbnail grid is dropped from
# the script entirely — redrawing 60 page images on every key-press was the slowness.
st.subheader("1. Floor-plan pages")

if st.session_state.get("pick_pages", True):
    select_col, clear_col, thumb_col, count_col = st.columns([1, 1, 1.2, 1.8])
    select_all = select_col.button("Select all", use_container_width=True)
    clear_all = clear_col.button("Clear selection", use_container_width=True)
    show_thumbs = thumb_col.toggle("Show page images", value=True)
    for pn in ALL:
        if select_all:
            st.session_state[key_of(pn)] = True
        elif clear_all:
            st.session_state[key_of(pn)] = False
    chosen = [pn for pn in ALL if st.session_state.get(key_of(pn))]
    count_col.caption(f"**{len(chosen)} of {page_count} pages selected**")

    show_counts = st.toggle("Show how many tags each page holds (slower)", value=False)
    columns_per_row = THUMB_COLUMNS if show_thumbs else 2
    with st.spinner("Rendering pages…"):
        for start in range(0, len(ALL), columns_per_row):
            for column, pn in zip(st.columns(columns_per_row), ALL[start:start + columns_per_row]):
                with column:
                    note = f" · {tag_counts(file_hash, pdf, pn)} tags" if show_counts else ""
                    if show_thumbs:
                        st.image(thumbnail(file_hash, pdf, pn), use_container_width=True)
                        st.checkbox(f"**{pn}** · {sheets.get(pn, {}).get('sheet') or '—'}{note}",
                                    key=key_of(pn), help=page_label(pn))
                    else:
                        st.checkbox(page_label(pn) + note, key=key_of(pn))
    chosen = [pn for pn in ALL if st.session_state.get(key_of(pn))]

    if chosen and st.button(f"Count units on {len(chosen)} page(s)", type="primary"):
        found: list[dict] = []
        progress = st.progress(0.0, text="Scanning…")
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            for n, pn in enumerate(chosen, start=1):
                found += count_page(doc[pn - 1], pn)
                progress.progress(n / len(chosen), text=f"Page {pn} ({n} of {len(chosen)})")
        progress.empty()
        st.session_state.update(tags=found, pages=chosen, cursor=0,
                                tagged_pdf=None, pick_pages=False, pending=None)
        st.rerun()
    st.stop()

chosen = st.session_state.pages
summary_col, change_col = st.columns([4, 1])
summary_col.caption("Counting pages " + ", ".join(str(p) for p in chosen))
if change_col.button("Change pages", use_container_width=True):
    st.session_state.pick_pages = True
    st.rerun()

tags = reindex()
st.success(f"**{len(tags)} units** found across {len({t['page'] for t in tags})} page(s).")
st.caption(" · ".join(f"page {pn}: {sum(1 for t in tags if t['page'] == pn)}"
                      for pn in sorted({t["page"] for t in tags})))

# ----------------------------------------------------------------- 2. walkthrough
st.subheader("2. Check every tag")
st.caption("Enter keeps the tag and moves on. The cross deletes a wrong one. Correct the name "
           "in the box first if it is wrong.")


@st.fragment
def walkthrough(store: str = "tags", cursor_key: str = "cursor", word: str = "Tag") -> None:
    tags = reindex(store)
    if not tags:
        st.warning(f"No {word.lower()}s left.")
        return
    cursor = min(st.session_state.get(cursor_key, 0), len(tags) - 1)
    st.session_state[cursor_key] = cursor
    tag = tags[cursor]

    head, zoom_col = st.columns([3, 1])
    head.markdown(f"**{word} {cursor + 1} of {len(tags)}** — page {tag['page']} "
                  f"({sheets.get(tag['page'], {}).get('sheet') or '—'}) · "
                  f"{tag.get('floor_level') or 'level not identified'}")
    zoom = zoom_col.slider("Zoom", 1.0, 4.0, 2.0, 0.5, key=f"walk_zoom_{store}")
    st.progress((cursor + 1) / len(tags))

    # Everything drawn or cropped works in the coordinates of the rendered page.
    shown = to_view(ROT, tag)
    cx = shown["x"] + shown["w"] / 2
    cy = shown["y"] + shown["h"] / 2
    image, origin = crop(file_hash, pdf, tag["page"], cx, cy, WINDOW, WINDOW, zoom)
    nearby = [v for v in (to_view(ROT, t) for t in tags if t["page"] == tag["page"])
              if abs(v["x"] - cx) < WINDOW and abs(v["y"] - cy) < WINDOW]

    view, side = st.columns([2, 1])
    view.image(draw_tags(image, nearby, zoom, origin, highlight=cursor))
    with side:
        st.caption(f"Read from the sheet: `{tag.get('matched_text') or '—'}`")
        if tag.get("unit_no"):
            st.caption(f"Unit number: `{tag['unit_no']}`")
        with st.form(f"review_{store}_{cursor}", clear_on_submit=False, border=False):
            name = st.text_input(f"{word} name", value=label_of(tag),
                                 key=f"name_{store}_{cursor}")
            keep = st.form_submit_button("✔ Keep and next  (Enter)", type="primary",
                                         use_container_width=True)
            drop = st.form_submit_button(f"✖ Wrong — delete this {word.lower()}",
                                         use_container_width=True)
        back_col, end_col = st.columns(2)
        back = back_col.button("← Back", use_container_width=True, disabled=cursor == 0,
                               key=f"back_{store}")
        jump = end_col.button(f"Last {word.lower()}", use_container_width=True,
                              key=f"jump_{store}")

    # Render the next tag's window now, while the manager is still reading this one, so the
    # key-press finds it already in the cache.
    if cursor + 1 < len(tags):
        nxt = to_view(ROT, tags[cursor + 1])
        crop(file_hash, pdf, nxt["page"], nxt["x"] + nxt["w"] / 2, nxt["y"] + nxt["h"] / 2,
             WINDOW, WINDOW, zoom)

    if keep:
        clean = name.strip()
        tag["unit"] = "" if clean in ("", "?") else clean
        st.session_state[cursor_key] = min(cursor + 1, len(tags) - 1)
        st.rerun(scope="fragment")
    if drop:
        st.session_state[store] = [t for t in tags if t["_i"] != cursor]
        st.session_state[cursor_key] = min(cursor, max(len(st.session_state[store]) - 1, 0))
        st.rerun()
    if back:
        st.session_state[cursor_key] = cursor - 1
        st.rerun(scope="fragment")
    if jump:
        st.session_state[cursor_key] = len(tags) - 1
        st.rerun(scope="fragment")


walkthrough()

# ----------------------------------------------------------------- 3. add a missed tag
st.subheader("3. Add anything the program missed")

if image_click is None:
    st.error("Click-to-place needs one package that is not installed yet. "
             "`streamlit.exe` and `pip.exe` are blocked by Application Control on this "
             "laptop, so run it through Python instead:")
    st.code(r'..\code\.venv\Scripts\python.exe -m pip install streamlit-image-coordinates',
            language="powershell")
    st.caption("Then stop the app and start it again with "
               "`python -m streamlit run app.py`.")
else:
    st.caption("The small sheet on the left is the whole page — click it to move the view. "
               "The large pane is the sheet itself: click where a tag is missing, name it, "
               "press Enter. Zoom changes how much of the sheet the pane holds.")

    @st.fragment
    def viewer() -> None:
        # Unit-plan sheets are offered too, so a missed unit label can be clicked in the
        # same way. A click on one of those pages adds to the unit labels, not the units.
        label_pages = st.session_state.get("unit_pages") or []
        pages = list(dict.fromkeys(list(chosen) + list(label_pages)))
        pick, zoom_col = st.columns([3, 1])
        pn = pick.selectbox("Page", pages, format_func=page_label, key="add_page")
        zoom = zoom_col.slider("Zoom", 0.5, 6.0, 2.0, 0.5, key="add_zoom")
        store = "units" if pn in label_pages and pn not in chosen else "tags"
        tags = reindex(store)
        if store == "units":
            st.caption("This is a unit-plan sheet — anything added here joins the unit labels.")

        width, height = page_sizes[pn]
        centre = st.session_state.centre.get(pn) or (width / 2, height / 2)
        # How much of the sheet fits the pane at this zoom.
        span_w, span_h = VIEW_W / zoom, VIEW_H / zoom
        cx = min(max(centre[0], span_w / 2), max(width - span_w / 2, span_w / 2))
        cy = min(max(centre[1], span_h / 2), max(height - span_h / 2, span_h / 2))

        on_page = [t for t in tags if t["page"] == pn]
        # Drawn against the rendered page, so rotated sheets line up.
        shown_tags = [to_view(ROT, t) for t in on_page]
        nav_col, main_col = st.columns([1, 4])

        nav_zoom = NAV_WIDTH / width
        with nav_col:
            nav = thumbnail(file_hash, pdf, pn, nav_zoom).copy()
            pen = ImageDraw.Draw(nav)
            pen.rectangle(((cx - span_w / 2) * nav_zoom, (cy - span_h / 2) * nav_zoom,
                           (cx + span_w / 2) * nav_zoom, (cy + span_h / 2) * nav_zoom),
                          outline=(220, 0, 0), width=2)
            for tag in shown_tags:
                tx = (tag["x"] + tag["w"] / 2) * nav_zoom
                ty = (tag["y"] + tag["h"] / 2) * nav_zoom
                pen.ellipse((tx - 2, ty - 2, tx + 2, ty + 2), fill=(40, 120, 215))
            moved = fresh_click(f"nav_{pn}", image_click(nav, key=f"nav_{pn}_{zoom:.1f}"))
            st.caption("Click to move the view")

        if moved:
            st.session_state.centre[pn] = (moved[0] / nav_zoom, moved[1] / nav_zoom)
            st.rerun(scope="fragment")

        with main_col:
            image, origin = crop(file_hash, pdf, pn, cx, cy, span_w, span_h, zoom)
            placed = fresh_click(
                f"main_{pn}",
                image_click(draw_tags(image, shown_tags, zoom, origin), key=f"main_{pn}_{zoom}_{cx:.0f}_{cy:.0f}"))

        if placed:
            # The click is on the rendered page; store it where the tags live.
            px, py = from_view(ROT, pn, origin[0] + placed[0] / zoom,
                               origin[1] + placed[1] / zoom)
            st.session_state.pending = {"page": pn, "x": px, "y": py}
            st.rerun(scope="fragment")

        pending = st.session_state.get("pending")
        if pending and pending["page"] == pn:
            with st.form("place", clear_on_submit=True, border=False):
                label_col, add_col, cancel_col = st.columns([2, 1, 1])
                name = label_col.text_input(
                    f"Name the tag at x={pending['x']:.0f}, y={pending['y']:.0f}",
                    placeholder="MA1.2")
                add_col.markdown("<br>", unsafe_allow_html=True)
                cancel_col.markdown("<br>", unsafe_allow_html=True)
                add = add_col.form_submit_button("Add  (Enter)", type="primary",
                                                 use_container_width=True)
                cancel = cancel_col.form_submit_button("Cancel", use_container_width=True)
            if add and name.strip():
                floor = next((t.get("floor_level") for t in on_page), "")
                st.session_state[store] = tags + [
                    new_tag(pn, pending["x"], pending["y"], name.strip(), floor)]
                st.session_state.pending = None
                st.rerun()
            if cancel:
                st.session_state.pending = None
                st.rerun(scope="fragment")

        with st.expander(f"The {len(on_page)} tag(s) on page {pn}"):
            for tag in on_page:
                row, button = st.columns([5, 1])
                row.write(f"`{label_of(tag)}` · x={float(tag['x']):.0f}, "
                          f"y={float(tag['y']):.0f} · {tag.get('source')}")
                if button.button("Delete", key=f"del_{store}_{tag['_i']}"):
                    st.session_state[store] = [t for t in tags if t["_i"] != tag["_i"]]
                    st.rerun()

    viewer()

# ----------------------------------------------------------------- 4. unit labels
st.subheader("4. Unit labels on the unit-plan sheets")
st.caption("The name under each enlarged unit plan — “UNIT MI0.1”, “UNIT MI0.2” — found by the "
           "scale line printed beneath it. These get the same markup and their own Units tab.")

suggested = unit_label_pages(file_hash, pdf)
default = st.session_state.get("unit_pages") or sorted(suggested)
label_pages = st.multiselect(
    "Unit-plan sheets", ALL, default=[p for p in default if p in ALL], format_func=page_label,
    help="Suggested: " + (", ".join(f"page {p} ({n} plans)" for p, n in sorted(suggested.items()))
                          or "none recognised"))

if label_pages and st.button(f"Read unit labels from {len(label_pages)} page(s)"):
    found = []
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        for pn in label_pages:
            found += detect_unit_labels(doc[pn - 1], pn)
    st.session_state.update(units=found, unit_pages=label_pages, units_cursor=0, tagged_pdf=None)
    st.rerun()

units = reindex("units")
if units:
    st.success(f"**{len(units)} unit labels** across {len({u['page'] for u in units})} sheet(s).")
    st.caption("Same walkthrough as the floor-plan tags: Enter keeps the label and moves on, "
               "the cross deletes a wrong one.")
    walkthrough(store="units", cursor_key="units_cursor", word="Unit label")
    with st.expander(f"All {len(units)} unit labels"):
        st.dataframe(pd.DataFrame(sorted((label_of(u), u["page"]) for u in units),
                                  columns=["Unit", "Page"]),
                     hide_index=True, use_container_width=True, height=240)
elif st.session_state.get("units") is not None:
    st.warning("No unit labels were found on those sheets.")

# ----------------------------------------------------------------- 5. match
st.subheader("5. Put each floor tag under its unit plan")

tags = st.session_state.tags
units = st.session_state.get("units") or []
summary = summarise(tags)
merge_table: list[dict] | None = None

if not units:
    st.info("Read the unit labels in step 4 to group the counted floor tags under them.")
else:
    st.caption("The unit plan is the parent; one plan can carry several floor tags. A floor tag "
               "starts under a plan only when the two names are exactly the same — nothing is "
               "guessed. Drag the rest from the panel onto the plan they belong to; drag one "
               "back to take it out. A tag you place by hand shows red.")
    unit_cards, pool = parents(summary, [label_of(u) for u in units])
    answer = matcher(units=unit_cards, pool=pool, key="matcher", default=None)
    if answer:
        st.session_state.match = answer

    state = st.session_state.get("match") or {
        "assigned": {u["unit"]: [k["tag"] for k in u["kids"]] for u in unit_cards},
        "manual": [], "unassigned": [p["tag"] for p in pool]}
    merge_table = merged_rows(summary, state.get("assigned", {}), state.get("manual", []))
    unplaced = sum(dict(summary).get(t, 0) for t in state.get("unassigned", []))
    st.caption(f"**{sum(q for _, q in summary) - unplaced} of {sum(q for _, q in summary)}** "
               f"units placed under a plan · "
               f"**{sum(1 for r in merge_table if r['manual'])}** floor tags placed by hand"
               + (f" · **{unplaced} units still unplaced**" if unplaced else ""))

# ----------------------------------------------------------------- 6. export
st.subheader("6. Export")
left, right = st.columns([2, 1])
with left:
    st.dataframe(pd.DataFrame(summary, columns=["Label", "Qty"]),
                 hide_index=True, use_container_width=True, height=320)
    st.caption(f"**{len(tags)} units**, {len(summary)} distinct labels."
               + (f" Plus **{len(units)} unit labels** on the unit-plan sheets." if units else ""))
with right:
    stem = Path(uploaded.name).stem
    sheet_numbers = {pn: (info.get("sheet") or "") for pn, info in sheets.items()}
    st.download_button("Download Excel",
                       build_xlsx(tags, units, merge_table, sheet_numbers), type="primary",
                       file_name=f"{stem}_UNIT_MATRIX.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if st.button("Build tagged PDF"):
        with st.spinner("Adding count markups…"):
            st.session_state.tagged_pdf = build_pdf(pdf, tags, units)
    if st.session_state.get("tagged_pdf"):
        st.download_button("Download tagged PDF", st.session_state.tagged_pdf,
                           file_name=f"{stem}_tagged.pdf", mime="application/pdf")
    st.caption("Each markup is a red Polygon whose Subject is the tag name, so Bluebeam's "
               "Markups List names every count instead of repeating “FLOOR UNIT”.")
