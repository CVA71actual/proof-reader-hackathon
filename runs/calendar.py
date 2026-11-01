"""The calendar list: which event rows may be placed on the calendar, computed from the state rule.

A row reaches the calendar only when runs/state.py current_state puts it in group "kept", or in group "corrected"
with a passed re-check (on_calendar). A row that is removed, unverified or still open never does, nor does a
corrected row whose fix is not yet re-checked, failed or could not be re-checked, or whose readers are still undecided, whatever its first status said. Each entry carries the
row's status and whether a person decided it, so the screen can show "Kept by you" beside a failed check and
never turns it into a tick. Values are the current ones (a person's fix applied). Rows with no exact day or
period start are listed after the dated ones, never dropped.
"""

from __future__ import annotations

from datetime import date
import re

from checks.event_checks import add_months_to, period_last_day
from runs.state import run_reference_date

_DEPENDENCY = re.compile(r"\b(?:if|unless|subject to|contingent on|dependent on|provided that|only if|pending)\b", re.I)


def explicit_dependency(row: dict) -> bool:
    return bool(_DEPENDENCY.search(row.get("quote") or ""))


def timing_state(row: dict) -> str:
    """Describe date wording; conditionality is independent and recorded separately."""
    text = (row.get("date_text") or "").strip()
    if not text and not row.get("date") and not row.get("date_end"):
        return "undated"
    if not row.get("date") and not row.get("date_end"):
        return "timing_unresolved"
    return "dated"


def on_calendar(state: dict) -> bool:
    """Compatibility view of the single placement decision made by current_state."""
    return bool(state.get("on_calendar"))


def calendar_entries(run: dict, state: dict) -> list[dict]:
    out = []
    for row in run.get("rows", []):
        s = state["targets"].get(row["id"])
        if s is None or not s.get("on_calendar"):
            continue
        v = {**row, **s["values"]}      # the state's current values; the raw row only where the state has none
        out.append({
            "id": row["id"], "ticker": v["ticker"], "company": v["company"], "event_type": v["event_type"],
            "title": v["title"], "program": v["program"], "date_text": v["date_text"], "date": v["date"],
            "date_end": v["date_end"], "quote": v["quote"], "source_id": row["source_id"],
            "timing_state": s.get("timing_state"), "contingent": s.get("contingent"),
            "assessment_status": s.get("assessment_status"),
            "page_number": row["page_number"], "group": s["group"], "status": s["status"],
            "decided_by_person": s["decided_by_person"], "last_action": s["last_action"],
            "needs_recheck": s["needs_recheck"], "reason": s["reason"],
        })
    out.sort(key=lambda e: (e["date"] is None, e["date"] or "", e["id"]))
    return out


def partition_intervals(entries: list[dict], anchor: str, end: str) -> dict[str, list[dict]]:
    """Partition eligible calendar rows once for both the API/UI and saved reports.

    Intersecting periods stay in-window. Historical rows are retained in Past,
    including human-kept rows. Missing dates remain Undated, unresolved date wording stays distinct, and Contingent
    requires explicit dependency wording in the quoted passage.
    """
    lower, upper = date.fromisoformat(anchor), date.fromisoformat(end)
    out = {key: [] for key in ("calendar", "past", "later", "contingent", "undated", "timing_unresolved")}
    for row in entries:
        try:
            start = date.fromisoformat(row.get("date") or "")
        except (TypeError, ValueError):
            start = None
        if start is None:
            state = "contingent" if row.get("contingent") else (row.get("timing_state") or
                     ("undated" if not row.get("date_text") else "timing_unresolved"))
            out[state if state in {"contingent", "undated", "timing_unresolved"} else "timing_unresolved"].append(row)
            continue
        try:
            finish_value = row.get("date_end")
            if not finish_value:
                period_end = period_last_day(row)
                finish_value = period_end.isoformat() if period_end else row.get("date")
            finish = date.fromisoformat(finish_value or "")
        except (TypeError, ValueError):
            finish = start
        if finish < start:
            finish = start
        if finish < lower:
            out["past"].append(row)
        elif start > upper:
            out["later"].append(row)
        else:
            out["calendar"].append(row)
    return out


def run_interval_partition(run: dict, state: dict) -> tuple[dict[str, list[dict]], str | None, str | None]:
    """The six-month, run-anchored partition used by the screen and reports."""
    start = run_reference_date(run)
    if start is None:       # no wall-clock fallback: placement is unresolved
        return {"calendar": [], "later": [], "past": [], "contingent": calendar_entries(run, state)}, None, None
    anchor = start.isoformat()
    end = add_months_to(start, 6).isoformat()
    return partition_intervals(calendar_entries(run, state), anchor, end), anchor, end
