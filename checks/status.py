"""Combine the check results into one status per cell, and run the checks over a reading.

Statuses:
    matched     every required check ran and passed
    failed      at least one check failed
    proposed    anything else (a check could not run): the reader's value stands as a proposal
    unreadable  the reading itself could not be understood, so there are no cells to check

A box being present, or two readers agreeing, never makes a cell "matched" on its own.
"""

from __future__ import annotations

from checks.common import FAIL, PASS, CheckResult
from checks.row_column_match import CHECK_ID as ROW_COLUMN_ID
from checks.row_column_match import row_column_match
from checks.selector_match import CHECK_ID as SELECTOR_ID
from checks.selector_match import selector_match
from checks.text_layer_match import CHECK_ID as TEXT_ID
from checks.text_layer_match import text_layer_match

MATCHED = "matched"
FAILED = "failed"
PROPOSED = "proposed"
UNREADABLE = "unreadable"

REQUIRED_CHECKS = (TEXT_ID, SELECTOR_ID, ROW_COLUMN_ID)


def combine_status(results: list[CheckResult], required: tuple[str, ...] = REQUIRED_CHECKS) -> str:
    if any(r.result == FAIL for r in results):
        return FAILED
    by_id = {r.check: r for r in results}
    if all(cid in by_id and by_id[cid].result == PASS for cid in required):
        return MATCHED
    return PROPOSED


def check_cell(cell: dict, words_doc: dict, row_label: str | None = None,
               columns: list[str] | None = None) -> dict:
    """`row_label`: the label of the row the cell is saved in; it is always the claim checked. A "label" the
    reader put on the cell itself is only compared against it (a different one fails the check). With no
    `row_label` (a cell checked on its own), the cell's own "label" is the claim.
    `columns`: the table's column headers as the reader gave them."""
    words = words_doc["words"]
    value = cell.get("value")
    if row_label is None:
        label, reader_label = cell.get("label"), None
    else:
        label, reader_label = row_label, cell.get("label")
    results = [
        text_layer_match(value, words),
        selector_match(value, cell.get("box"), words, words_doc["page_width"], words_doc["page_height"]),
        row_column_match(label, cell.get("column"), cell.get("box"), words, columns, reader_label),
    ]
    return {**cell, "checks": [r.to_dict() for r in results], "status": combine_status(results)}


def check_reading(reading: dict, words_doc: dict) -> dict:
    """Return {status, rows:[{label, cells:[cell + checks + status]}], counts}."""
    table = reading.get("table")
    if not reading.get("readable") or not isinstance(table, dict):
        return {"status": UNREADABLE, "rows": [], "counts": {UNREADABLE: 1}}
    rows = []
    counts = {MATCHED: 0, FAILED: 0, PROPOSED: 0}
    for row in table.get("rows", []):
        columns = [str(c) for c in table.get("columns", [])] or None
        # the row the cells are saved under is the claim: "" when it has no label, never the cell's own label
        shown = row.get("label")
        claim = "" if shown is None else str(shown)
        cells = [check_cell(c, words_doc, claim, columns) for c in row.get("cells", [])]
        for c in cells:
            counts[c["status"]] += 1
        rows.append({"label": row.get("label", ""), "cells": cells})
    return {"status": "checked", "rows": rows, "counts": counts}
