# Unit Matrix

Streamlit app that counts dwelling units on HVAC/architectural floor plans and exports:

- **Excel** in the unit-matrix format: `UNIT_MATRIX` (UNIT | Total + Grand Total), `TO` (pivot, "Sum of QTY"), `DATA` (one row per unit: UNIT | QTY | Count), `UNIT FLOOR COUNT` (UNIT | Total | FLOOR / LEVEL).
- **Tagged PDF** with one Bluebeam count markup per unit (Polygon, subject `FLOOR UNIT`, contents `<unit>\r1`), so Bluebeam's markup summary gives the same totals.

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

You choose the pages: nothing is pre-selected or filtered out, so the counts come only from the sheets you tick. The picker shows each sheet's title and how many unit labels it holds; leave out unit-plan, enlarged-plan, RCP and schedule sheets so each unit is counted once. The floor comes from the sheet title (the largest text naming a level) and can be edited in the review table. Image-only pages fall back to Tesseract OCR.

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
core/detect.py    unit-label detection, page suggestions, OCR fallback
core/export.py    Excel (UNIT_MATRIX / TO / DATA / UNIT FLOOR COUNT) and Bluebeam markup PDF
```
