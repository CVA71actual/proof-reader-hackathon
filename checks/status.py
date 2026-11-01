"""Combine the check results into one status per cell, and run the checks over a reading.

Statuses:
    matched     every required check ran and passed
    failed      at least one check failed
    proposed    anything else (a check could not run): the reader's value stands as a proposal
    unreadable  the reading itself could not be understood, so there are no cells to check

A box being present, or two readers agreeing, never makes a cell "matched" on its own.
"""

from __future__ import annotations

from checks.common import COULD_NOT_RUN, FAIL, PASS, CheckResult
from checks.row_column_match import CHECK_ID as ROW_COLUMN_ID, LABEL as ROW_COLUMN_LABEL
from checks.restriction import CELL, capability, page_restriction
from checks.row_column_match import row_column_match
from checks.selector_match import CHECK_ID as SELECTOR_ID, LABEL as SELECTOR_LABEL
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
               columns: list[str] | None = None, *, visual_evidence: dict | None,
               derived_words_present: bool) -> dict:
    """`row_label`: the label of the row the cell is saved in; it is always the claim checked. A "label" the
    reader put on the cell itself is only compared against it (a different one fails the check). With no
    `row_label` (a cell checked on its own), the cell's own "label" is the claim.
    `columns`: the table's column headers as the reader gave them.
    `visual_evidence` / `derived_words_present`: the page capability, required (pass None / False for a
    native page): an unflagged call raises TypeError rather than running geometry on an unknown page.
    Each returned check carries the capability it was computed with under "page_capability"; the binding
    (runs/claim_assessment.bind_contract_checks) consumes it and run.json never stores it."""
    words = words_doc["words"]
    value = cell.get("value")
    if row_label is None:
        label, reader_label = cell.get("label"), None
    else:
        label, reader_label = row_label, cell.get("label")
    # OCR/transcription words can support a narrow text-occurrence fact, but
    # their synthetic positions cannot establish where a table value or its
    # row/column sits on the source page.
    box = cell.get("box")
    # No `box` argument: a box-less cell on a restricted page keeps the page's own reason (pinned by
    # tests/test_row_column.py's job-table test); only the row path falls through to the selector.
    restriction = page_restriction(visual_evidence, derived_words_present, path=CELL)
    if restriction.restricted:
        results = [
            text_layer_match(value, words, transcribed=True),
            CheckResult(SELECTOR_ID, SELECTOR_LABEL, COULD_NOT_RUN, restriction.reason,
                        {"value": value, "box": box}),
            CheckResult(ROW_COLUMN_ID, ROW_COLUMN_LABEL, COULD_NOT_RUN, restriction.reason,
                        {"value": f"row {label}, column {cell.get('column')}", "box": box}),
        ]
    else:
        results = [
            text_layer_match(value, words),
            selector_match(value, box, words, words_doc["page_width"], words_doc["page_height"]),
            row_column_match(label, cell.get("column"), box, words, columns, reader_label),
        ]
    used = capability(visual_evidence, derived_words_present)
    return {**cell, "checks": [{**r.to_dict(), "page_capability": used} for r in results],
            "status": combine_status(results)}


def check_reading(reading: dict, words_doc: dict, visual_evidence: dict | None,
                  derived_words_present: bool) -> dict:
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
        cells = [check_cell(c, words_doc, claim, columns, visual_evidence=visual_evidence,
                            derived_words_present=derived_words_present) for c in row.get("cells", [])]
        for c in cells:
            counts[c["status"]] += 1
        rows.append({"label": row.get("label", ""), "cells": cells})
    return {"status": "checked", "rows": rows, "counts": counts}
