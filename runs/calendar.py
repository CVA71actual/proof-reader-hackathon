"""The calendar list: which event rows may be placed on the calendar, computed from the state rule.

A row reaches the calendar only after an explicit human Keep of its effective proposal.
Mechanical checks and field fixes never grant semantic acceptance (on_calendar).
An explicit Keep may accept a proposal with failed or unresolved checks; those
checks remain visible. An expired Keep does not admit the changed proposal. Each entry carries the
row's status and whether a person decided it, so the screen can show "Kept by you" beside a failed check and
never turns it into a tick. Values are the current ones (a person's fix applied). Rows with no exact day or
period start are listed after the dated ones, never dropped.
"""

from __future__ import annotations

import calendar
from datetime import date
import re

from checks.event_checks import add_months_to, period_last_day
from runs.state import run_reference_date
from runs.proposal import context_fields

_DEPENDENCY = re.compile(r"\b(?:if|unless|subject to|contingent on|dependent on|provided that|only if|pending)\b", re.I)


def period_word(start: str, end: str) -> str | None:
    """The word for a whole month, quarter, half or year ('Q1 2027'), or None when the period has no such name."""
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    if a.year != b.year or a.day != 1 or b.day != calendar.monthrange(b.year, b.month)[1]:
        return None
    months = (a.month, b.month)
    if a.month == b.month:
        return f"{calendar.month_name[a.month]} {a.year}"
    if months == (1, 12):
        return str(a.year)
    if months in ((1, 6), (7, 12)):
        return f"H{1 if a.month == 1 else 2} {a.year}"
    if b.month - a.month == 2 and a.month % 3 == 1:
        return f"Q{a.month // 3 + 1} {a.year}"
    return None


def period_display(start: str | None, end: str) -> str:
    """A period with no stated text: first day to last day, led by its word when the code can name it."""
    if not start:
        return end
    span = f"{start} to {end}"
    word = period_word(start, end)
    return f"{word} ({span})" if word else span


def stated_date_display(values: dict) -> str:
    """Period rows show the period as stated; never invent a day."""
    text = (values.get("date_text") or "").strip()
    if values.get("date_end"):
        return text or period_display(values.get("date"), values["date_end"])
    if text:
        return text
    return values.get("date") or "—"


def effective_date_display(values: dict, timing: str | None) -> str:
    """The one date display for a row, from its complete effective date tuple
    (date_text, date, date_end, timing_state). A cleared date_text with the
    structured period kept shows that period, never the cleared wording."""
    shown = stated_date_display(values)
    if shown != "—":
        return shown
    return "Timing unresolved" if timing == "timing_unresolved" else "Undated · no timing stated"


def date_text_cleared(values: dict, fixes: list[dict]) -> bool:
    """True when a person's active fix explicitly cleared the date wording (null or '')."""
    return (values.get("date_text") in (None, "")
            and any(fx.get("field") == "date_text" and bool(str(fx.get("from") or "").strip())
                    for fx in fixes or []))


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
            **context_fields(v),
            "type_presentation": s["type_presentation"],
            "id": row["id"], "ticker": v["ticker"], "company": v["company"], "event_type": v["event_type"],
            "title": v["title"], "program": v["program"], "date_text": v["date_text"], "date": v["date"],
            "date_end": v["date_end"], "quote": v["quote"], "source_id": row["source_id"],
            "date_display": s.get("date_display"), "date_text_cleared": s.get("date_text_cleared"),
            "timing_state": s.get("timing_state"), "contingent": s.get("contingent"),
            "assessment_status": s.get("assessment_status"),
            "semantic_status": s.get("semantic_status"),
            "review_label": s.get("review_label"), "decision_label": s.get("decision_label"),
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
