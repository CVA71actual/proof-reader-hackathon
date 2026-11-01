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
from checks.common import COULD_NOT_RUN, FAIL, PASS, UNRESOLVED, CheckResult
from checks.selector_match import CHECK_ID as SELECTOR_ID, LABEL as SELECTOR_LABEL, selector_match
from checks.selector_match import region_match, selector_match
from checks.status import MATCHED as CELL_MATCHED
from checks.status import check_cell
from runs.build_run import to_contract_check
from runs.state import checked_fields
from runs.claim_assessment import bind_checks

PASSED_REASON = "The checks were run again on the fixed value and passed."
FAILED_REASON = "The checks were run again on the fixed value and failed."
COULD_NOT_RUN_FALLBACK = "The re-check could not run: a check had nothing to compare."
SETTLES_PREFIX = "Settles the readers' disagreement by the person's fix. "

BLANK_PAGE = {"words": [], "page_width": 612.0, "page_height": 792.0}


def page_not_captured(page_number) -> str:
    return f"Page {page_number} was not captured."


def uncovered_reason(field: str) -> str:
    return f"No check compares {field.replace('_', ' ')}; the fix is recorded but not demonstrated."


def _not_captured(result: CheckResult, page_number) -> CheckResult:
    return CheckResult(result.check, result.label, COULD_NOT_RUN, page_not_captured(page_number), result.compared)


DATE_FIELDS = ("date_text", "date", "date_end")


def as_iso(value):
    """A day a person typed as it is printed ('March 12, 2027') is read as the day it names ('2027-03-12'); anything
    else is returned as it was."""
    if not isinstance(value, str):
        return value
    day = event_checks.parse_exact_date(event_checks.clean_date_text(value))
    return day.isoformat() if day else value


def row_checks(row: dict, source: dict, words_doc: dict | None, chosen: bool = False,
               consumed_artifact: dict | None = None) -> list:
    """The five event-row checks, in the order the run shows them. `chosen`: a person typed the date (a fix of
    date_text, date or date_end), so the quote need not show by itself which of its dates is the event's."""
    words = words_doc["words"] if words_doc else []
    w, h = (words_doc["page_width"], words_doc["page_height"]) if words_doc else (612.0, 792.0)
    regions = next((p.get("regions") for p in source.get("pages", []) if p.get("page_number") == row.get("page_number")),
                   None)
    page = next((p for p in source.get("pages", []) if p.get("page_number") == row.get("page_number")), {})
    visual = page.get("visual_evidence") or {}
    # A positive contradiction from the captured SAM regions remains a failed
    # citation even when transcription/visibility cannot be established.
    region_width = (words_doc or {}).get("page_width") or page.get("width_pt") or 612.0
    region_height = (words_doc or {}).get("page_height") or page.get("height_pt") or 792.0
    region_result = region_match(row.get("box"), regions, region_width, region_height) if regions and words_doc is not None else None
    if region_result is not None:
        # This is the narrower, actually executed box-in-region comparison.
        # Approximate transcription geometry affects word alignment, not this
        # independently bound comparison in the page coordinate frame.
        box_check = region_result
    elif row.get("box") is not None and (page.get("derived_words") or visual.get("origin") == "approximate"):
        box_check = CheckResult(
            SELECTOR_ID, SELECTOR_LABEL, COULD_NOT_RUN,
            "SAM 3.1 found no region to compare the box with; word alignment cannot run on derived or approximate geometry.",
            {"value": row.get("quote"), "box": row.get("box")},
        )
    elif row.get("box") is not None and visual.get("origin") == "hidden_text":
        box_check = CheckResult(SELECTOR_ID, SELECTOR_LABEL, COULD_NOT_RUN, visual["reason"],
                                {"value": row.get("quote"), "box": row.get("box")})
    else:
        box_check = selector_match(row.get("quote"), row.get("box"), words, w, h, quote_lines=True)
    results = [
        event_checks.quote_match(row.get("quote"), words, row.get("box")),
        box_check,
        event_checks.date_match(row.get("date_text"), as_iso(row.get("date")), row.get("quote"),
                                date_end=as_iso(row.get("date_end")), chosen=chosen, event_type=row.get("event_type"),
                                title=row.get("title"), program=row.get("program"),
                                filed=source.get("published_date"),
                                today=(event_checks.release_day(words, source.get("published_date")) or (None,))[0]),
        event_checks.source_rule(source["kind"], []),
        event_checks.type_match(row.get("event_type"), row.get("quote")),
    ]
    return bind_checks(results, row, source, consumed_artifact=consumed_artifact, chosen=chosen)


def could_not_run_reason(checks: list[dict]) -> str:
    """The reason of the first check that could not run, as that check gave it on the new value."""
    first = (next((c for c in checks if c["result"] == COULD_NOT_RUN), None)
             or next((c for c in checks if c["result"] == UNRESOLVED), None))
    return f"The re-check could not run: {first['reason']}" if first else COULD_NOT_RUN_FALLBACK


def unresolved_reason(checks: list[dict]) -> str:
    """The plain-words reason of the first unresolved check, as that check gave it."""
    first = next((c for c in checks if c["result"] == UNRESOLVED), None)
    return first["reason"] if first else COULD_NOT_RUN_FALLBACK


def failed_reason(checks: list[dict]) -> str:
    """The reason of the first check that failed, as that check gave it on the new value."""
    first = next((c for c in checks if c["result"] == FAIL), None)
    return f"The re-check failed: {first['reason']}" if first else FAILED_REASON


def recheck_result(checks: list[dict], kind: str | bool, field: str | None = None,
                   settles: bool = False) -> str:
    """Derive the re-check result from the same applicability contract at write and replay time."""
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        return COULD_NOT_RUN
    results = [c.get("result") for c in checks]
    # Keep the small result-only entry point used by callers that have already
    # computed applicability. Stateful write/replay paths pass kind + field.
    if isinstance(kind, bool):
        if kind:
            return "pass"
        if FAIL in results:
            return "fail"
        if UNRESOLVED in results:
            return UNRESOLVED
        return COULD_NOT_RUN if COULD_NOT_RUN in results else "fail"
    if FAIL in results:
        return "fail"
    if UNRESOLVED in results:
        return UNRESOLVED
    if field is not None and not settles and field not in checked_fields(kind):
        return COULD_NOT_RUN
    if kind == "row":
        status = event_checks.row_status(checks)
        if status == event_checks.MATCHED:
            return "pass"
        if status == event_checks.FAILED:
            return "fail"
        return COULD_NOT_RUN
    from checks.status import REQUIRED_CHECKS
    by_name = {c.get("name") or c.get("check"): c.get("result") for c in checks}
    if all(by_name.get(name) == "pass" for name in REQUIRED_CHECKS):
        return "pass"
    if UNRESOLVED in by_name.values():
        return UNRESOLVED
    return COULD_NOT_RUN


def recheck_line(at: str, target: str, checks: list[dict], kind: str | bool, field: str | None = None,
                 settles: bool = False, reason_if_not_run: str | None = None) -> dict:
    """`settles`: the fix changed the field the two readers disagreed on."""
    result = recheck_result(checks, kind, field, settles)
    if isinstance(kind, bool):
        reason = (PASSED_REASON if result == "pass" else failed_reason(checks) if result == "fail"
                  else unresolved_reason(checks) if result == UNRESOLVED
                  else reason_if_not_run or could_not_run_reason(checks))
        return {"at": at, "target": target, "action": "recheck", "note": None, "fix": None, "by": "code",
                "recheck": {"result": result, "reason": reason, "checks": checks}}
    unmeasured = (field is not None and not settles and field not in checked_fields(kind)
                  and FAIL not in {c.get("result") for c in checks})
    reason = (PASSED_REASON if result == "pass" else failed_reason(checks) if result == "fail"
              else unresolved_reason(checks) if result == UNRESOLVED
              else uncovered_reason(field) if unmeasured else reason_if_not_run or could_not_run_reason(checks))
    if settles and result == "pass":
        reason = SETTLES_PREFIX + reason
    return {"at": at, "target": target, "action": "recheck", "note": None, "fix": None, "by": "code",
            "recheck": {"result": result, "reason": reason, "checks": checks}}


def _finish(at: str, target: str, kind: str, checks: list[dict], settles: bool,
            field: str | None, page_missing) -> dict:
    not_run = f"The re-check could not run: {page_not_captured(page_missing)}" if page_missing is not None else None
    return recheck_line(at, target, checks, kind, field, settles, reason_if_not_run=not_run)


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
                field: str | None = None, before: dict | None = None,
                consumed_artifact: dict | None = None) -> dict:
    """`row` already carries the fixed values; `field` is the field the fix changed (None: not given).
    `before`: the row as it was before the fix (the old value); the same checks run on it too.
    words_doc None: the row's page was not captured."""
    # the old value was typed by the reader, not chosen by the person: the quote alone must show its date
    before_line = (None if before is None
                   else _recheck_row(at, before, source, words_doc, settles, field, chosen=False,
                                     consumed_artifact=consumed_artifact))
    return _with_before(_recheck_row(at, row, source, words_doc, settles, field, chosen=True,
                                     consumed_artifact=consumed_artifact), before_line)


def _recheck_row(at: str, row: dict, source: dict, words_doc: dict | None, settles: bool,
                 field: str | None, chosen: bool = True, consumed_artifact: dict | None = None) -> dict:
    if words_doc is None and consumed_artifact is None:
        page = next((p for p in source.get("pages", []) if p.get("page_number") == row.get("page_number")), {})
        if not page.get("words") and not page.get("derived_words"):
            consumed_artifact = {"file": None, "sha256": None, "kind": "unavailable"}
    results = row_checks(row, source, words_doc, chosen=chosen and field in DATE_FIELDS,
                         consumed_artifact=consumed_artifact)
    page_missing = None
    if words_doc is None:
        page_missing = row.get("page_number")
        results[0], results[1] = _not_captured(results[0], page_missing), _not_captured(results[1], page_missing)
        results = bind_checks(results, row, source, consumed_artifact=consumed_artifact,
                              chosen=chosen and field in DATE_FIELDS)
    return _finish(at, row["id"], "row", [to_contract_check(r.to_dict()) for r in results], settles,
                   field, page_missing)


def recheck_cell(at: str, cell: dict, words_doc: dict | None, settles: bool = False,
                 field: str | None = None, page_number=None, before: dict | None = None,
                 row_label: str | None = None, columns: list[str] | None = None,
                 visual_evidence: dict | None = None, derived_words_present: bool = False) -> dict:
    """`cell` already carries the fixed value; its box is the one the reader proposed.
    `before`: the cell as it was before the fix (the old value); the same checks run on it too.
    `row_label` / `columns`: the label of the row the cell sits in and the table's column headers.
    words_doc None: the table's page (`page_number`) was not captured."""
    before_line = None if before is None else recheck_cell(at, before, words_doc, settles, field, page_number,
                                                           row_label=row_label, columns=columns,
                                                           visual_evidence=visual_evidence,
                                                           derived_words_present=derived_words_present)
    return _with_before(_recheck_cell(at, cell, words_doc, settles, field, page_number, row_label, columns,
                                      visual_evidence, derived_words_present),
                        before_line)


def _recheck_cell(at: str, cell: dict, words_doc: dict | None, settles: bool, field: str | None,
                  page_number, row_label: str | None = None, columns: list[str] | None = None,
                  visual_evidence: dict | None = None, derived_words_present: bool = False) -> dict:
    checked = check_cell(cell, words_doc or BLANK_PAGE, row_label, columns, visual_evidence=visual_evidence,
                         derived_words_present=derived_words_present)
    checks = [to_contract_check(c) for c in checked["checks"]]
    page_missing = None
    if words_doc is None:
        page_missing = page_number
        checks = [{**c, "result": COULD_NOT_RUN, "reason": page_not_captured(page_number)} for c in checks]
    return _finish(at, cell["id"], "cell", checks, settles, field, page_missing)
