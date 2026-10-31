"""Pure view of a saved run for the report. No files written; no network."""

from __future__ import annotations

import calendar as _calendar
import json
from datetime import date, datetime
from pathlib import Path

from runs.state import as_of_note, current_state, run_reference_date
from runs.calendar import on_calendar, run_interval_partition
from runs.state import _needs_attention
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
    run = json.loads((run_dir / "run.json").read_text())
    job_path = run_dir / "job.json"
    if job_path.is_file():
        job = json.loads(job_path.read_text())
        meta = run.setdefault("run", {})
        meta.setdefault("pages_read", len(run.get("sources", [])))
        if meta.get("status") == "done" and isinstance(job.get("skipped"), dict):
            meta.setdefault("pages_total", len(run.get("sources", [])) + len(job["skipped"]))
    dec_path = run_dir / "decisions.jsonl"
    decisions = []
    if dec_path.exists() and dec_path.stat().st_size:
        for line in dec_path.read_text().splitlines():
            line = line.strip()
            if line:
                decisions.append(json.loads(line))
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


def _period_word(start: str, end: str) -> str | None:
    """The word for a whole month, quarter, half or year ('Q1 2027'), or None when the period has no such name."""
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    if a.year != b.year or a.day != 1 or b.day != _calendar.monthrange(b.year, b.month)[1]:
        return None
    months = (a.month, b.month)
    if a.month == b.month:
        return f"{_calendar.month_name[a.month]} {a.year}"
    if months == (1, 12):
        return str(a.year)
    if months in ((1, 6), (7, 12)):
        return f"H{1 if a.month == 1 else 2} {a.year}"
    if b.month - a.month == 2 and a.month % 3 == 1:
        return f"Q{a.month // 3 + 1} {a.year}"
    return None


def _period_display(start: str | None, end: str) -> str:
    """A period with no stated text: first day to last day, led by its word when the code can name it."""
    if not start:
        return end
    span = f"{start} to {end}"
    word = _period_word(start, end)
    return f"{word} ({span})" if word else span


def _date_display(values: dict) -> str:
    """Period rows show the period as stated; never invent a day."""
    text = (values.get("date_text") or "").strip()
    if values.get("date_end"):
        return text or _period_display(values.get("date"), values["date_end"])
    if text:
        return text
    return values.get("date") or "—"


def _person_tag(st: dict) -> str | None:
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
    return [{"name": c.get("label") or c.get("name") or "Check",
             "result": c.get("result") or "unknown", "limitation": c.get("reason") or ""}
            for c in checks]


def _check_status_text(state: dict) -> str | None:
    if state.get("needs_recheck"):
        return "Re-check pending; no checks are recorded for the current value."
    if (state.get("recheck") or {}).get("result") == "could_not_run":
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
    read_source_ids = [s.get("id", "").lower() for s in run.get("sources", [])]
    companies_read = [ticker for ticker in companies
                      if any(sid.startswith("src-" + ticker.lower() + "-") for sid in read_source_ids)]
    companies_unread = [ticker for ticker in companies if ticker not in companies_read]
    pages_total = meta.get("pages_total")
    pages_read = meta.get("pages_read") if isinstance(meta.get("pages_read"), int) else len(run.get("sources", []))
    pages_unread = max(0, pages_total - pages_read) if isinstance(pages_total, int) else None
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
        if st["kind"] != "row" or not on_calendar(st):
            continue
        row = rows_raw[rid]
        values = st["values"]
        src = sources.get(row.get("source_id"))
        effective_checks = st.get("effective_checks") or []
        check_history = st.get("check_history") or []
        check_status_text = _check_status_text(st)
        calendar.append({
            "id": rid,
            "date_display": _date_display(values),
            "sort_key": _calendar_date_key(values),
            "ticker": values.get("ticker") or row.get("ticker") or "",
            "program": _value(values, row, "program") or "",
            "event": _event_label(row, values),
            "source": _source_line(src),
            "status": st["status"],
            "group": st["group"],
            "group_label": GROUP_LABEL[st["group"]],
            "person_tag": _person_tag(st),
            "bucket": bucket_for_id.get(rid, "calendar"),
            "indication": _value(values, row, "indication") or "",
            "action": _value(values, row, "event_type") or "unknown",
            "source_document": (src or {}).get("title") or (src or {}).get("publisher") or row.get("source_id") or "—",
            "source_id": row.get("source_id") or "—",
            "page_number": row.get("page_number"),
            "quote": _value(values, row, "quote") or "",
            "check_limitations": _check_rows(effective_checks),
            "check_history": _check_rows(check_history),
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
        window_text = "Six months from the run date"
    past = [r for r in calendar if r["bucket"] == "past"]
    later = [r for r in calendar if r["bucket"] == "later"]
    contingent = [r for r in calendar if r["bucket"] == "contingent"]
    calendar = [r for r in calendar if r["bucket"] == "calendar"]
    for row in contingent:
        if row["date_display"] == "—":
            row["date_display"] = "Date not stated"

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
            "ticker": (st.get("values") or {}).get("ticker") or row.get("ticker") or "",
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
                "ticker": (st.get("values") or {}).get("ticker") or rows_raw.get(rid, {}).get("ticker") or "",
                "event": _event_label(rows_raw.get(rid, {}), st.get("values") or {}),
                "what": line,
                "caught_by": _caught_by_label("second_reader" if fx.get("second_reader_value") else "person"),
                "check": check,
                "before_after": line,
                "latest_recheck": rc.get("result") or "not run",
                "override_reason": (st.get("reason") or "Kept by you") if st.get("decided_by_person") else None,
            }
            if fx.get("mistake"):
                if not any(c["id"] == rid and (line in (c.get("what") or "") or said in (c.get("what") or ""))
                           for c in corrections):
                    corrections.append(entry)
            else:
                if fx.get("edit_without_error"):
                    entry["edit_status"] = "The old value passed its checks; no mistake was demonstrated."
                elif fx.get("before_undemonstrated"):
                    entry["edit_status"] = "The old value could not be assessed; no mistake was demonstrated."
                elif not rc or rc.get("result") == "could_not_run":
                    entry["edit_status"] = "This change was not demonstrated to be a mistake."
                else:
                    entry["edit_status"] = "The checks did not demonstrate a mistake."
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
        if st["kind"] != "row" or not _needs_attention(st):
            continue
        row = rows_raw[rid]
        open_rows.append({
            "id": rid,
            "group": st["group"],
            "group_label": GROUP_LABEL[st["group"]],
            "ticker": st["values"].get("ticker") or row.get("ticker") or "",
            "event": _event_label(row, st["values"]),
            "reason": st.get("reason") or row.get("reason") or "No reason recorded.",
            "latest_recheck": (st.get("recheck") or {}).get("result"),
            "override_reason": st.get("reason") if st.get("decided_by_person") else None,
            "check_limitations": _check_rows(st.get("effective_checks") or []),
            "check_history": _check_rows(st.get("check_history") or []),
            "check_status_text": _check_status_text(st),
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
        })

    return {
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
        "companies": companies,
        "generated_at": built_on_words(generated_at),
        "counts": {g: counts[g] for g in GROUP_LABEL},
        "group_labels": GROUP_LABEL,
        "mistakes": mistakes,
        "excluded_total": excluded_n,
        "unresolved": state["unresolved"],
        "counters_line": (
            f"Mistakes caught by code {mistakes['code']} · by the second reader "
            f"{mistakes['second_reader']} · by a person {mistakes['person']} · Excluded {excluded_n}"
        ),
        "calendar": calendar,
        "calendar_window": window_text if window_start is None else f"{window_anchor} – {window_end.isoformat()}",
        "past": past,
        "later": later,
        "contingent": contingent,
        "corrections": corrections,
        "edits": edits,
        "exclusions": exclusions,
        "open_rows": open_rows,
        "sources": source_rows,
        "state": state,
    }
