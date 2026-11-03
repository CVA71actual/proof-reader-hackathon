"""Bind saved checks to current page evidence when a run is loaded.

This is deterministic code only: it reads the run's already captured page
artifacts and makes no model calls. The old results remain attached as history.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def bind_on_load(run: dict, run_dir: Path, *, refuse_invalid_artifact: bool = False) -> dict:
    """See _bind_on_load. PERF: one checker-content digest for this load (claim_assessment)."""
    from runs.claim_assessment import checker_digest_scope
    with checker_digest_scope():
        return _bind_on_load(run, run_dir, refuse_invalid_artifact=refuse_invalid_artifact)


def _bind_on_load(run: dict, run_dir: Path, *, refuse_invalid_artifact: bool = False) -> dict:
    """Refresh unbound/stale row checks against their exact saved page bytes, in memory."""
    from runs import sources
    from runs.claim_assessment import check_current, checks_current
    from runs.claim_assessment import bind_contract_checks
    from runs.recheck import row_checks
    from runs.build_run import to_contract_check
    from checks.status import REQUIRED_CHECKS as REQUIRED_CELL_CHECKS, check_cell

    by_source = {source.get("id"): source for source in run.get("sources", [])}
    for row in run.get("rows", []):
        source = by_source.get(row.get("source_id"))
        if source is None:
            continue
        existing = row.get("checks") or []
        page = next((p for p in source.get("pages", [])
                     if p.get("page_number") == row.get("page_number")), None)
        if page is None:
            continue
        try:
            words, consumed = sources.load_page_evidence(Path(run_dir), page)
        except sources.SourcesError:
            if refuse_invalid_artifact:
                raise
            row["check_history"] = [*(row.get("check_history") or []), *existing]
            row["checks"] = []
            continue
        if checks_current(existing, row, source, consumed_artifact=consumed):
            continue
        results = row_checks(row, source, words, chosen=False, consumed_artifact=consumed)
        row["check_history"] = [*(row.get("check_history") or []), *existing]
        row["checks"] = [to_contract_check(result.to_dict()) for result in results]
    _bind_research_spans(run, Path(run_dir), by_source, refuse_invalid_artifact)
    for table in run.get("tables", []):
        source = by_source.get(table.get("source_id"))
        page = next((p for p in (source or {}).get("pages", [])
                     if p.get("page_number") == table.get("page_number")), None)
        if source is None or page is None:
            continue
        try:
            words, consumed = sources.load_page_evidence(Path(run_dir), page)
        except sources.SourcesError:
            if refuse_invalid_artifact:
                raise
            continue
        for row in table.get("rows", []):
            for cell in row.get("cells", []):
                context = {"page_number": table["page_number"], "value": cell.get("value"),
                           "box": cell.get("box"), "column": cell.get("column"),
                           "row_label": row.get("label"), "columns": table.get("columns", [])}
                existing = cell.get("checks") or []
                by_name = {check.get("name") or check.get("check"): check for check in existing}
                if all(name in by_name and check_current(by_name[name], context, source,
                                                         consumed_artifact=consumed)
                       for name in REQUIRED_CELL_CHECKS):
                    continue
                checked = check_cell(cell, words, row.get("label"), table.get("columns"),
                                     visual_evidence=page.get("visual_evidence"),
                                     derived_words_present=bool(page.get("derived_words")))
                if existing:
                    cell["check_history"] = [*(cell.get("check_history") or []), *existing]
                cell["checks"] = bind_contract_checks(checked["checks"], context, source,
                                                      consumed_artifact=consumed)
                cell["status"] = checked["status"]
    # CLAIM-001 / UNKNOWN-001: research labels are derived from the recorded review on every load
    from runs.research_labels import derive_review_labels
    derive_review_labels(run)
    return run


SPAN_RESULT_FIELDS = ("quote_match", "quote_match_reason", "printed_quote_match", "unknown_word_ids",
                      "capture_incomplete", "hidden_text")


def _bind_research_spans(run: dict, run_dir: Path, by_source: dict, refuse_invalid_artifact: bool) -> None:
    """CALENDAR-RULE-001 (Manus CALENDAR-NEW-001): a research row's recorded span checks describe the words they
    were run on. Re-run each span's check on the exact page words it consumes NOW (sources.load_page_evidence: the
    captured words, or a verified derived transcription) and, where a result differs, replace it in memory and keep
    the recorded one as that span's check_history: per span, like the row checks above. The state rule then reads
    current span support. A span whose page cannot be loaded is marked could_not_run (never a pass)."""
    from runs import sources
    from runs.research_contract import span_check

    for row in run.get("rows", []):
        research = row.get("research")
        if not isinstance(research, dict) or not isinstance(research.get("evidence"), list):
            continue
        for span in research["evidence"]:
            if not isinstance(span, dict) or not span.get("quote"):
                continue
            source = by_source.get(span.get("source_id")) or {}
            page = next((p for p in source.get("pages", []) if p.get("page_number") == span.get("page_number")), None)
            recorded = {k: span.get(k) for k in SPAN_RESULT_FIELDS}
            try:
                if page is None:
                    raise sources.SourcesError(f"{span.get('source_id')} has no page {span.get('page_number')}")
                words, consumed = sources.load_page_evidence(run_dir, page)
                current = {k: v for k, v in span_check({**span, "roles": span.get("roles") or []}, words, page).items()
                           if k in SPAN_RESULT_FIELDS}
            except sources.SourcesError as e:
                if refuse_invalid_artifact and page is not None:
                    raise
                consumed = None
                current = {**recorded, "quote_match": "could_not_run", "printed_quote_match": "could_not_run",
                           "quote_match_reason": f"The cited page could not be loaded: {e}"}
            if current != recorded:
                span["check_history"] = [*(span.get("check_history") or []), recorded]
                span.update(current)
                span["rechecked_on_load"] = {
                    "reason": "The words this span cites changed after it was checked; its check was run again on "
                              "the page as now captured.", "consumed": consumed}


def list_input_stamp(run_bytes: bytes, run: dict, decisions_bytes: bytes) -> str:
    """Key a run-list summary to every persisted input that can change its bound state."""
    from runs.claim_assessment import checker_version

    pages = []
    for source in run.get("sources", []):
        for page in source.get("pages", []):
            pages.append({"source_id": source.get("id"), "page_number": page.get("page_number"),
                          "image": page.get("image"), "image_sha256": page.get("image_sha256"),
                          "words": page.get("words"), "words_sha256": page.get("words_sha256"),
                          "derived_words": page.get("derived_words")})
    payload = {"run_sha256": hashlib.sha256(run_bytes).hexdigest(),
               "decisions_sha256": hashlib.sha256(decisions_bytes).hexdigest(),
               "checker_version": checker_version("selector_match"), "pages": pages}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_bound_run(run_dir: Path) -> dict:
    """Read run.json and refresh stale row checks from its captured artifacts."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    from runs.sources import verify_page_files
    verify_page_files(run_dir, run)
    return bind_on_load(run, run_dir, refuse_invalid_artifact=True)


def bind_decisions_on_load(run: dict, decisions: list[dict], run_dir: Path) -> list[dict]:
    """See _bind_decisions_on_load. PERF: one checker-content digest for this replay (claim_assessment)."""
    from runs.claim_assessment import checker_digest_scope
    with checker_digest_scope():
        return _bind_decisions_on_load(run, decisions, run_dir)


def _bind_decisions_on_load(run: dict, decisions: list[dict], run_dir: Path) -> list[dict]:
    """Refresh saved row re-checks from the exact stored page bytes, in memory.

    Decision lines stay append-only on disk. An old unbound re-check is history;
    when its fix is still pending in the replay prefix, recompute the before and
    after evidence from the captured page and bind that fresh result.
    """
    from runs import sources
    from runs.recheck import recheck_row
    from runs.state import current_state

    run_dir = Path(run_dir)
    refreshed = []
    rows = {row.get("id"): row for row in run.get("rows", [])}
    by_source = {source.get("id"): source for source in run.get("sources", [])}
    for original in decisions:
        line = dict(original)
        if line.get("action") == "recheck" and line.get("target") in rows:
            state = current_state(run, refreshed)
            target = state["targets"].get(line["target"], {})
            fixes = target.get("fixes") or []
            if target.get("needs_recheck") and fixes:
                fix = fixes[-1]
                row = rows[line["target"]]
                source = by_source.get(row.get("source_id"), {"kind": "other"})
                page = next((p for p in source.get("pages", [])
                             if p.get("page_number") == row.get("page_number")), None)
                words_doc, consumed = (None, None)
                if page is not None:
                    words_doc, consumed = sources.load_page_evidence(run_dir, page)
                elif source.get("pages"):
                    consumed = {"file": None, "sha256": None, "kind": "unavailable"}
                fixed = {**row, **{item["field"]: item["to"] for item in fixes}}
                before = {**fixed, fix["field"]: fix["from"]}
                line["recheck"] = recheck_row(
                    line.get("at", ""), fixed, source, words_doc, fix.get("settles", False),
                    field=fix["field"], before=before, consumed_artifact=consumed)["recheck"]
        refreshed.append(line)
    return refreshed
