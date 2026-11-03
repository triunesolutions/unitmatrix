# Unit Matrix

Streamlit app that counts dwelling units on the mechanical sheets of a drawing set, compares the
count against the architect's own unit table, and exports:

- **Excel**: `COMPARISON` (UNIT | ARCHITECT | COUNTED | VARIANCE | CHECK) and `COMPARISON BY LEVEL`,
  plus the unit-matrix sheets `UNIT_MATRIX` (UNIT | Total + Grand Total), `TO` (pivot, "Sum of QTY"),
  `DATA` (one row per unit) and `UNIT FLOOR COUNT` (UNIT | Total | FLOOR / LEVEL).
- **Tagged PDF** with one Bluebeam count markup per unit (Polygon, subject `FLOOR UNIT`, contents `<unit>\r1`), so Bluebeam's markup summary gives the same totals.

## Workflow

1. **Pick the mechanical sheets.** The sheet number is read from each title block, so the page list
   filters to the mechanical disciplines — `M…` and `H…` (an HVAC set such as Emma's Court numbers its
   mechanical sheets `H0.01`–`H3.01B` and has no `M` sheets at all). Nothing is ticked for you.
   Where overall and enlarged plans cover the same units, tick only one family, and leave out roof,
   single-unit, RCP and schedule sheets, so each unit is counted once.
2. **Detect and review** the units on those sheets.
3. **Point at the architect's table** — the cover or a general sheet. The tool reads the unit names and
   quantities and you correct anything it misreads, or type the table in yourself.
4. **Compare.** Variance per unit type, and per unit x level where the architect's table gives levels.

## How units are found

**Auto** reads unit labels from the plans' text layer. A line that is exactly `UNIT TYPE 2C`, `UNIT A1.0` or `Unit 2-0` is one unit. For `UNIT TYPE` labels, the bedroom line in the same room tag (`3BD`) is added: `2C-3BED`.

```
3BD             <- bedroom count
1035 SF
2101            <- unit number (shown for review)
UNIT TYPE 2C    <- one unit, named 2C-3BED
MAAB GROUP 1
```

**Match my list of unit names** is for jobs whose plans don't use `UNIT …` labels: type the names (e.g. `A1`, `A2 END`) and every line that reads exactly that name is tagged.

The floor comes from the sheet title (the largest text naming a level) and can be edited in the review
table. The architect's table is read from cell geometry and handles the three layouts seen so far:
level-grouped rows (`LEVEL 1` then name/count rows), a wide matrix (one row per unit, `L1 QTY`…`L7 QTY`,
`TOTAL`), and a flat name/count list. Several tables on one sheet are offered as separate choices. Image-only pages fall back to Tesseract OCR.

## Run locally

Needs 64-bit Python 3.10+ (Streamlit has no 32-bit Windows build) and, for scanned pages, Tesseract on PATH.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## Deploy (Streamlit Community Cloud)

1. Push this folder to a GitHub repo.
2. https://share.streamlit.io → **Create app** → pick the repo, branch `main`, main file `app.py`.
3. `packages.txt` installs Tesseract; `.streamlit/config.toml` raises the upload limit to 300 MB.

## Layout

```
app.py            Streamlit UI (upload -> pages -> detect -> review -> export)
core/detect.py    unit-label detection, floor level, OCR fallback
core/sheets.py    sheet numbers from title blocks, discipline and series
core/schedule.py  parses the architect's unit table and compares it with our count
core/export.py    Excel (COMPARISON / UNIT_MATRIX / TO / DATA / UNIT FLOOR COUNT) and Bluebeam PDF
```
