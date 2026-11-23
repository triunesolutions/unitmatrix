"""Floor tags grouped under the unit plans they belong to.

The unit plan is the parent: one plan may be used by several floor tags. Only an exact
name match is placed automatically — nothing is guessed from a shared base code. Every
other floor tag waits in the pool for the manager to drag onto a unit, and a tag placed
that way is marked so it stays identifiable.
"""
from __future__ import annotations

from typing import Any

from .sorting import natural_key

UNASSIGNED = "(not placed)"


def normalise(name: str) -> str:
    return str(name or "").strip().upper()


def parents(summary: list[tuple[str, int]],
            plans: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(units, pool). `summary` is (floor tag, qty); `plans` the unit-plan names.

    A floor tag whose name is exactly a unit plan's name starts under that plan.
    """
    qty = {normalise(t): q for t, q in summary}
    names = {normalise(t): t for t, _ in summary}
    units = []
    claimed: set[str] = set()
    for plan in sorted(set(plans), key=natural_key):
        key = normalise(plan)
        kids = []
        if key in qty and key not in claimed:
            claimed.add(key)
            kids.append({"tag": names[key], "qty": qty[key]})
        units.append({"unit": plan, "kids": kids})
    pool = [{"tag": names[k], "qty": qty[k]} for k in sorted(
        qty, key=lambda key: natural_key(names.get(key, key))) if k not in claimed]
    return units, pool


def merged_rows(summary: list[tuple[str, int]], assigned: dict[str, list[str]],
                manual: list[str]) -> list[dict[str, Any]]:
    """Floor Tag / Unit Plan / Qty / Merge Qty, one row per floor tag.

    Floor tags run in ascending order, and a group's tags stay together: the groups
    themselves are ordered by their own lowest tag, so the table reads as one ascending
    list of floor tags while each unit plan still spans its own block. Only a group's
    first row carries the plan and its merged quantity, as in the manual takeoffs.

    A floor tag nobody placed falls under "(not placed)" so the grand total still equals
    the count, and a plan with no tags is kept so it is visible as unused.
    """
    qty = {normalise(t): q for t, q in summary}
    names = {normalise(t): t for t, _ in summary}
    by_hand = {normalise(m) for m in manual}

    groups = []
    placed: set[str] = set()
    for plan, tags in assigned.items():
        keys = sorted({normalise(t) for t in tags if normalise(t) in qty},
                      key=lambda key: natural_key(names.get(key, key)))
        placed.update(keys)
        groups.append((keys[0] if keys else "￿" + normalise(plan), plan, keys))

    left = sorted((k for k in qty if k not in placed),
                  key=lambda key: natural_key(names.get(key, key)))
    if left:
        groups.append((left[0], UNASSIGNED, left))

    rows: list[dict[str, Any]] = []
    for _, plan, keys in sorted(groups, key=lambda group: (
            natural_key(names.get(group[0], group[0])), natural_key(group[1]))):
        if not keys:
            rows.append({"tag": "", "unit": plan, "qty": "", "merged": 0, "manual": False})
            continue
        merged = sum(qty[k] for k in keys)
        for position, key in enumerate(keys):
            rows.append({
                "tag": names[key],
                "unit": plan if position == 0 else "",
                "qty": qty[key],
                "merged": merged if position == 0 else "",
                "manual": key in by_hand,
            })
    return rows


def matching_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Totals for the numeric Qty and Merge Qty cells displayed by the matcher."""
    return {
        "qty": sum(value for row in rows
                   if isinstance((value := row.get("qty")), int) and not isinstance(value, bool)),
        "merged": sum(value for row in rows
                      if isinstance((value := row.get("merged")), int) and not isinstance(value, bool)),
    }
