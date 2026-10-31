"""Stable target IDs and integrity checks for saved runs."""

from __future__ import annotations

import re

_ROW_ID = re.compile(r"^e([1-9][0-9]*)$")


def duplicate_target_ids(run: dict) -> list[str]:
    """Return duplicate row/cell target IDs in first-seen order."""
    seen: set[str] = set()
    duplicates: list[str] = []

    def visit(target):
        target_id = target.get("id")
        if not isinstance(target_id, str):
            return
        if target_id in seen and target_id not in duplicates:
            duplicates.append(target_id)
        seen.add(target_id)

    for row in run.get("rows", []):
        visit(row)
    for table in run.get("tables", []):
        for row in table.get("rows", []):
            for cell in row.get("cells", []):
                visit(cell)
    return duplicates


def require_unique_target_ids(run: dict) -> None:
    duplicates = duplicate_target_ids(run)
    if duplicates:
        joined = ", ".join(duplicates)
        raise ValueError(f"Run has duplicate target IDs: {joined}. No decisions or reports can safely use it.")


def reserve_row_numbers(run: dict, job: dict, count: int) -> int:
    """Reserve `count` row numbers from a persisted monotonic counter; return the first number."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("Row ID reservation count must be a non-negative integer.")
    require_unique_target_ids(run)
    largest = 0
    for row in run.get("rows", []):
        match = _ROW_ID.fullmatch(str(row.get("id") or ""))
        if match:
            largest = max(largest, int(match.group(1)))
    saved_next = job.get("next_row_number")
    if not isinstance(saved_next, int) or isinstance(saved_next, bool) or saved_next < 1:
        saved_next = 1
    first = max(saved_next, largest + 1)
    job["next_row_number"] = first + count
    return first
