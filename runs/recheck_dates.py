"""Refresh literal date checks and recover old date-only exclusions for review. $0.

    python -m runs.recheck_dates <run-id>|--all [--runs-dir runs] [--write]

No model is called and nothing is fetched: only the code checks run again, on what the run already stored (the row's
date text, dates and quote, the source's filing date, the words of the cited page when they are saved).

1. "Date read as written" (checks.event_checks.date_match, which reads dates through parse_date_phrase) runs again on
   every row; the row's date_match check, its status and, if the status changed, its group are updated, with a trail
   step "Date re-checked" (Code).
2. A past code exclusion is lifted for review when its literal date check is current.
   Literal date comparison cannot establish that the date belongs to the proposed event.
   No new date-only exclusion is applied. Explicitly accepted history stays in Past;
   accepted future rows beyond the six-month calendar stay available in Later.

A row a person has decided on is never touched. Per run it prints Excluded / Proposed — needs review / Accepted by you before
and after. Dry run unless --write (do not --write inside the repo's runs/: the saved runs are the record).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from checks import event_checks
from checks.common import CheckResult
from runs.build_entry import START_GROUP, row_reason
from runs.state import current_state, run_reference_date
from runs import sources
from runs.build_run import to_contract_check
from runs.claim_assessment import bind_checks
from server.jobs import past_candidate_reason, past_reason

RULE = "event_in_past"
STEP = {"step": "Put back", "tag": "Code", "who": "runs.rewindow",
        "text": "A printed date does not establish which event it belongs to; the old date-only exclusion returns for review."}


def rewindow(run: dict, decided: set[str], at: str) -> dict:
    """Change `run` in place. Returns {"back": [row ids], "person": n skipped for a person's decision}."""
    months = ((run.get("run") or {}).get("choices") or {}).get("time_window_months")
    out = {"back": [], "person": 0}
    if months is None:
        return out
    rows = {r["id"]: r for r in run.get("rows", [])}
    sources_by_id = {s.get("id"): s for s in run.get("sources", [])}
    keep = []
    for x in run.get("exclusions", []):
        row = rows.get(x.get("target"))
        if x.get("rule") != RULE or x.get("by") != "code" or row is None or row.get("group") != "removed":
            keep.append(x)
            continue
        now = run_reference_date(run)
        source = sources_by_id.get(row.get("source_id"), {})
        date_check = next((c for c in row.get("checks", [])
                           if c.get("name") == "date_match" or c.get("id") == "date_match"
                           or c.get("label") == "Date read as written"), None)
        from runs.claim_assessment import check_current
        if (now is None or not date_check or not check_current(date_check, row, source)
                or past_reason(row, now, source) is not None):
            keep.append(x)
            continue
        if row["id"] in decided:
            out["person"] += 1
            keep.append(x)
            continue
        old = row.get("reason") or ""
        note = old[old.find("Source note:"):] if "Source note:" in old else ""
        row["group"] = START_GROUP.get(row.get("status"), "unverified")
        row["reason"] = note
        row.setdefault("trail", []).append({**STEP, "at": at})
        out["back"].append(row["id"])
    run["exclusions"] = keep
    return out



def _to_result(c: dict) -> CheckResult:
    compared = c.get("compared") or {}
    return CheckResult(c["name"], c["label"], c["result"], c["reason"],
                       {"value": compared.get("looked_for"), "found": compared.get("found")})


def person_decisions_in_effect(run: dict, decisions: list[dict]) -> set[str]:
    """Targets with an active human decision after replay, excluding undone history lines."""
    return {target for target, state in current_state(run, decisions)["targets"].items()
            if state.get("decided_by_person")}


def _record_date_recheck(row: dict, check: dict, at: str) -> None:
    row.setdefault("trail", []).append({
        "step": "Date re-checked", "tag": "Code", "who": "runs.recheck_dates", "at": at,
        "text": f"{event_checks.DATE_LABEL}: {check['result'].replace('_', ' ')}. {check['reason']}"})


def recheck_dates(run: dict, run_dir: Path, decided: set[str], at: str) -> dict:
    """Run the literal date comparison again on every row not decided by a person. Changes `run` in place. Returns
    {"rows": rows re-run, "changed": [(row id, old result, new result)], "mutated": [row ids whose result, reason or
    trail changed], "person": rows skipped}."""
    source_by_id = {s["id"]: s for s in run.get("sources", [])}
    out = {"rows": 0, "changed": [], "person": 0, "mutated": [], "unavailable": []}
    for row in run.get("rows", []):
        idx = next((i for i, c in enumerate(row.get("checks") or []) if c.get("name") == event_checks.DATE_ID), None)
        if idx is None:
            continue
        if row["id"] in decided:
            out["person"] += 1
            continue
        source = source_by_id.get(row.get("source_id")) or {}
        filed = source.get("published_date")
        words_doc = None
        consumed = None
        page = next((p for p in source.get("pages", []) if p.get("page_number") == row.get("page_number")), None)
        if page is None:
            out["unavailable"].append((row["id"], "The cited page is missing from the saved source."))
            continue
        try:
            words_doc, consumed = sources.load_page_evidence(run_dir, page)
        except sources.SourcesError as exc:
            out["unavailable"].append((row["id"], str(exc)))
            continue
        words = (words_doc or {}).get("words", [])
        got = event_checks.date_match(
            row.get("date_text"), row.get("date"), row.get("quote"), row.get("date_end"),
            event_type=row.get("event_type"), title=row.get("title"), program=row.get("program"), filed=filed,
            today=(event_checks.release_day(words, filed, anchor=row.get("quote")) or (None,))[0])
        out["rows"] += 1
        old = row["checks"][idx]
        bound = bind_checks([got], row, source, consumed_artifact=consumed)
        current = to_contract_check(bound[0].to_dict())
        new = {**old, **current}
        if new == old:
            continue
        row["checks"][idx] = new
        out["mutated"].append(row["id"])
        if new["result"] != old["result"]:
            out["changed"].append((row["id"], old["result"], new["result"]))
        _record_date_recheck(row, new, at)
    return out


def exclude_again(run: dict, decided: set[str], at: str) -> dict:
    """Compatibility no-op: date checks do not authorize semantic exclusions.

    A current human Keep determines publication; the shared calendar partition
    preserves accepted historical events in Past. Saved old exclusions are only
    lifted by the explicit rewindow migration, never silently erased here.
    """
    return {"out": []}


def keep_reports_honest(run_dir: Path, at: str) -> str:
    """After a write, saved PDF/Word reports must not describe the old state. Rebuild them offline from the new
    state (report.build, no model calls); if that cannot be done, move them to report/stale-<timestamp>/ and set
    run.json report_stale (+ report_stale_reason) so /runs/{id}/report/{name} answers 404. No report: untouched."""
    rep = run_dir / "report"
    files = [p for p in rep.iterdir() if p.is_file()] if rep.is_dir() else []
    run_file = run_dir / "run.json"
    if not files:
        return "none saved"
    try:
        from report.build import build
        build(run_dir.name, run_dir=run_dir, force=True)
    except Exception as e:  # noqa: BLE001 - any failure to rebuild means the old files must not be served
        stamp = re.sub(r"[^0-9A-Za-z]", "", at)
        dest = rep / f"stale-{stamp}"
        dest.mkdir(parents=True, exist_ok=True)
        for p in files:
            p.rename(dest / p.name)
        run = json.loads(run_file.read_text())
        run["report_stale"] = True
        run["report_stale_reason"] = (f"The date check was re-run at {at} and the report could not be rebuilt "
                                      f"({type(e).__name__}: {e}); the old files are in {dest.name}/.")
        run_file.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n")
        return f"could not rebuild, moved to {dest.name}/ and marked stale"
    run = json.loads(run_file.read_text())
    stale = run.pop("report_stale", None)
    reason = run.pop("report_stale_reason", None)
    if stale is not None or reason is not None:
        run_file.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n")
    return "rebuilt from the new state"


def _counts(run: dict, decisions: list) -> dict:
    st = current_state(run, decisions)
    review = st["counts"]["review"]
    return {"Excluded": review["excluded"], "Proposed — needs review": review["proposed"],
            "Accepted by you": review["accepted"]}


def _line(c: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.items())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m runs.recheck_dates", description=__doc__.split("\n")[0])
    ap.add_argument("run", nargs="?", help="a run id, or --all")
    ap.add_argument("--all", dest="every", action="store_true")
    ap.add_argument("--runs-dir", type=Path, default=Path("runs"))
    ap.add_argument("--write", action="store_true", help="save the changed run.json (default: dry run)")
    args = ap.parse_args(argv)
    if not args.every and not args.run:
        ap.error("give a run id or --all")
    files = sorted(args.runs_dir.glob("*/run.json")) if args.every else [args.runs_dir / args.run / "run.json"]
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for f in files:
        if not f.is_file():
            print(f"{f.parent.name}: no run.json")
            return 1
        from runs.check_load import bind_decisions_on_load, load_bound_run
        stored = json.loads(f.read_text())
        run = load_bound_run(f.parent)
        dec = f.parent / "decisions.jsonl"
        decisions = [json.loads(x) for x in dec.read_text().splitlines() if x.strip()] if dec.is_file() else []
        decisions = bind_decisions_on_load(run, decisions, f.parent)
        # The JSONL is an immutable trail. Undo changes which decisions are in
        # effect; only the replayed state may suppress migration work.
        decided = person_decisions_in_effect(run, decisions)
        before = _counts(run, decisions)
        dates = recheck_dates(run, f.parent, decided, at)
        # The shared loader may already have refreshed this date check. When
        # the CLI persists that change, record it just as a direct recheck;
        # unchanged checks and human-decided rows create no new trail step.
        old_rows = {row["id"]: row for row in stored.get("rows", [])}
        for row in run.get("rows", []):
            if row["id"] in decided or row["id"] in dates["mutated"]:
                continue
            old_date = next((c for c in old_rows.get(row["id"], {}).get("checks", [])
                             if c.get("name") == event_checks.DATE_ID), None)
            new_date = next((c for c in row.get("checks", [])
                             if c.get("name") == event_checks.DATE_ID), None)
            if new_date is not None and new_date != old_date:
                _record_date_recheck(row, new_date, at)
                dates["mutated"].append(row["id"])
                if old_date and old_date.get("result") != new_date["result"]:
                    dates["changed"].append((row["id"], old_date["result"], new_date["result"]))
        back = rewindow(run, decided, at)
        out = exclude_again(run, decided, at)
        after = _counts(run, decisions)
        # Loading may refresh checks before the date-specific migration runs.
        # Persist that refresh only on --write; never rewrite decision history.
        changed = run != stored
        print(f"{f.parent.name}: before {_line(before)} | after {_line(after)}")
        print(f"    date check re-run on {dates['rows']} rows, {len(dates['changed'])} changed result; "
              f"{len(back['back'])} put back, {len(out['out'])} newly left out; "
              f"{dates['person'] + back['person']} left alone (a person decided)")
        if args.write and changed:
            f.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n")
            print("    (written)")
            print("    reports: " + keep_reports_honest(f.parent, at))
    if not args.write:
        print("Dry run: nothing saved. Add --write to save.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
