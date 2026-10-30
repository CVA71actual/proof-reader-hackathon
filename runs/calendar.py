"""The calendar list: which event rows may be placed on the calendar, computed from the state rule.

A row reaches the calendar only when runs/state.py current_state puts it in group "kept", or in group "corrected"
with a passed re-check (on_calendar). A row that is removed, unverified or still open never does, nor does a
corrected row whose fix is not yet re-checked, failed or could not be re-checked, or whose readers are still undecided, whatever its first status said. Each entry carries the
row's status and whether a person decided it, so the screen can show "Kept by you" beside a failed check and
never turns it into a tick. Values are the current ones (a person's fix applied). Rows with no exact day or
period start are listed after the dated ones, never dropped.
"""

from __future__ import annotations

CALENDAR_GROUPS = ("kept", "corrected")


def on_calendar(s: dict) -> bool:
    """A kept row is on the calendar. A row a person corrected is on it only when the re-check of the fix passed
    and the readers' disagreement, if there was one, is settled; a fix that is "fixed but still open" (not yet
    re-checked, re-check failed or could not run, readers still undecided) stays off until the person keeps it."""
    if s["group"] not in CALENDAR_GROUPS:
        return False
    if s["group"] == "kept" or not s["fixes"]:
        return True
    return (s["recheck"] or {}).get("result") == "pass" and not s["needs_recheck"] and s["status"] != "undecided"


def calendar_entries(run: dict, state: dict) -> list[dict]:
    out = []
    for row in run.get("rows", []):
        s = state["targets"].get(row["id"])
        if s is None or not on_calendar(s):
            continue
        v = {**row, **s["values"]}      # the state's current values; the raw row only where the state has none
        out.append({
            "id": row["id"], "ticker": v["ticker"], "company": v["company"], "event_type": v["event_type"],
            "title": v["title"], "program": v["program"], "date_text": v["date_text"], "date": v["date"],
            "date_end": v["date_end"], "quote": v["quote"], "source_id": row["source_id"],
            "page_number": row["page_number"], "group": s["group"], "status": s["status"],
            "decided_by_person": s["decided_by_person"], "last_action": s["last_action"],
            "needs_recheck": s["needs_recheck"], "reason": s["reason"],
        })
    out.sort(key=lambda e: (e["date"] is None, e["date"] or "", e["id"]))
    return out
