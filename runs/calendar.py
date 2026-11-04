"""The calendar list: which event rows may be placed on the calendar, computed from the state rule.

CALENDAR-RULE-001 (CVA 2026-11-02): the one inclusion predicate is runs.review.review_projection. A row is eligible
for the interval partition (on_calendar) when a person kept it, or when it is a clean proposal: not excluded, no
issues, no reviewer findings. The partition then places it: in the run-anchored window -> the calendar (a clean
row there reads "Included — not reviewed by you", a kept one "Kept by you"); wholly past -> Context (past); after the
window -> Later; no date -> Undated: needs timing. A row with issues is not eligible until a person keeps it.
Mechanical checks and field fixes never grant semantic acceptance. An explicit Keep may accept a proposal with
failed or unresolved checks; those checks remain visible. An expired Keep does not admit the changed proposal (it
is an issue). Each entry carries the row's status and whether a person decided it, so the screen can show "Kept
by you" beside a failed check and never turns it into a tick, and never says "Kept by you" for automatic inclusion.
Values are the current ones (a person's fix applied). Rows with no exact day or period start are listed after the
dated ones, never dropped.
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


def research_placement(row: dict) -> str | None:
    """A research row's own placement proposal (upcoming, contingent, undated, context, later, unresolved); None
    for every other row."""
    research = row.get("research")
    return research.get("placement_proposal") if isinstance(research, dict) else None


# A research proposal that is not future-facing never lands on the calendar, even when its date is in the window:
# context -> Past; undated/unresolved with a date the code read -> Timing unresolved (never a day invented away).
_NOT_FUTURE_FACING = {"context": "past", "undated": "timing_unresolved", "unresolved": "timing_unresolved"}


def interval_bucket(row: dict, lower: date, upper: date) -> str:
    """THE placement of one row against the window [lower, upper]: calendar, past, later, contingent, undated or
    timing_unresolved. The period is tested on its END: a period that ends before the window starts is past; one
    that starts after the window ends is later; any intersection is in the window. Used by current_state (the
    inclusion predicate) and partition_intervals (the screen and reports), so both always agree."""
    try:
        start = date.fromisoformat(row.get("date") or "")
    except (TypeError, ValueError):
        start = None
    if start is None:
        if row.get("placement_proposal") == "context":
            return "past"       # a completed fact with no date is context, not an event that needs timing
        state = "contingent" if row.get("contingent") else (row.get("timing_state") or
                 ("undated" if not row.get("date_text") else "timing_unresolved"))
        return state if state in {"contingent", "undated", "timing_unresolved"} else "timing_unresolved"
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
        return "past"
    if start > upper:
        return "later"
    return _NOT_FUTURE_FACING.get(row.get("placement_proposal"), "calendar")


def run_window(run: dict) -> tuple[date, date] | None:
    """The six-month, run-anchored window; None when the run has no reference date (no wall-clock fallback)."""
    start = run_reference_date(run)
    return None if start is None else (start, add_months_to(start, 6))


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
            "semantic_status": s.get("semantic_status"), "inclusion": s.get("inclusion"), "placement": s.get("placement"),
            "issues": list(s.get("issues") or []), "placement_proposal": research_placement(row),
            "review_label": s.get("review_label"), "decision_label": s.get("decision_label"),
            "page_number": row["page_number"], "group": s["group"], "status": s["status"],
            "decided_by_person": s["decided_by_person"], "last_action": s["last_action"],
            "needs_recheck": s["needs_recheck"], "reason": s["reason"],
            # DUPLICATE-RESOLUTION-001 (runs.state._resolve_duplicates): the other copies of this event.
            "duplicate_of": s.get("duplicate_of"), "duplicate_state": s.get("duplicate_state"),
            "supporting_sources": list(s.get("supporting_sources") or []),
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
        out[interval_bucket(row, lower, upper)].append(row)
    return out


def run_interval_partition(run: dict, state: dict) -> tuple[dict[str, list[dict]], str | None, str | None]:
    """The six-month, run-anchored partition used by the screen and reports."""
    window = run_window(run)
    if window is None:       # no wall-clock fallback: placement is unresolved
        return {"calendar": [], "later": [], "past": [], "contingent": calendar_entries(run, state)}, None, None
    anchor, end = window[0].isoformat(), window[1].isoformat()
    return partition_intervals(calendar_entries(run, state), anchor, end), anchor, end
