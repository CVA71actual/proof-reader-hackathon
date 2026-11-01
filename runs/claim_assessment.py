"""Versioned evidence bindings for event-row checks.

The text/box checks do not establish that the claimed words are visible in the
rendered page. This module records that limitation explicitly and binds each
ordinary check to its exact candidate inputs, page artifacts, and checker code.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from checks import event_checks
from checks.restriction import page_capability

REPO = Path(__file__).resolve().parent.parent
VISIBLE_NOT_CHECKED = {
    "result": "not_checked",
    "label": "Claimed words visible on the rendered page",
    "reason": "The text-layer and box checks do not read the words from rendered page ink.",
}

CHECK_INPUTS = {
    event_checks.QUOTE_ID: ("quote", "box"),
    "selector_match": ("quote", "box"),
    event_checks.DATE_ID: ("date_text", "quote", "event_type", "title", "program"),
    event_checks.SOURCE_ID: (),
    event_checks.TYPE_ID: ("event_type", "quote"),
    "text_layer_match": ("value",),
    "row_column_match": ("value", "box", "column", "row_label", "columns"),
}
# Checks whose result depends on the page capability (checks/restriction.py): the capability the check was
# COMPUTED with is a bound input. check_current re-derives it from the page record now, so a check computed
# without it (None) or with another page's capability is unbound by construction and load re-runs it.
CAPABILITY_CHECKS = frozenset({event_checks.QUOTE_ID, "selector_match", "text_layer_match", "row_column_match"})
_STAMPED = object()

def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def checker_version(check_name: str) -> str:
    if check_name not in {*CHECK_INPUTS, event_checks.QUOTE_ID}:
        return "unknown"
    files = sorted(REPO.joinpath("checks").rglob("*.py"))
    return _digest({str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files})


def page_evidence(source: dict, page_number, consumed_artifact: dict | None = None) -> dict:
    page = next((p for p in source.get("pages", []) if p.get("page_number") == page_number), {})
    derived = page.get("derived_words") or {}
    return {"source_id": source.get("id"), "page_number": page_number,
            "image_sha256": page.get("image_sha256"), "words_sha256": page.get("words_sha256"),
            "words_used": consumed_artifact,
            "derived_words": derived or None,
            "visual_evidence": page.get("visual_evidence"),
            "capture_completeness": page.get("capture_completeness"),
            "scanned": page.get("scanned"),
            "regions": page.get("regions"), "width_pt": page.get("width_pt"),
            "height_pt": page.get("height_pt")}


def _page(source: dict, page_number) -> dict:
    return next((p for p in source.get("pages", []) if p.get("page_number") == page_number), {})


def assessment_for(check_name: str, row: dict, source: dict, *, consumed_artifact: dict | None = None,
                   chosen: bool = False, output: dict | None = None, capability: dict | None = None) -> dict:
    """`capability`: the page capability the check was computed with (checks.restriction.capability).
    None for a capability check means "computed without it": it is bound as None and never current."""
    fields = CHECK_INPUTS.get(check_name, ())
    candidate = {key: row.get(key) for key in fields}
    if check_name in CAPABILITY_CHECKS:
        candidate["page_capability"] = capability
    if check_name == event_checks.SOURCE_ID:
        candidate = {"source_kind": source.get("kind")}
    elif check_name == event_checks.DATE_ID:
        candidate["source_published_date"] = source.get("published_date")
        # A date produced by this same check is a projection, not an input.
        # A person's selected date is a real input and must bind explicitly.
        if chosen:
            candidate["date"] = row.get("date")
            candidate["date_end"] = row.get("date_end")
    # Every row check consumes the cited page's text or geometry; both hashes
    # are included so a rendered image change invalidates the visible evidence too.
    bound = {"inputs": candidate, "evaluation_mode": {"date_person_selected": bool(chosen)} if check_name == event_checks.DATE_ID else None,
             "evidence": page_evidence(source, row.get("page_number"), consumed_artifact),
             "checker_version": checker_version(check_name), "output": output}
    return {**bound, "input_sha256": _digest(candidate), "assessment_sha256": _digest(bound)}


def visible_passage_assessment(row: dict, source: dict) -> dict:
    inputs = {"quote": row.get("quote"), "box": row.get("box")}
    evidence = page_evidence(source, row.get("page_number"))
    version = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    bound = {"inputs": inputs, "evidence": evidence, "checker_version": version}
    return {**VISIBLE_NOT_CHECKED, "assessment": {**bound, "input_sha256": _digest(inputs),
                                                   "assessment_sha256": _digest(bound)}}


def bind_checks(results, row: dict, source: dict, *, consumed_artifact: dict | None = None, chosen: bool = False,
                capability: dict | None = None):
    """Attach an immutable assessment binding to every returned CheckResult.
    Row checks always derive their restriction from the cited page itself (runs/recheck.row_checks), so
    the capability defaults to the page's own; row_checks passes the value it actually used."""
    from dataclasses import replace
    from runs.build_run import to_contract_check

    if capability is None:
        capability = page_capability(_page(source, row.get("page_number")))
    bound = []
    for result in results:
        contract = to_contract_check(result.to_dict())
        output = {key: contract[key] for key in ("result", "reason", "compared", "near_match", "differs")
                  if key in contract}
        bound.append(replace(result, assessment=assessment_for(
            result.check, row, source, consumed_artifact=consumed_artifact, chosen=chosen, output=output,
            capability=capability)))
    return bound


def bind_contract_checks(checks: list[dict], row: dict, source: dict, *, consumed_artifact: dict | None = None,
                         capability=_STAMPED) -> list[dict]:
    """Bind already serialized table check results to the exact cell and page inputs.
    `capability`: the page capability the cell was checked with. When not passed, each check's own
    "page_capability" stamp (set by checks.status.check_cell) is used; a check without a stamp binds
    None and is never current, so a check computed without capability re-runs on load.
    TODO(SLICE-1B-001, after PERF-001): runs/check_load.py:73 should pass capability= explicitly (the
    cell's checks all carry the same stamp); then drop the _STAMPED default so an unflagged call raises
    TypeError. Left out of this change because runs/check_load.py is reserved for PERF-001."""
    out = []
    for check in checks:
        from runs.build_run import to_contract_check

        used = check.get("page_capability") if capability is _STAMPED else capability
        result = to_contract_check(check) if check.get("check") else dict(check)
        result.pop("page_capability", None)
        name = result.get("name") or result.get("check")
        result["assessment"] = assessment_for(name, row, source, consumed_artifact=consumed_artifact,
                                             output={key: result[key] for key in
                                                     ("result", "reason", "compared", "near_match", "differs")
                                                     if key in result},
                                             capability=used)
        out.append(result)
    return out


def check_current(check: dict, row: dict, source: dict, *, consumed_artifact: dict | None = None) -> bool:
    from runs.build_run import to_contract_check

    name = check.get("name") or check.get("check")
    recorded = check.get("assessment")
    if not isinstance(recorded, dict):
        return False
    mode = (recorded or {}).get("evaluation_mode") or {}
    chosen = bool(mode.get("date_person_selected"))
    actual = consumed_artifact or (recorded.get("evidence") or {}).get("words_used")
    if not actual or actual.get("kind") == "unverified":
        return False
    page = _page(source, row.get("page_number"))
    if actual.get("kind") == "unavailable":
        # A missing cited page is a stable, explicit could-not-run fact. It is
        # current only while the source still has no page at that number.
        if page:
            return False
        declared = {"file": None, "sha256": None}
    elif actual.get("kind") == "image":
        declared = {"file": page.get("image"), "sha256": page.get("image_sha256")}
    elif actual.get("kind") == "derived":
        derived = page.get("derived_words") or {}
        declared = {"file": derived.get("file"), "sha256": derived.get("sha256")}
    elif actual.get("kind") == "captured":
        declared = {"file": page.get("words"), "sha256": page.get("words_sha256")}
    else:
        return False
    if actual.get("file") != declared.get("file") or actual.get("sha256") != declared.get("sha256"):
        return False
    # Bind and validate the same public check representation. In-memory checks
    # can use tuples or internal comparison keys that JSON serialization later
    # normalizes; hashing those raw values makes a valid recheck look stale.
    contract = to_contract_check(check) if check.get("check") else check
    output = {key: contract[key] for key in ("result", "reason", "compared", "near_match", "differs")
              if key in contract}
    if output.get("result") is None:
        return False
    # The capability is re-derived from the page record now, never read back from the binding.
    return recorded == assessment_for(name, row, source, consumed_artifact=actual, chosen=chosen, output=output,
                                      capability=page_capability(page))


def checks_current(checks: list[dict], row: dict, source: dict, *, consumed_artifact: dict | None = None) -> bool:
    """Validate the complete event-check set through its sole aggregator, then each bound input."""
    if not event_checks.complete_event_checks(checks):
        return False
    by_name = {check.get("name") or check.get("check"): check for check in checks}
    return all(check_current(by_name[name], row, source, consumed_artifact=consumed_artifact)
               for name in event_checks.REQUIRED_EVENT_CHECKS)


def row_source(run: dict, row: dict) -> dict:
    return next((s for s in run.get("sources", []) if s.get("id") == row.get("source_id")), {})

