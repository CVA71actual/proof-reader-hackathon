"""The draft audit of a research run (RESEARCHER-FULL-001 S1), computed by code from the saved run.

    python -m runs.research_audit <run_dir>        prints the audit, row by row, with the scope of every check

What it establishes, and only that:
  every_row_sourced     each research row cites a source that is in run.json AND in the run's source manifest
  every_source_captured each fetched source's original bytes still hash to retrieval.original_sha256, its page files
                        verify (runs.sources.verify_page_files) and the manifest has a matching source line; the
                        manifest's checkpoint chain recomputes
  window                the window test on the period END: in window when end >= as_of and start <= window_end
                        (an exact day is its own start and end); undated rows are listed as undated, never placed
  near_duplicates       linked candidate consolidations (never merged)
  check_scope           per row: every check that ran, what it compared and its result; the evidence spans' own quote
                        checks; the cited pages' capture checks; and what code did NOT check
  searched_not_found    coverage records with status checked_none, with the retrieval times of what was searched
  capture_failures      per company, every fetch that could not be captured
  exports_agree         the report view (report.data.build_view, which the PDF and DOCX are written from) places the
                        same rows with the same dates as the shared state (runs.state.current_state)
  complete              every company finished and nothing above failed; a partial draft is never published
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

from runs import sources
from runs.research_contract import LABEL
from runs.state import current_state, run_reference_date

NOT_CHECKED = ("Meaning, relevance and the relationship between spans are the model's proposal, not checked by code. "
               "Numbers other than dates are not compared. Independent review did not run (stage S2). "
               "Completeness of the web search is not established.")


def _manifest_sources(run_dir: Path) -> dict:
    path = Path(run_dir) / "research" / "manifest.jsonl"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("kind") == "source":
                out[rec["source_id"]] = rec
    return out


def window_test(row: dict, as_of: date, window_end: date) -> dict:
    start, end = row.get("date"), row.get("date_end") or row.get("date")
    if not start:
        return {"result": "undated", "start": None, "end": None,
                "reason": "No timing bounds: shown as undated, never given an invented date."}
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return {"result": "unreadable", "start": start, "end": end, "reason": "The timing bounds are not ISO dates."}
    inside = e >= as_of and s <= window_end
    why = (f"the period ends {e} on or after {as_of} and starts {s} on or before {window_end}" if inside else
           f"the period ends {e}, before {as_of}" if e < as_of else f"the period starts {s}, after {window_end}")
    return {"result": "in_window" if inside else "outside_window", "start": start, "end": end, "reason": why}


def row_scope(row: dict, source: dict | None) -> dict:
    checks = [{"name": c.get("name"), "label": c.get("label"), "result": c.get("result"),
               "compared": c.get("compared"), "reason": c.get("reason")} for c in row.get("checks") or []]
    spans = row["research"]["evidence"]
    pages = {p["page_number"]: p for p in (source or {}).get("pages", [])}
    page = pages.get(row.get("page_number")) or {}
    return {"row_checks": checks,
            "span_checks": [{"source_id": s["source_id"], "page_number": s["page_number"], "roles": s["roles"],
                             "quote_match": s["quote_match"], "printed_quote_match": s["printed_quote_match"]}
                            for s in spans],
            "page_capture_checks": page.get("research_checks"),
            "issues": [i["code"] for i in row["research"].get("issues") or []],
            "label": row["research"]["label"], "not_checked": NOT_CHECKED}


def draft_audit(run_dir: Path, run: dict, decisions: list) -> dict:
    run_dir = Path(run_dir)
    meta = run["run"]
    research = meta.get("research") or {}
    by_id = {s["id"]: s for s in run.get("sources", [])}
    manifest = _manifest_sources(run_dir)
    problems: list[str] = []
    as_of = run_reference_date(run) or date.today()
    from runs.research import verify_manifest
    from server.jobs import add_months
    window_end = add_months(as_of, 6)

    rows = [r for r in run.get("rows", []) if r.get("research")]
    # DATE-001 / UNKNOWN-001: one source of bounds. A period's bounds are the ones code derives with the date check
    # (state values), the same values the calendar places by; the raw row keeps null for a period.
    targets = current_state(run, decisions)["targets"]
    row_out = []
    for r in rows:
        src = by_id.get(r.get("source_id"))
        sourced = src is not None and r.get("source_id") in manifest
        if not sourced:
            problems.append(f"{r['id']} cites {r.get('source_id')}, which is not a captured source in the manifest.")
        for s in r["research"]["evidence"]:
            if s["source_id"] not in by_id or s["source_id"] not in manifest:
                problems.append(f"{r['id']} cites span source {s['source_id']}, which is not captured.")
        row_out.append({"id": r["id"], "ticker": r["ticker"], "title": r["title"], "date_text": r.get("date_text"),
                        "status": r["status"], "revision": r["research"]["revision"], "source_id": r.get("source_id"),
                        "sourced": sourced,
                        "window": window_test({**r, **{k: (targets.get(r["id"]) or {}).get("values", {}).get(k)
                                                        for k in ("date", "date_end")}}, as_of, window_end),
                        "near_duplicates": list(r["research"].get("near_duplicates") or []),
                        "scope": row_scope(r, src)})

    src_out = []
    for s in run.get("sources", []):
        ret = s.get("retrieval")
        if not ret:
            continue
        ok, why = True, []
        f = run_dir / ret.get("original_file", "")
        if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != ret["original_sha256"]:
            ok = False
            why.append("its original bytes are missing or do not match their hash")
        m = manifest.get(s["id"])
        if m is None or m.get("original_sha256") != ret["original_sha256"]:
            ok = False
            why.append("the manifest has no matching source line")
        elif [(p["page_number"], p["image_sha256"], p["words_sha256"]) for p in m.get("pages", [])] != \
                [(p["page_number"], p["image_sha256"], p["words_sha256"]) for p in s["pages"]]:
            ok = False
            why.append("its pages differ from the manifest")
        try:
            sources.verify_page_files(run_dir, {"sources": [s]})
        except sources.SourcesError as e:
            ok = False
            why.append(str(e))
        if not ok:
            problems.append(f"Source {s['id']}: " + "; ".join(why) + ".")
        src_out.append({"id": s["id"], "url": s["url"], "retrieved_at": ret["retrieved_at"],
                        "original_sha256": ret["original_sha256"], "pages": len(s["pages"]), "captured": ok,
                        "render_limitations": (ret.get("rendered") or {}).get("limitations") or [],
                        "page_flags": [{"page_number": p["page_number"],
                                        **{k: (p.get("research_checks") or {}).get(k, {}).get("result")
                                           for k in ("capture_incomplete", "hidden_text")}} for p in s["pages"]]})
    problems += verify_manifest(run_dir)

    exports = exports_agree(run, decisions)
    if not exports["agree"]:
        problems.append("The report view and the shared state do not place the same rows: " + exports["detail"])

    companies = research.get("companies") or {}
    unfinished = [t for t, c in companies.items() if c.get("status") != "finished"]
    return {
        "label": research.get("label") or LABEL, "as_of": as_of.isoformat(), "window_end": window_end.isoformat(),
        "every_row_sourced": all(r["sourced"] for r in row_out),
        "every_source_captured": all(s["captured"] for s in src_out),
        "rows": row_out, "sources": src_out,
        "near_duplicates": list(research.get("consolidations") or []),
        "searched_not_found": [c for c in research.get("coverage") or [] if c.get("searched_not_found")],
        "capture_failures": {t: c.get("capture_failures") or [] for t, c in companies.items()},
        "limitations": {t: c.get("limitation") for t, c in companies.items() if c.get("limitation")},
        "model_limitations": {t: ((c.get("finish") or {}).get("limitations") or []) for t, c in companies.items()},
        "exports": exports, "problems": problems,
        "unfinished_companies": unfinished,
        "complete": not problems and not unfinished,
        "published": False,
        "publish_note": ("A draft. Rows are proposals: " + (research.get("label") or LABEL) + "." +
                         (" Partial: research did not finish for " + ", ".join(unfinished) + "." if unfinished else "")),
    }


def exports_agree(run: dict, decisions: list) -> dict:
    """The report view (the PDF and DOCX are built from it) against the shared state: same rows placed, same dates."""
    from report.data import build_view
    state = current_state(run, decisions)
    view = build_view(run, decisions, generated_at="2026-01-01T00:00:00+00:00")
    placed = {r["id"]: r["date_display"] for key in ("calendar", "past", "later", "contingent", "undated",
                                                    "timing_unresolved") for r in view[key]}
    expected = {rid: st["date_display"] for rid, st in state["targets"].items()
                if st["kind"] == "row" and st.get("on_calendar")}
    attention = {r["id"] for r in view["open_rows"]}
    expected_attention = {rid for rid, st in state["targets"].items() if st["kind"] == "row" and st.get("needs_attention")}
    agree = set(placed) == set(expected) and attention == expected_attention and all(
        placed[k] == expected[k] or k in {r["id"] for r in view["undated"]} for k in placed)
    detail = "" if agree else (f"calendar {sorted(placed)} vs state {sorted(expected)}; attention "
                               f"{sorted(attention)} vs {sorted(expected_attention)}")
    return {"agree": agree, "calendar_rows": sorted(placed), "attention_rows": sorted(attention), "detail": detail,
            "research_label": view.get("research_label")}


def format_audit(audit: dict) -> str:
    """The audit as plain text, with the scope of each check per row."""
    out = [f"Draft audit — {audit['label']}", f"Window {audit['as_of']} to {audit['window_end']} (test on the period end)",
           f"Every row sourced: {audit['every_row_sourced']}. Every source captured: {audit['every_source_captured']}. "
           f"Exports agree: {audit['exports']['agree']}. Complete: {audit['complete']}. Published: no.",
           audit["publish_note"], ""]
    for r in audit["rows"]:
        out.append(f"{r['id']} {r['ticker']} rev {r['revision']} [{r['status']}] {r['title']} — {r['date_text'] or 'undated'}"
                   f" — window: {r['window']['result']} ({r['window']['reason']})")
        for c in r["scope"]["row_checks"]:
            looked = (c.get("compared") or {}).get("looked_for")
            out.append(f"    check {c['name']}: {c['result']} — compared {looked!r}: {c['reason']}")
        for s in r["scope"]["span_checks"]:
            out.append(f"    span {s['source_id']} p{s['page_number']} {','.join(s['roles'])}: quote "
                       f"{s['quote_match']}, printed-text quote {s['printed_quote_match']}")
        pc = r["scope"]["page_capture_checks"] or {}
        out.append(f"    page capture: incomplete {pc.get('capture_incomplete', {}).get('result')}, hidden text "
                   f"{pc.get('hidden_text', {}).get('result')}")
        if r["scope"]["issues"]:
            out.append("    issues: " + ", ".join(r["scope"]["issues"]))
        if r["near_duplicates"]:
            out.append("    linked near-duplicates (not merged): " + ", ".join(r["near_duplicates"]))
        out.append("    not checked: " + r["scope"]["not_checked"])
    out.append("")
    for s in audit["sources"]:
        out.append(f"source {s['id']} {s['url']} retrieved {s['retrieved_at']} sha256 {s['original_sha256'][:16]}… "
                   f"{s['pages']} pages captured={s['captured']}"
                   + (" — render limitations: " + "; ".join(s["render_limitations"]) if s["render_limitations"] else ""))
    for c in audit["searched_not_found"]:
        out.append(f"searched, not found ({c['ticker']}, {c['category']}): {c['question']} — at "
                   f"{', '.join(c.get('retrieval_times') or []) or 'no retrieval time recorded'}")
    for t, fails in audit["capture_failures"].items():
        for f in fails:
            out.append(f"capture failed ({t}): {f['url']} — {f['reason']}")
    for t, lim in audit["limitations"].items():
        out.append(f"limitation ({t}): {lim['reason']}")
    for p in audit["problems"]:
        out.append("PROBLEM: " + p)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    run_dir = Path(args[0])
    from report.data import load_run_dir
    run, decisions = load_run_dir(run_dir)
    print(format_audit(draft_audit(run_dir, run, decisions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
