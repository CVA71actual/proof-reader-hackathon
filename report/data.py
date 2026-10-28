"""Pure view of a saved run for the report. No files written; no network."""

from __future__ import annotations

import json
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


def _date_display(values: dict) -> str:
    """Period rows show the period as stated; never invent a day."""
    text = (values.get("date_text") or "").strip()
    if values.get("date_end"):
        return text or values.get("date") or "—"
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


def _event_label(row: dict, values: dict) -> str:
    title = row.get("title") or values.get("title") or ""
    return title


def _caught_by_label(who: str) -> str:
    return {"code": "code", "second_reader": "second reader", "person": "person"}.get(who, who)


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
            "program": row.get("program") or "",
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
            if any(c["id"] == rid and fx["field"] in (c.get("what") or "") for c in corrections):
                continue
            line = f"{fx['field']}: {fx['from']} → {fx['to']}"
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
        "run_date": (meta.get("finished_at") or meta.get("started_at") or "")[:10],
        "companies": companies,
        "generated_at": generated_at,
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
