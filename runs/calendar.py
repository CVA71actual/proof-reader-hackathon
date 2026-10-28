"""The calendar list: which event rows may be placed on the calendar, computed from the state rule.

A row reaches the calendar only when runs/state.py current_state puts it in group "kept" or "corrected". A row
that is removed, unverified or still open never does, whatever its first status said. Each entry carries the
row's status and whether a person decided it, so the screen can show "Kept by you" beside a failed check and
never turns it into a tick. Values are the current ones (a person's fix applied). Rows with no exact day or
period start are listed after the dated ones, never dropped.
"""

from __future__ import annotations

CALENDAR_GROUPS = ("kept", "corrected")


def calendar_entries(run: dict, state: dict) -> list[dict]:
    out = []
    for row in run.get("rows", []):
        s = state["targets"].get(row["id"])
        if s is None or s["group"] not in CALENDAR_GROUPS:
            continue
        v = s["values"]
        out.append({
            "id": row["id"], "ticker": v["ticker"], "company": v["company"], "event_type": v["event_type"],
            "title": v["title"], "program": row.get("program"), "date_text": v["date_text"], "date": v["date"],
            "date_end": v["date_end"], "quote": v["quote"], "source_id": row["source_id"],
            "page_number": row["page_number"], "group": s["group"], "status": s["status"],
            "decided_by_person": s["decided_by_person"], "last_action": s["last_action"],
            "needs_recheck": s["needs_recheck"], "reason": s["reason"],
        })
    out.sort(key=lambda e: (e["date"] is None, e["date"] or "", e["id"]))
    return out
