"""The twelve-company run from the command line: the same code path as the server's POST /api/runs/new.

    python -m runs.twelve --companies all --window 6 --limit 5.00

creates the run (server/jobs.py create_run), carries it on with the live readers (Muse Spark first, Muse Spark Open second
on the page image, Muse Wonder for a row that fails a check, SAM 3.1 for a scan), resumes it past a turn limit, builds the
report when it is done, and prints the audit counts, the cost and the cost per kept row. Every page comes from the captured-pages
file (runs/sources.py); every limit is the server's. Keys are read by the readers from ~/.config/proof-reader/env and are
never printed.

    --companies all | PTGX,IONS    which companies (default all: every company in the captured-pages file)
    --window 6                     months ahead a date may be; earlier or later is left out (default: none)
    --limit 5.00                   spending limit in dollars (default: none)
    --turns 40                     model calls before a pause; the command resumes through pauses (--max-resumes)
    --types milestone,trial_results  event types (default: all)
    --note "..."                   a note for the agent (context only: it cannot remove a check)
    --runs-dir DIR / --index FILE  where runs go (default runs/) / the captured-pages file (default: runs/sources_index.twelve.json when present, else runs/sources_index.json)
    --no-second / --no-wonder      leave that model out of the run (cost control)
    --no-report                    do not build the PDF and Word report
    --reader-tools                 the first reader may call the checks as tools while it reads (capture/reader_tools.py)
    --id twelve-1                  keep the run under this folder name (default run-<n>): for the recorded run
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from pathlib import Path

from capture import read_events
from capture.reader import LiveReader
from capture.sam import SamReader
from capture.second_reader import SecondReader
from capture.wonder import WonderReader
from runs import build_run, sources
from runs.calendar import calendar_entries
from runs.check_load import bind_decisions_on_load, load_bound_run
from runs.state import current_state
from server import jobs


def summary(run: dict, state: dict, calendar: list[dict]) -> list[str]:
    m = run["run"]
    cells = state["counts"]["cells"]
    review = state["counts"]["review"]
    checks = Counter(target["status"] for target in state["targets"].values() if target["kind"] == "row")
    kept = review["accepted"]
    inc = state["counts"]["inclusion"]
    in_window = [e for e in calendar if e.get("placement") == "calendar"]
    kept_on = sum(1 for e in in_window if e.get("inclusion") == "kept")
    auto_on = sum(1 for e in in_window if e.get("inclusion") == "included")
    who = state["mistakes"]
    cost = m.get("spend_usd")
    per = state["cost_per_kept_row_usd"]
    page_outcomes = state.get("page_outcomes") or {}
    pages_read = (page_outcomes.get("counts") or {}).get("read", "not recorded") \
        if page_outcomes.get("recorded") else "not recorded"
    lines = [f"{m['id']} · {m['label']}: {m.get('status') or 'saved'}"
             + (f" ({m['status_reason']})" if m.get("status_reason") else ""),
             f"  pages read: {pages_read}"
             + (f"; turns: {m['turns_total']}; resumes: {m.get('resumes', 0)}" if "turns_total" in m else ""),
             "  check results: " + (", ".join(f"{status} {n}" for status, n in sorted(checks.items())) or "no rows"),
             # CALENDAR-RULE-001: kept by you and included automatically are never one number
             f"  rows: Kept by you {inc['kept']}, Included automatically (not reviewed by you) {inc['included']}, "
             f"Needs your review {inc['review']}, Context (past) {inc['context']}, Later {inc['later']}, "
             f"Undated {inc['undated']}, Excluded {inc['excluded']}",
             "  values: " + ", ".join(f"{g} {n}" for g, n in cells.items()),
             f"  needs attention: {state['unresolved']}",
             f"  mistakes caught by code {who['code']}, second reader {who['second_reader']}, person {who['person']}; "
             f"excluded by code {state['excluded']['code']}, by person {state['excluded']['person']}",
             f"  calendar: {kept_on} kept by you, {auto_on} included automatically",
             f"  cost: {'not recorded' if cost is None else '$%.4f' % cost}; accepted rows: {kept}; cost per accepted row: "
             f"{'not computed' if per is None else '$%.4f' % per}"]
    skipped = m.get("skipped_pages") or []
    if skipped:
        lines.append(f"  skipped pages: {len(skipped)}")
        lines += [f"    {s['ticker']} {s['source_id']}: {s['reason']}" for s in skipped]
    sr, esc = m.get("second_reader"), m.get("escalation")
    if sr:
        lines.append(f"  second reader: {sr['status']}" + (f" ({sr['input']})" if sr.get("input") else ""))
    if esc:
        lines.append(f"  escalation to Muse Wonder: {esc['rows_sent']} sent, {esc['rows_replaced']} replaced")
    return lines


def main(argv: list[str] | None = None, readers: tuple | None = None) -> int:
    """`readers`: (first, second, wonder, sam) for a test; default the live ones."""
    ap = argparse.ArgumentParser(prog="python -m runs.twelve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--companies", default="all")
    ap.add_argument("--window", type=int, default=None)
    ap.add_argument("--limit", type=float, default=None)
    ap.add_argument("--turns", type=int, default=None)
    ap.add_argument("--max-resumes", type=int, default=60)
    ap.add_argument("--types", default=None)
    ap.add_argument("--note", default=None)
    ap.add_argument("--runs-dir", type=Path, default=build_run.RUNS)
    ap.add_argument("--index", type=Path, default=None)
    ap.add_argument("--no-second", action="store_true")
    ap.add_argument("--no-wonder", action="store_true")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--reader-tools", action="store_true",
                    help="let the first reader call the checks as tools while it reads (default off)")
    ap.add_argument("--id", default=None)
    ap.add_argument("--as-of", default=None, help="the run's reference date, YYYY-MM-DD (default: today)")
    args = ap.parse_args(argv)

    try:
        # The command line prefers the captured twelve-company index when it exists (capture/fetch_filings.py
        # writes runs/sources_index.twelve.json); the server's default stays the small built-in index.
        twelve = build_run.REPO / "runs" / "sources_index.twelve.json" if hasattr(build_run, "REPO") else Path("runs/sources_index.twelve.json")
        chosen = args.index or (twelve if twelve.is_file() else None)
        index = sources.load_index(chosen) if chosen else sources.load_index()
        companies = ["all"] if args.companies.strip().lower() == "all" else \
            [c.strip().upper() for c in args.companies.split(",") if c.strip()]
        types = list(read_events.EVENT_TYPES) if not args.types else [t.strip() for t in args.types.split(",")]
        as_of = jobs.validate_as_of(args.as_of)
        choices = jobs.validate_choices({"companies": companies, "event_types": types, "time_window_months": args.window,
                                         "spending_limit_usd": args.limit, "note_for_agent": args.note,
                                         "reader_tools": args.reader_tools}, index)
    except (sources.SourcesError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    first, second, wonder, sam = readers or (LiveReader(), SecondReader(), WonderReader(), SamReader())
    second = None if args.no_second else second
    wonder = None if args.no_wonder else wonder

    args.runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        run = jobs.create_run(args.runs_dir, choices, args.turns, args.id, as_of)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    rid = run["run"]["id"]
    print(f"Created {rid}: {run['run']['label']} ({len(choices['companies'])} companies)")
    if index.empty and companies == ["all"]:
        print(f"  left out, no page indexed for {', '.join(index.empty)}")
    for resumed in range(args.max_resumes + 1):
        jobs.run_job(args.runs_dir, rid, first, second, None, wonder, sam, index)
        meta = json.loads((args.runs_dir / rid / "run.json").read_text())["run"]
        if meta["status"] not in ("paused_turn_limit", "failed") or resumed == args.max_resumes:
            break
        # a turn-limit pause is resumed; so is a failure (a call that did not succeed): the page is tried again, and one
        # that stops the run twice is skipped by the job, so the rest of the run goes on
        print(f"  {meta['status']}: {meta.get('status_reason')}\n  resuming ({resumed + 1})")
        jobs.resume_run(args.runs_dir, rid)

    run_json = load_bound_run(args.runs_dir / rid)
    decisions_file = args.runs_dir / rid / "decisions.jsonl"
    decisions = [json.loads(x) for x in decisions_file.read_text().splitlines() if x.strip()]
    decisions = bind_decisions_on_load(run_json, decisions, args.runs_dir / rid)
    state = current_state(run_json, decisions)
    for line in summary(run_json, state, calendar_entries(run_json, state)):
        print(line)
    status = run_json["run"]["status"]
    if status == "done" and not args.no_report:
        from report.build import build

        built = build(rid, run_dir=args.runs_dir / rid)
        print(f"  report: {built['pdf']}\n          {built['docx']}")
    return 0 if status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
