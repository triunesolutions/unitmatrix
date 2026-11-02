from __future__ import annotations

import io
import math
import re
from difflib import SequenceMatcher
from typing import Any

import pymupdf as fitz
import pytesseract
from PIL import Image

# Floor-plan room tags are stacked text, e.g.
#   3BD / 1035 SF / 2101 / UNIT TYPE 2C / MAAB GROUP 1
# One label line = one unit; the bedroom line in the same stack names it "2C-3BED".
# The whole line must be the label ("UNIT TYPE 2C", "UNIT A1.0", "Unit 2-0") so notes such as
# "REFER TO UNIT TYPE 2A PLANS" are not counted.
# The code must contain a digit, so headers such as "UNIT HEATER" / "UNIT TOTALS" are skipped.
# A trailing variant ("Unit 4-0 Type A") stays part of the name, as in the reference "A1 TYPE A".
UNIT_LABEL_RE = re.compile(
    r"^UNIT\s+(TYPE\s+)?([A-Z]*\d[A-Z0-9.\-]*)(\s+TYPE\s+[A-Z0-9]+)?"
    r"(?:\s*-\s*\d\s*(?:BD|BED|BR|BEDROOM)S?)?$", re.I)
LABEL_PREFILTER_RE = re.compile(r"^\s*UNIT\s+(?:TYPE\s+)?[A-Z]*\d", re.I | re.M)
INLINE_BED_RE =re.compile(r"-\s*(\d)\s*(?:BD|BED|BR|BEDROOM)S?\b", re.I)
BED_LINE_RE = re.compile(r"^(\d)\s*(?:BD|BED|BR|BEDROOM)S?$", re.I)
STUDIO_LINE_RE = re.compile(r"^(STUDIO|STU)$", re.I)
UNIT_NO_RE = re.compile(r"^\d{3,5}[A-Z]?$")

# How far (PDF points) around a UNIT TYPE line to look for the rest of its label stack.
STACK_DX = 90
STACK_DY = 75
# Some sheets draw every label twice, slightly offset (overlaid text layers). Same-name labels
# closer than this are one unit; real neighbouring units measured >= 121pt apart.
DUPLICATE_DISTANCE = 100


def _lines(page: fitz.Page) -> list[dict[str, Any]]:
    """Text lines with page-space bbox and rotation; OCR when the page has no text layer."""
    out = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = " ".join(span["text"] for span in line.get("spans", [])).strip()
            if not text:
                continue
            dx, dy = line.get("dir", (1.0, 0.0))
            rotation = int(round(math.degrees(math.atan2(dy, dx)) / 90.0) * 90) % 360
            out.append({"text": re.sub(r"\s+", " ", text), "bbox": fitz.Rect(line["bbox"]),
                        "rotation": rotation, "source": "PDF text"})
    if len(out) < 5:
        out.extend(_ocr_lines(page))
    return out


def _ocr_lines(page: fitz.Page) -> list[dict[str, Any]]:
    """OCR a page in four orientations so sideways and upside-down plan labels are found."""
    pix = page.get_pixmap(matrix=fitz.Matrix(2.4, 2.4), alpha=False)
    original = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    ow, oh = original.size
    sx, sy = page.rect.width / ow, page.rect.height / oh
    found: list[dict[str, Any]] = []
    try:
        for angle in (0, 90, 180, 270):
            rotated = original if angle == 0 else original.rotate(angle, expand=True)
            data = pytesseract.image_to_data(rotated, config="--psm 11", output_type=pytesseract.Output.DICT)
            grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
            for i, text in enumerate(data.get("text", [])):
                text = str(text).strip()
                try:
                    confidence = float(data["conf"][i])
                except (ValueError, TypeError, KeyError):
                    confidence = -1
                if not text or confidence < 25:
                    continue
                key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
                grouped.setdefault(key, []).append({
                    "text": text, "x": int(data["left"][i]), "y": int(data["top"][i]),
                    "w": int(data["width"][i]), "h": int(data["height"][i]), "order": i,
                })
            for group in grouped.values():
                group.sort(key=lambda item: item["order"])
                boxes = []
                for item in group:
                    x, y, w, h = item["x"], item["y"], item["w"], item["h"]
                    # Map the box from rotated-image pixels back to original page pixels.
                    if angle == 0:
                        box = (x, y, x + w, y + h)
                    elif angle == 90:  # PIL rotates counter-clockwise
                        box = (ow - (y + h), x, ow - y, x + w)
                    elif angle == 180:
                        box = (ow - (x + w), oh - (y + h), ow - x, oh - y)
                    else:
                        box = (y, oh - (x + w), y + h, oh - x)
                    boxes.append(fitz.Rect(box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy))
                rect = fitz.Rect(boxes[0])
                for b in boxes[1:]:
                    rect |= b
                found.append({"text": " ".join(i["text"] for i in group), "bbox": rect,
                              "rotation": angle, "source": "OCR"})
    except pytesseract.TesseractNotFoundError:
        return []
    return found


def _centre(rect: fitz.Rect) -> tuple[float, float]:
    return (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2


def _near(a: fitz.Rect, b: fitz.Rect) -> float | None:
    """Distance between two lines if b is inside a's label stack, else None."""
    (ax, ay), (bx, by) = _centre(a), _centre(b)
    if abs(ax - bx) > STACK_DX or abs(ay - by) > STACK_DY:
        return None
    return math.hypot(ax - bx, ay - by)


def detect_unit_types(page: fitz.Page, page_number: int) -> list[dict[str, Any]]:
    """One tag per unit label line, named 'xx-<n>BED' when a bedroom line is in its stack."""
    lines = _lines(page)
    beds = [ln for ln in lines if BED_LINE_RE.match(ln["text"]) or STUDIO_LINE_RE.match(ln["text"])]
    unit_nos = [ln for ln in lines if UNIT_NO_RE.match(ln["text"])]
    used_beds: set[int] = set()
    tags = []
    for line in lines:
        match = UNIT_LABEL_RE.match(line["text"])
        if not match:
            continue
        unit_type = match.group(2).upper().strip(".-")
        if match.group(3):
            unit_type += " " + re.sub(r"\s+", " ", match.group(3).strip().upper())
        bed = None
        inline = INLINE_BED_RE.search(line["text"])
        if inline:
            bed = f"{inline.group(1)}BED"
        elif match.group(1):
            # Only "UNIT TYPE" tags carry a bedroom line in their stack; for plain "UNIT A1.0"
            # labels a nearby "2BD" belongs to something else.
            options = [(d, i) for i, b in enumerate(beds)
                       if i not in used_beds and (d := _near(line["bbox"], b["bbox"])) is not None]
            if options:
                _, i = min(options)
                used_beds.add(i)
                m = BED_LINE_RE.match(beds[i]["text"])
                bed = f"{m.group(1)}BED" if m else "STUDIO"
        nos = [(d, u["text"]) for u in unit_nos if (d := _near(line["bbox"], u["bbox"])) is not None]
        tags.append(_tag(
            page_number, f"{unit_type}-{bed}" if bed else unit_type, line,
            unit_no=min(nos)[1] if nos else "",
        ))
    return _with_floor(page, _dedupe(tags))


def _token_key(value: str) -> str:
    """Normalise OCR/PDF tokens while tolerating common 0/O and 1/I errors."""
    value = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).casefold()
    if any(ch.isdigit() for ch in value):
        value = value.replace("o", "0").replace("i", "1").replace("l", "1")
    return value


def _tokens(text: str) -> list[str]:
    return [k for t in re.findall(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", text) if (k := _token_key(t))]


def _contains(line_tokens: list[str], name_tokens: list[str]) -> bool:
    n = len(name_tokens)
    for i in range(len(line_tokens) - n + 1):
        sample = line_tokens[i:i + n]
        if sample == name_tokens:
            return True
        if all(e == a or (min(len(e), len(a)) >= 4 and SequenceMatcher(None, e, a).ratio() >= 0.82)
               for e, a in zip(name_tokens, sample)):
            return True
    return False


def detect_names(page: fitz.Page, page_number: int, names: list[str]) -> list[dict[str, Any]]:
    """Fallback for jobs without UNIT TYPE labels: tag every line whose whole text is a listed name.

    Matching the whole line (not a substring) keeps 'A2' from also tagging 'A2 END'.
    """
    wanted = [(n.strip(), _tokens(n)) for n in names if len(_token_key(n)) >= 2]
    tags = []
    for line in _lines(page):
        tokens = _tokens(line["text"])
        for name, name_tokens in sorted(wanted, key=lambda w: -len(w[1])):
            if len(tokens) == len(name_tokens) and _contains(tokens, name_tokens):
                tags.append(_tag(page_number, name, line))
                break
    return _with_floor(page, _dedupe(tags))


def _dedupe(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for tag in tags:
        cx, cy = tag["x"] + tag["w"] / 2, tag["y"] + tag["h"] / 2
        if not any(k["unit"] == tag["unit"]
                   and math.hypot(k["x"] + k["w"] / 2 - cx, k["y"] + k["h"] / 2 - cy) < DUPLICATE_DISTANCE
                   for k in kept):
            kept.append(tag)
    return kept


def _tag(page_number: int, name: str, line: dict[str, Any], unit_no: str = "") -> dict[str, Any]:
    r = line["bbox"]
    return {
        "page": page_number, "unit": name, "unit_no": unit_no, "matched_text": line["text"],
        "x": round(r.x0, 1), "y": round(r.y0, 1), "w": round(r.width, 1), "h": round(r.height, 1),
        "source": line["source"],
    }


def suggest_plan_pages(doc: fitz.Document) -> dict[int, int]:
    """Pages holding floor-plan unit labels -> label count.

    Unit-plan sheets title their drawings 'UNIT TYPE 2A - 1 BED'; a bedroom count written
    inline marks a drawing title, not a unit, so those labels do not count here.
    """
    pages = {}
    for i, page in enumerate(doc, start=1):
        # Cheap text check first; also skips image-only pages, so the scan never runs OCR.
        if not LABEL_PREFILTER_RE.search(page.get_text()):
            continue
        n = sum(1 for t in detect_unit_types(page, i) if not INLINE_BED_RE.search(t["matched_text"]))
        if n:
            pages[i] = n
    return pages


# "GROUND FLOOR", "2ND LEVEL" / "LEVEL 4B" (sheet B of level 4), "FLOOR 3".
NAMED_FLOOR_RE = re.compile(
    r"\b(GROUND|BASEMENT|PODIUM|MEZZANINE|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH"
    r"|\d{1,2}(?:ST|ND|RD|TH))\s+(FLOOR|LEVEL)\b", re.I)
NUMBERED_FLOOR_RE = re.compile(r"\b(LEVEL|FLOOR)\s*[-:#]?\s*(\d{1,2})[A-Z]?\b", re.I)


def detect_floor_level(page: fitz.Page) -> str:
    """Floor named by the sheet title: the largest text on the page that names a level.

    Going by size keeps notes and cross-references ("SEE LEVEL 16") from being read as the floor.
    """
    best = (0.0, False, "")
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = " ".join(s["text"] for s in spans).strip()
            if not spans or len(text) > 80:
                continue
            named = NAMED_FLOOR_RE.search(text)
            numbered = None if named else NUMBERED_FLOOR_RE.search(text)
            if not named and not numbered:
                continue
            floor = (f"{named.group(1)} {named.group(2)}" if named
                     else f"{numbered.group(1)} {numbered.group(2)}").upper()
            # On a size tie a line that says PLAN is the title ("LEVEL 4 WATER HEATER ROOM" is not).
            key = (max(s["size"] for s in spans), "PLAN" in text.upper(), floor)
            if key[:2] > best[:2]:
                best = key
    return best[2]


def _with_floor(page: fitz.Page, tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if tags:
        floor = detect_floor_level(page)
        for tag in tags:
            tag["floor_level"] = floor
    return tags


TITLE_RE = re.compile(r"PLAN|LEVEL|FLOOR|SCHEDULE|MATRIX|MIX|ROOF", re.I)


def page_title(page: fitz.Page) -> str:
    """Largest-font plan/schedule heading on the page, for the page picker."""
    best = (0.0, "")
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = " ".join(s["text"] for s in spans).strip()
            if spans and TITLE_RE.search(text) and len(text) <= 80:
                size = max(s["size"] for s in spans)
                if size > best[0]:
                    best = (size, re.sub(r"\s+", " ", text))
    return best[1]
