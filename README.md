# Unit Matrix — Review build

A separate, cut-down build of the Unit Matrix tool. The main app in `../code` is untouched.

What it does, and nothing more:

1. Upload the drawing set.
2. Tick the floor-plan pages (thumbnail grid).
3. Count the unit tags on them.
4. **Walk through every tag.** Enter keeps it and moves to the next; the cross deletes a
   wrong one; the name can be corrected in place.
5. **Add anything missed** by clicking straight onto the page.
6. Export the Excel and a tagged PDF whose markup *Subject* is the tag name, so Bluebeam's
   Markups List identifies each count.

There is no benchmark table, no unit-plan mapping and no name resolution here by design.
A tag with no printed type code falls back to its unit number (MA-501), matching the
manager's checked takeoff; only a tag with neither shows as `?`.

## Run

    cd "C:\Users\Purplle\Downloads\Unit Matrix Jobs\code-annotate"
    ..\code\.venv\Scripts\Activate.ps1
    streamlit run app.py

`streamlit-image-coordinates` is needed for click-to-add. Without it the app still runs and
offers a typed X/Y instead.
