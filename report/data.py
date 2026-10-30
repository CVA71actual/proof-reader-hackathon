"""Pure view of a saved run for the report. No files written; no network."""

from __future__ import annotations

import calendar as _calendar
import json
from datetime import date, datetime
from pathlib import Path

from runs.state import current_state

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

    calendar = []
    for rid, st in state["targets"].items():
        if st["kind"] != "row" or st["group"] not in ("kept", "corrected"):
            continue
        row = rows_raw[rid]
        values = st["values"]
        src = sources.get(row.get("source_id"))
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
        })
    calendar.sort(key=lambda r: (r["sort_key"], r["ticker"], r["id"]))

    corrections = []
    for m in run.get("mistakes", []):
        rid = m.get("target")
        st = state["targets"].get(rid)
        if not st or st["kind"] != "row":
            continue
        if st["group"] != "corrected" and rid not in {c.get("id") for c in corrections}:
            # still list the recorded mistake even if a later decision moved the row
            pass
        row = rows_raw.get(rid, {})
        check_name = None
        for c in row.get("checks") or []:
            if c.get("result") == "fail":
                check_name = c.get("label") or c.get("name")
                break
        corrections.append({
            "id": rid,
            "what": m.get("what_changed") or "",
            "caught_by": _caught_by_label(m.get("caught_by") or ""),
            "check": check_name,
            "before_after": m.get("what_changed") or "",
        })
    # Person fixes still in effect (before → after from the fix record)
    for rid, st in state["targets"].items():
        if st["kind"] != "row" or st["group"] != "corrected":
            continue
        for fx in st.get("fixes") or []:
            line = f"{fx['field']}: {fx['from']} → {fx['to']}"
            said = f"{fx['field']} changed from {fx['from']} to {fx['to']}"
            if any(c["id"] == rid and (line in (c.get("what") or "") or said in (c.get("what") or ""))
                   for c in corrections):
                continue
            check = None
            rc = fx.get("recheck") or {}
            for c in rc.get("checks") or []:
                if c.get("result") == "fail":
                    check = c.get("label") or c.get("name")
                    break
            if not check:
                for c in (rc.get("before") or {}).get("checks") or []:
                    if c.get("result") == "fail":
                        check = c.get("label") or c.get("name")
                        break
            corrections.append({
                "id": rid,
                "what": line,
                "caught_by": "person",
                "check": check,
                "before_after": line,
            })

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
        if st["kind"] != "row" or st["group"] not in ("open", "unverified"):
            continue
        row = rows_raw[rid]
        open_rows.append({
            "id": rid,
            "group": st["group"],
            "group_label": GROUP_LABEL[st["group"]],
            "ticker": st["values"].get("ticker") or row.get("ticker") or "",
            "event": _event_label(row, st["values"]),
            "reason": st.get("reason") or row.get("reason") or "No reason recorded.",
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
        "run_date": date_in_words(meta.get("finished_at") or meta.get("started_at") or ""),
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
        "corrections": corrections,
        "exclusions": exclusions,
        "open_rows": open_rows,
        "sources": source_rows,
        "state": state,
    }
