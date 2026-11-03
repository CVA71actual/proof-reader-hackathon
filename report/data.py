"""Pure view of a saved run for the report. No files written; no network."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from runs.state import UNRESOLVED_REASON, as_of_note, current_state, run_reference_date
from runs.calendar import period_display, period_word, run_interval_partition, stated_date_display
from runs.proposal import context_fields
from runs.one_model import NO_SECOND_READER, ONE_MODEL_LINE, run_is_one_model
from runs.check_load import bind_decisions_on_load, load_bound_run
from checks import event_checks

TITLE = "Biotech Coverage Catalyst Calendar"
FOOTER = "Built with Meta Muse models · built by code from the saved results"
SAMPLE_STAMP = "Sample data — not real events"

GROUP_LABEL = {
    "kept": "Checked and kept",
    "corrected": "Corrected",
    "removed": "Checked and removed",
    "unverified": "Unverified",
    "open": "Still open",
}

RULE_WORDS = {
    "third_party_calendar_only": "House rule: a third-party calendar is never a source on its own.",
    "event_in_past": "House rule: the event date is already in the past.",
    "person_removed": "Removed by you.",
}

# Status words that are allowed on the report. Never: verified, proved, correct (alone).
ALLOWED_STATUS_WORDS = ("matched", "proposed", "failed", "unreadable", "undecided")

BANNED_WORDS = ("verified", "proved")  # "correct" checked as a whole word separately


def load_run_dir(run_dir: Path) -> tuple[dict, list[dict]]:
    run = load_bound_run(run_dir)
    dec_path = run_dir / "decisions.jsonl"
    decisions = []
    if dec_path.exists() and dec_path.stat().st_size:
        for line in dec_path.read_text().splitlines():
            line = line.strip()
            if line:
                decisions.append(json.loads(line))
    decisions = bind_decisions_on_load(run, decisions, run_dir)
    return run, decisions


def report_filenames(run_number: int) -> tuple[str, str]:
    base = f"biotech-coverage-catalyst-calendar-run{run_number}"
    return f"{base}.pdf", f"{base}.docx"


def _sources_by_id(run: dict) -> dict:
    return {s["id"]: s for s in run.get("sources", [])}


def _row_by_id(run: dict) -> dict:
    return {r["id"]: r for r in run.get("rows", [])}


def _calendar_date_key(values: dict) -> tuple:
    """Sort key: exact day, then period start, then undated last."""
    d = values.get("date") or "9999-99-99"
    end = values.get("date_end") or ""
    return (d, end, values.get("date_text") or "")


# The date display is derived once in runs.state (from runs.calendar) and
# carried on each row target and calendar entry; these names stay for callers.
_period_word = period_word
_period_display = period_display
_date_display = stated_date_display


def _person_tag(st: dict) -> str | None:
    if "decision_label" in st:
        return st["decision_label"]
    if not st.get("decided_by_person"):
        return None
    action = st.get("last_action")
    if action == "keep":
        return "Kept by you"
    if action == "fix":
        return "Fixed by you"
    return None


def _source_line(src: dict | None) -> str:
    if not src:
        return "—"
    parts = [src.get("publisher") or src.get("title") or src["id"]]
    if src.get("rendered_from"):
        parts.append("page rendered by code from the filed HTML")
    return " · ".join(parts)


def _value(values: dict, row: dict, field: str):
    """The current value of a field (a person's fix applied); the raw row's only when the state has none."""
    return values[field] if field in values else row.get(field)


def _event_label(row: dict, values: dict) -> str:
    return _value(values, row, "title") or ""


def _caught_by_label(who: str) -> str:
    return {"code": "code", "second_reader": "second reader", "person": "person"}.get(who, who)


def _check_rows(checks: list[dict]) -> list[dict]:
    rows = []
    for c in checks:
        row = {"name": c.get("label") or c.get("name") or "Check",
               "check_name": c.get("name") or c.get("check"),
               "result": c.get("result") or "unknown", "limitation": c.get("reason") or ""}
        for key in ("kind", "compared", "near_match", "differs", "assessment", "code_ref"):
            if key in c:
                row[key] = c[key]
        rows.append(row)
    return rows


def _effective_check_rows(state: dict) -> list[dict]:
    out = _check_rows(state.get("effective_checks") or [])
    claim = (state.get("claim_assessments") or {}).get("visible_passage")
    if claim:
        out.append({"name": claim["label"], "result": claim["result"], "limitation": claim["reason"]})
    return out


def _page_evidence_note(evidence: dict | None) -> str | None:
    evidence = evidence or {}
    capture = evidence.get("capture_completeness") or {}
    if capture.get("capture_incomplete"):
        note = "Capture may be incomplete: " + (capture.get("capture_incomplete_reason") or "reason not recorded")
    elif capture.get("completeness_not_established"):
        note = "Capture completeness was not established: " + (capture.get("completeness_reason") or "reason not recorded")
    else:
        note = None
    if evidence.get("derived_words"):
        transcription = "Words came from a model transcription linked to the captured page image; geometry is approximate."
        note = f"{note} {transcription}" if note else transcription
    return note


def _check_status_text(state: dict) -> str | None:
    claim = (state.get("claim_assessments") or {}).get("visible_passage")
    if claim and claim.get("result") == "not_checked":
        if state.get("checks_stale"):
            return ("Checks are not current for this candidate or page, and visible words have not been checked "
                    "against rendered page ink.")
        return "Visible words have not been checked against rendered page ink."
    if state.get("needs_recheck"):
        return "Re-check pending; no checks are recorded for the current value."
    rc_result = (state.get("recheck") or {}).get("result")
    if rc_result == "unresolved":
        return "Re-check unresolved; the row remains unresolved."
    if rc_result == "could_not_run":
        if state.get("effective_checks"):
            return "Re-check incomplete; the checks below ran on the current value, and the row remains unresolved."
        return "Re-check could not run; no checks are recorded for the current value."
    if state.get("fixes") and not state.get("effective_checks"):
        return "No checks are recorded for the current value."
    return None


def built_on_words(when: str) -> str:
    """'Built on 29 October 2026 at 17:46 (local time)' from an ISO time (a trailing Z means UTC)."""
    try:
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return when
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return f"Built on {dt.day} {dt:%B %Y} at {dt:%H:%M} (local time)"


def date_in_words(iso: str) -> str:
    """'28 October 2026' from 'YYYY-MM-DD' (or a longer ISO time); anything else comes back as given."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return f"{d.day} {d:%B %Y}"


def _research_lines(meta: dict, run: dict) -> tuple[str | None, list[str]]:
    """A research run's label and its per-company outcome, recorded limitations, searched-but-not-found notes,
    capture failures and source capture limitations, from run.research as the job saved it."""
    research = meta.get("research")
    if not research:
        return None, []
    lines = []
    for ticker, c in (research.get("companies") or {}).items():
        lines.append(f"{ticker}: {c.get('status')} — {c.get('status_reason')} Sources captured {c.get('sources', 0)}, "
                     f"proposals {c.get('proposals', 0)}, model requests {c.get('requests', 0)}, searches "
                     f"{c.get('searches', 0)}.")
        if c.get("limitation"):
            lines.append(f"{ticker} limitation: {c['limitation']['reason']} {c['limitation'].get('not_checked') or ''}".strip())
        for lim in (c.get("finish") or {}).get("limitations") or []:
            lines.append(f"{ticker} limitation stated by the model: {lim.get('question')} — {lim.get('reason')}")
        for f in c.get("capture_failures") or []:
            lines.append(f"{ticker} capture failed: {f.get('url')} — {f.get('reason')}")
    for cov in research.get("coverage") or []:
        if cov.get("searched_not_found"):
            when = ", ".join(cov.get("retrieval_times") or []) or "no retrieval time recorded"
            lines.append(f"{cov.get('ticker')} searched, not found ({cov.get('category')}): {cov.get('question')} "
                         f"— retrieved {when}.")
    for src in run.get("sources", []):
        lim = ((src.get("retrieval") or {}).get("rendered") or {}).get("limitations") or []
        if lim:
            lines.append(f"{src['id']} capture limitations: " + "; ".join(lim) + ".")
    if (research.get("support_check") or {}).get("label"):
        lines.append(research["support_check"]["label"] + ".")
    for k in research.get("kill_conditions") or []:
        lines.append(f"Kill condition met: {k.get('detail')}")
    return research.get("label"), lines


def build_view(run: dict, decisions: list[dict], *, generated_at: str) -> dict:
    """Assemble every section the PDF and Word writers share."""
    state = current_state(run, decisions)
    meta = run["run"]
    sources = _sources_by_id(run)
    rows_raw = _row_by_id(run)

    counts = state["counts"]["rows"]
    mistakes = state["mistakes"]
    excluded_n = state["excluded"]["code"] + state["excluded"]["person"]

    companies = list(meta.get("choices", {}).get("companies") or [])
    if not companies:
        companies = sorted({r.get("ticker") for r in run.get("rows", []) if r.get("ticker")})
    outcomes_view = state["page_outcomes"]
    outcomes_recorded = outcomes_view["recorded"]
    outcome_counts = outcomes_view["counts"]
    per_company_outcomes = outcomes_view["by_company"]
    companies_read = [x["ticker"] for x in per_company_outcomes if x["read"]]
    companies_unread = [x["ticker"] for x in per_company_outcomes if x["unread"] == x["pages"]]
    pages_read = outcome_counts["read"] if outcome_counts is not None else None
    pages_unread = outcome_counts["unread"] if outcome_counts is not None else None
    status = meta.get("status") or ("done" if meta.get("finished_at") else "unknown")
    status_label = {
        "done": "Done",
        "paused_turn_limit": "Paused at the turn limit",
        "needs_input": "Paused; needs input",
        "failed": "Stopped after an error",
        "running": "Still running",
        "queued": "Queued",
    }.get(status, status)

    interval_rows, window_anchor, window_end = run_interval_partition(run, state)
    bucket_for_id = {entry["id"]: bucket for bucket, entries in interval_rows.items() for entry in entries}
    calendar = []
    for rid, st in state["targets"].items():
        if st["kind"] != "row" or not st.get("on_calendar"):
            continue
        row = rows_raw[rid]
        values = st["values"]
        src = sources.get(row.get("source_id"))
        effective_checks = st.get("effective_checks") or []
        check_history = st.get("check_history") or []
        check_status_text = _check_status_text(st)
        calendar.append({
            **context_fields(values),
            "type_presentation": st["type_presentation"],
            "id": rid,
            "date_display": st["date_display"],
            "date_text_cleared": st["date_text_cleared"],
            "sort_key": _calendar_date_key(values),
            "ticker": _value(values, row, "ticker") or "",
            "program": _value(values, row, "program") or "",
            "event": _event_label(row, values),
            "source": _source_line(src),
            "status": st["status"],
            "group": st["group"],
            "group_label": st["review_label"],
            "semantic_status": st["semantic_status"],
            "review_label": st["review_label"],
            "decision_label": st["decision_label"],
            "person_tag": _person_tag(st),
            "bucket": bucket_for_id.get(rid, "calendar"),
            "indication": _value(values, row, "indication") or "",
            "action": st["type_presentation"]["summary"],
            "source_document": (src or {}).get("title") or (src or {}).get("publisher") or row.get("source_id") or "—",
            "source_id": row.get("source_id") or "—",
            "page_number": row.get("page_number"),
            "page_evidence": st.get("page_evidence"),
            "page_evidence_note": _page_evidence_note(st.get("page_evidence")),
            "timing_state": st.get("timing_state"),
            "contingent": st.get("contingent"),
            "quote": _value(values, row, "quote") or "",
            "check_limitations": _effective_check_rows(st),
            "check_history": _check_rows(check_history),
            "check_history_label": ("Saved checks · history (not current)" if st.get("checks_stale")
                                    or any(c.get("assessment") is None for c in st.get("check_history") or [])
                                    else "Original checks · history (before edit)"),
            "check_status_text": check_status_text,
            "reason": st.get("reason") or row.get("reason") or "",
        })
    calendar.sort(key=lambda r: (r["sort_key"], r["ticker"], r["id"]))
    try:
        window_start = run_reference_date(run)
        if window_start is None:
            raise ValueError
        # The calendar and saved report always use the same six-month horizon.
        months = 6
        window_end = event_checks.add_months_to(window_start, months)
        window_text = f"{window_start.isoformat()} – {window_end.isoformat()}"
    except (TypeError, ValueError):
        window_start = window_end = None
        window_text = UNRESOLVED_REASON
    past = [r for r in calendar if r["bucket"] == "past"]
    later = [r for r in calendar if r["bucket"] == "later"]
    contingent = [r for r in calendar if r["bucket"] == "contingent"]
    undated = [r for r in calendar if r["bucket"] == "undated"]
    timing_unresolved = [r for r in calendar if r["bucket"] == "timing_unresolved"]
    calendar = [r for r in calendar if r["bucket"] == "calendar"]
    for row in undated:
        row["date_display"] = "Undated · no timing stated"

    corrections = []
    for m in run.get("mistakes", []):
        rid = m.get("target")
        st = state["targets"].get(rid)
        if not st or st["kind"] != "row":
            continue
        row = rows_raw.get(rid, {})
        check_name = None
        for c in row.get("checks") or []:
            if c.get("result") == "fail":
                check_name = c.get("label") or c.get("name")
                break
        corrections.append({
            "id": rid,
            "ticker": _value(st.get("values") or {}, row, "ticker") or "",
            "event": _event_label(row, st.get("values") or {}),
            "what": m.get("what_changed") or "",
            "caught_by": _caught_by_label(m.get("caught_by") or ""),
            "check": check_name,
            "before_after": m.get("what_changed") or "",
            "latest_recheck": "not applicable",
            "override_reason": None,
        })
    edits = []
    # A changed value is a correction only when the saved before/after re-check
    # demonstrates a mistake. Keep ordinary and unassessed edits in a separate log.
    for rid, st in state["targets"].items():
        if st["kind"] != "row":
            continue
        for fx in st.get("fixes") or []:
            line = f"{fx['field']}: {fx['from']} → {fx['to']}"
            said = f"{fx['field']} changed from {fx['from']} to {fx['to']}"
            check = None
            rc = fx.get("recheck") or {}
            for c in (rc.get("before") or {}).get("checks") or []:
                if c.get("result") == "fail":
                    check = c.get("label") or c.get("name")
                    break
            entry = {
                "id": rid,
                "ticker": _value(st.get("values") or {}, rows_raw.get(rid, {}), "ticker") or "",
                "event": _event_label(rows_raw.get(rid, {}), st.get("values") or {}),
                "what": line,
                "caught_by": _caught_by_label("second_reader" if fx.get("second_reader_value") else "person"),
                "check": check,
                "before_after": line,
                "latest_recheck": rc.get("result") or "not run",
                "override_reason": (st.get("reason") or "Kept by you") if st.get("accepted") else None,
            }
            if fx.get("mistake"):
                if not any(c["id"] == rid and (line in (c.get("what") or "") or said in (c.get("what") or ""))
                           for c in corrections):
                    corrections.append(entry)
            else:
                # A human edit can still belong in the visible chronology
                # even when the checks cannot establish that it corrected a
                # model error. Keep the explicit qualification on the entry;
                # do not count it in accuracy.
                if fx.get("edit_without_error"):
                    entry["edit_status"] = "The old value passed its checks; no mistake was demonstrated."
                elif fx.get("before_undemonstrated"):
                    entry["edit_status"] = "The old value could not be assessed; no mistake was demonstrated."
                elif not rc or rc.get("result") in ("could_not_run", "unresolved"):
                    entry["edit_status"] = "This change was not demonstrated to be a mistake."
                else:
                    entry["edit_status"] = "The checks did not demonstrate a mistake."
                # Keep a later human edit beside an already recorded mistake on
                # the same row. It is chronology, not another counted mistake;
                # unrelated or unassessed edits stay only in the edits section.
                if any(c["id"] == rid for c in corrections) and not any(
                        c["id"] == rid and line in (c.get("what") or "") for c in corrections):
                    corrections.append(entry)
                edits.append(entry)

    exclusions = []
    for x in state["exclusions"]:
        rule = x.get("rule") or ""
        exclusions.append({
            "id": x.get("target"),
            "rule": rule,
            "rule_words": RULE_WORDS.get(rule) or x.get("reason") or rule,
            "reason": x.get("reason") or "",
            "by": x.get("by"),
        })

    open_rows = []
    for rid, st in state["targets"].items():
        if st["kind"] != "row" or not st.get("needs_attention"):
            continue
        row = rows_raw[rid]
        src = sources.get(row.get("source_id"))
        open_rows.append({
            **context_fields(st["values"]),
            "type_presentation": st["type_presentation"],
            "action": st["type_presentation"]["summary"],
            "id": rid,
            "group": st["group"],
            "group_label": st["review_label"],
            "semantic_status": st["semantic_status"],
            "review_label": st["review_label"],
            "decision_label": st["decision_label"],
            "person_tag": _person_tag(st),
            "ticker": _value(st["values"], row, "ticker") or "",
            "company": _value(st["values"], row, "company") or "",
            "program": _value(st["values"], row, "program") or "",
            "indication": _value(st["values"], row, "indication") or "",
            "event": _event_label(row, st["values"]),
            "date_text": _value(st["values"], row, "date_text") or "",
            "date_display": st["date_display"],
            "date_text_cleared": st["date_text_cleared"],
            "source_document": (src or {}).get("title") or (src or {}).get("publisher") or row.get("source_id") or "—",
            "source_id": row.get("source_id") or "—",
            "page_number": row.get("page_number"),
            "page_evidence": st.get("page_evidence"),
            "page_evidence_note": _page_evidence_note(st.get("page_evidence")),
            "quote": _value(st["values"], row, "quote") or "",
            "reason": st.get("reason") or row.get("reason") or "No reason recorded.",
            "latest_recheck": (st.get("recheck") or {}).get("result"),
            "override_reason": st.get("reason") if st.get("accepted") else None,
            "check_limitations": _effective_check_rows(st),
            "check_history": _check_rows(st.get("check_history") or []),
            "check_history_label": ("Saved checks · history (not current)" if st.get("checks_stale")
                                    or any(c.get("assessment") is None for c in st.get("check_history") or [])
                                    else "Original checks · history (before edit)"),
            "check_status_text": _check_status_text(st),
            "timing_state": st.get("timing_state"),
            "contingent": st.get("contingent"),
        })
    open_rows.sort(key=lambda r: (r["group"], r["ticker"], r["id"]))

    source_rows = []
    for src in run.get("sources", []):
        rf = src.get("rendered_from")
        source_rows.append({
            "id": src["id"],
            "publisher": src.get("publisher") or "",
            "kind": src.get("kind") or "",
            "date": src.get("published_date") or "",
            "url": src.get("url") or "",
            "sha256": src.get("sha256") or "",
            "rendered_from_sha256": (rf or {}).get("sha256"),
            "renderer": (rf or {}).get("renderer"),
            "rendered_note": (
                "page rendered by code from the filed HTML"
                if rf else None
            ),
            "retrieved_at": (src.get("retrieval") or {}).get("retrieved_at"),
            "original_sha256": (src.get("retrieval") or {}).get("original_sha256"),
        })

    research_label, research_lines = _research_lines(meta, run)
    return {
        "research_label": research_label,
        "research_lines": research_lines,
        "title": TITLE,
        "footer": FOOTER,
        "run_id": meta["id"],
        "run_number": meta["number"],
        "run_label": meta.get("label") or meta["id"],
        "sample": bool(meta.get("sample")),
        "sample_stamp": SAMPLE_STAMP if meta.get("sample") else None,
        "as_of_note": as_of_note(run),
        "run_date": date_in_words(meta.get("finished_at") or meta.get("started_at") or ""),
        "run_status": status_label,
        "stopping_reason": meta.get("status_reason") or "No stopping reason recorded.",
        "companies_read": companies_read,
        "companies_unread": companies_unread,
        "pages_read": pages_read,
        "pages_unread": pages_unread,
        "page_outcomes_recorded": outcomes_recorded,
        "page_outcome_counts": outcome_counts,
        "page_outcomes_by_company": per_company_outcomes,
        "companies": companies,
        "generated_at": built_on_words(generated_at),
        "counts": dict(state["counts"]["review"]),
        "group_labels": {"accepted": "Accepted by you", "proposed": "Proposed — needs review", "excluded": "Excluded"},
        "mechanical_counts": {g: counts[g] for g in GROUP_LABEL},
        "mistakes": mistakes,
        "excluded_total": excluded_n,
        "unresolved": state["unresolved"],
        # ONE-MODEL-RUN-001: a run with no second reader never shows a second-reader count of 0
        "models_line": ONE_MODEL_LINE if run_is_one_model(run) else None,
        "counters_line": (
            f"Mistakes caught by code {mistakes['code']} · "
            + (NO_SECOND_READER if run_is_one_model(run) else f"by the second reader {mistakes['second_reader']}")
            + f" · by a person {mistakes['person']} · Excluded {excluded_n}"
        ),
        "no_accepted_rows_text": "No rows accepted by you." if state["counts"]["review"]["accepted"] == 0 else None,
        "calendar": calendar,
        "calendar_window": window_text if window_start is None else f"{window_anchor} – {window_end.isoformat()}",
        "past": past,
        "later": later,
        "contingent": contingent,
        "undated": undated,
        "timing_unresolved": timing_unresolved,
        "corrections": corrections,
        "edits": edits,
        "exclusions": exclusions,
        "open_rows": open_rows,
        "sources": source_rows,
        "state": state,
    }
