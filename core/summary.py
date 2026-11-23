"""Editable summary rows that keep corrections separate from detected records."""
from __future__ import annotations

from typing import Any

from .export import label_of
from .match import normalise
from .sorting import natural_key
from .tagrename import normalised_original


def group_id(record: dict[str, Any], category: str) -> str:
    """Stable per-category ID for a global original-name group."""
    original = normalised_original(record)
    if original:
        return f"{category}:name:{original}"
    # Unnamed detections do not form a rename group; retain each occurrence separately.
    identity = record.get("tag_id") or (record.get("page"), record.get("x"),
                                         record.get("y"), record.get("w"),
                                         record.get("h"), record.get("source"))
    return f"{category}:occurrence:{identity!r}"


def build_summary(records: list[dict[str, Any]], corrections: dict[str, int],
                  category: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(group_id(record, category), []).append(record)
    rows = []
    for identifier, group in grouped.items():
        detected = len(group)
        corrected = corrections.get(identifier, detected)
        rows.append({"Group ID": identifier, "Name": label_of(group[0]),
                     "Detected Qty": detected, "Corrected Qty": corrected})
    return sorted(rows, key=lambda row: natural_key(row["Name"]))


def summary_pairs(rows: list[dict[str, Any]], quantity_key: str = "Corrected Qty") -> list[tuple[str, int]]:
    """Convert summary rows to downstream (name, quantity) pairs in natural order."""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        label = str(row["Name"])
        key = normalise(label)
        values = grouped.setdefault(key, [label, 0])
        if natural_key(label) < natural_key(values[0]):
            values[0] = label
        values[1] += int(row[quantity_key])
    return sorted(((name, quantity) for name, quantity in grouped.values()),
                  key=lambda item: natural_key(item[0]))


def summary_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Independent detected and corrected grand totals for one summary table."""
    return {
        "detected": sum(int(row["Detected Qty"]) for row in rows),
        "corrected": sum(int(row["Corrected Qty"]) for row in rows),
    }
