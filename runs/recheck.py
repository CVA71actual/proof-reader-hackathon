"""Run the real checks again on a person's fix, and write the "recheck" line the state rule expects.

Used by the sample build (its example fix on e7) and by the decisions server (every fix a person makes).
Nothing here is re-judged by hand: the checks are the same functions that checked the run.

    row_checks(row, source, words_doc)   event row: quote_match, selector_match, date_match (period-aware),
                                         source_rule, type_match
    recheck_row(at, row, source, words_doc)
    recheck_cell(at, cell, words_doc)    table value: text_layer_match + selector_match + row_column_match
                                         (checks.status)

A re-check passes only when the fixed item comes out "matched" under the same status rule the run used.
If it did not pass, the result is "could_not_run" when no check failed but at least one could not run
(for example the reader proposed no box), and "fail" otherwise. A re-check that could not run is not a failure.

A fix settles the readers' disagreement only when it changes the field they disagreed on (the row's
"disagreement".field); a passing re-check of such a fix says so first. A fix of any other field leaves the
disagreement open, whatever its re-check says.

A fix of a field no check compares (state.ROW_CHECKED_FIELDS / CELL_CHECKED_FIELDS; for example a title) cannot
be shown right by a re-check: unless a check fails, its result is "could_not_run" with the reason
"No check compares <field>; the fix is recorded but not demonstrated." A fix that settles the disagreement is
the exception: the person's choice of the disputed value is the resolution, and the checks still run.

Before and after (a fix is a demonstrated mistake only when the old value was wrong): the server runs the same
checks twice, on the item as it was BEFORE the fix (the old value) and AFTER it (the new value), and writes both
into the re-check line as "before": {result, checks} and "after": {result, checks}. The line's own result,
reason and checks are the after-run. The state rule counts a caught mistake only when after is "pass" and before
is "fail". Before "could_not_run" does not count and is marked before_undemonstrated. Before "pass" and after "pass" is an edit
without an error: recorded, not counted.

words_doc None means the cited page was not captured. The checks that read the page (the quote and where it
sits; for a table value, the text layer and where it sits) then could not run: "Page N was not captured."
No other page is ever used in its place.
"""

from __future__ import annotations

from checks import event_checks
from checks.common import COULD_NOT_RUN, FAIL, CheckResult
from checks.selector_match import selector_match
from checks.status import MATCHED as CELL_MATCHED
from checks.status import check_cell
from runs.build_run import to_contract_check
from runs.state import checked_fields

PASSED_REASON = "The checks were run again on the fixed value and passed."
FAILED_REASON = "The checks were run again on the fixed value and failed."
SETTLES_PREFIX = "Settles the readers' disagreement by the person's fix. "

BLANK_PAGE = {"words": [], "page_width": 612.0, "page_height": 792.0}


def page_not_captured(page_number) -> str:
    return f"Page {page_number} was not captured."


def uncovered_reason(field: str) -> str:
    return f"No check compares {field}; the fix is recorded but not demonstrated."


def _not_captured(result: CheckResult, page_number) -> CheckResult:
    return CheckResult(result.check, result.label, COULD_NOT_RUN, page_not_captured(page_number), result.compared)


def row_checks(row: dict, source: dict, words_doc: dict | None) -> list:
    """The five event-row checks, in the order the run shows them."""
    words = words_doc["words"] if words_doc else []
    w, h = (words_doc["page_width"], words_doc["page_height"]) if words_doc else (612.0, 792.0)
    return [
        event_checks.quote_match(row.get("quote"), words),
        selector_match(row.get("quote"), row.get("box"), words, w, h),
        event_checks.date_match(row.get("date_text"), row.get("date"), row.get("quote"),
                                date_end=row.get("date_end"), event_type=row.get("event_type"),
                                title=row.get("title"), program=row.get("program")),
        event_checks.source_rule(source["kind"], []),
        event_checks.type_match(row.get("event_type"), row.get("quote")),
    ]


def could_not_run_reason(checks: list[dict]) -> str:
    labels = [c["label"] for c in checks if c["result"] == COULD_NOT_RUN]
    return f"The re-check could not run: {' and '.join(labels)} had nothing to compare."


def recheck_result(checks: list[dict], passed: bool) -> str:
    if passed:
        return "pass"
    results = {c["result"] for c in checks}
    return COULD_NOT_RUN if FAIL not in results and COULD_NOT_RUN in results else "fail"


def recheck_line(at: str, target: str, checks: list[dict], passed: bool, settles: bool = False,
                 reason_if_not_run: str | None = None) -> dict:
    """`settles`: the fix changed the field the two readers disagreed on."""
    result = recheck_result(checks, passed)
    reason = ({"pass": PASSED_REASON, "fail": FAILED_REASON}.get(result)
              or reason_if_not_run or could_not_run_reason(checks))
    if settles and result == "pass":
        reason = SETTLES_PREFIX + reason
    return {"at": at, "target": target, "action": "recheck", "note": None, "fix": None, "by": "code",
            "recheck": {"result": result, "reason": reason, "checks": checks}}


def _finish(at: str, target: str, kind: str, checks: list[dict], passed: bool, settles: bool,
            field: str | None, page_missing) -> dict:
    if field is not None and not settles and field not in checked_fields(kind):
        # Nothing compared the changed field: unless a check fails, the fix is recorded, not demonstrated.
        if FAIL in {c["result"] for c in checks}:
            return recheck_line(at, target, checks, False)
        return {"at": at, "target": target, "action": "recheck", "note": None, "fix": None, "by": "code",
                "recheck": {"result": COULD_NOT_RUN, "reason": uncovered_reason(field), "checks": checks}}
    not_run = f"The re-check could not run: {page_not_captured(page_missing)}" if page_missing is not None else None
    return recheck_line(at, target, checks, passed, settles, reason_if_not_run=not_run)


def _with_before(line: dict, before_line: dict | None) -> dict:
    """Attach the before-run and the after-run (the line's own result and checks) to a re-check line."""
    if before_line is None:
        return line
    rc = line["recheck"]
    b = before_line["recheck"]
    rc["before"] = {"result": b["result"], "checks": b["checks"]}
    rc["after"] = {"result": rc["result"], "checks": rc["checks"]}
    return line


def recheck_row(at: str, row: dict, source: dict, words_doc: dict | None, settles: bool = False,
                field: str | None = None, before: dict | None = None) -> dict:
    """`row` already carries the fixed values; `field` is the field the fix changed (None: not given).
    `before`: the row as it was before the fix (the old value); the same checks run on it too.
    words_doc None: the row's page was not captured."""
    before_line = None if before is None else recheck_row(at, before, source, words_doc, settles, field)
    return _with_before(_recheck_row(at, row, source, words_doc, settles, field), before_line)


def _recheck_row(at: str, row: dict, source: dict, words_doc: dict | None, settles: bool,
                 field: str | None) -> dict:
    results = row_checks(row, source, words_doc)
    page_missing = None
    if words_doc is None:
        page_missing = row.get("page_number")
        results[0], results[1] = _not_captured(results[0], page_missing), _not_captured(results[1], page_missing)
    passed = event_checks.row_status(results) == event_checks.MATCHED
    return _finish(at, row["id"], "row", [to_contract_check(r.to_dict()) for r in results], passed, settles,
                   field, page_missing)


def recheck_cell(at: str, cell: dict, words_doc: dict | None, settles: bool = False,
                 field: str | None = None, page_number=None, before: dict | None = None,
                 row_label: str | None = None, columns: list[str] | None = None) -> dict:
    """`cell` already carries the fixed value; its box is the one the reader proposed.
    `before`: the cell as it was before the fix (the old value); the same checks run on it too.
    `row_label` / `columns`: the label of the row the cell sits in and the table's column headers.
    words_doc None: the table's page (`page_number`) was not captured."""
    before_line = None if before is None else recheck_cell(at, before, words_doc, settles, field, page_number,
                                                           row_label=row_label, columns=columns)
    return _with_before(_recheck_cell(at, cell, words_doc, settles, field, page_number, row_label, columns),
                        before_line)


def _recheck_cell(at: str, cell: dict, words_doc: dict | None, settles: bool, field: str | None,
                  page_number, row_label: str | None = None, columns: list[str] | None = None) -> dict:
    checked = check_cell(cell, words_doc or BLANK_PAGE, row_label, columns)
    checks = [to_contract_check(c) for c in checked["checks"]]
    page_missing = None
    if words_doc is None:
        page_missing = page_number
        checks = [{**c, "result": COULD_NOT_RUN, "reason": page_not_captured(page_number)} for c in checks]
    return _finish(at, cell["id"], "cell", checks, checked["status"] == CELL_MATCHED and words_doc is not None,
                   settles, field, page_missing)
