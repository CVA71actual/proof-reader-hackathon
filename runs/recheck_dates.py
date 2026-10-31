"""Re-run already-past exclusion and "Date read as written" on every saved run, from the stored rows. $0.

    python -m runs.recheck_dates <run-id>|--all [--runs-dir runs] [--write]

No model is called and nothing is fetched: only the code checks run again, on what the run already stored (the row's
date text, dates and quote, the source's filing date, the words of the cited page when they are saved).

1. "Date read as written" (checks.event_checks.date_match, which reads dates through parse_date_phrase) runs again on
   every row; the row's date_match check, its status and, if the status changed, its group are updated, with a trail
   step "Date re-checked" (Code).
2. The past-event rule runs again: a past code exclusion is lifted when the period no longer ends before the run date;
   an already-past row is excluded. Future rows beyond the six-month calendar stay available in Later.

A row a person has decided on is never touched. Per run it prints Excluded / Needs attention / Checked and kept before
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
from runs.state import current_state
from server.jobs import past_candidate_reason, past_reason

RULE = "event_in_past"
STEP = {"step": "Put back", "tag": "Code", "who": "runs.rewindow",
        "text": "the past-event rule now tests the period's end against the run date"}
_TODAY = re.compile(r"before this run began \((\d{4}-\d{2}-\d{2})\)")


def run_day(run: dict, exclusion: dict) -> date | None:
    m = _TODAY.search(exclusion.get("reason") or "")
    if m:
        return date.fromisoformat(m[1])
    started = (run.get("run") or {}).get("started_at") or ""
    try:
        return date.fromisoformat(started[:10])
    except ValueError:
        return None


def rewindow(run: dict, decided: set[str], at: str) -> dict:
    """Change `run` in place. Returns {"back": [row ids], "person": n skipped for a person's decision}."""
    months = ((run.get("run") or {}).get("choices") or {}).get("time_window_months")
    out = {"back": [], "person": 0}
    if months is None:
        return out
    rows = {r["id"]: r for r in run.get("rows", [])}
    keep = []
    for x in run.get("exclusions", []):
        row = rows.get(x.get("target"))
        if x.get("rule") != RULE or x.get("by") != "code" or row is None or row.get("group") != "removed":
            keep.append(x)
            continue
        now = run_day(run, x)
        if now is None or past_reason(row, now) is not None:
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


def _today_of(run: dict) -> date | None:
    for x in run.get("exclusions", []):
        if x.get("rule") == RULE and _TODAY.search(x.get("reason") or ""):
            return run_day(run, x)
    return run_day(run, {})


def recheck_dates(run: dict, run_dir: Path, decided: set[str], at: str) -> dict:
    """Run "Date read as written" again on every row not decided by a person. Changes `run` in place. Returns
    {"rows": rows re-run, "changed": [(row id, old result, new result)], "mutated": [row ids whose result, reason or
    trail changed], "person": rows skipped}."""
    sources = {s["id"]: s for s in run.get("sources", [])}
    out = {"rows": 0, "changed": [], "person": 0, "mutated": []}
    for row in run.get("rows", []):
        idx = next((i for i, c in enumerate(row.get("checks") or []) if c.get("name") == event_checks.DATE_ID), None)
        if idx is None:
            continue
        if row["id"] in decided:
            out["person"] += 1
            continue
        source = sources.get(row.get("source_id")) or {}
        filed = source.get("published_date")
        words = []
        page = next((p for p in source.get("pages", []) if p.get("page_number") == row.get("page_number")), None)
        if page and page.get("words") and (run_dir / page["words"]).is_file():
            words = json.loads((run_dir / page["words"]).read_text()).get("words", [])
        got = event_checks.date_match(
            row.get("date_text"), row.get("date"), row.get("quote"), row.get("date_end"),
            event_type=row.get("event_type"), title=row.get("title"), program=row.get("program"), filed=filed,
            today=(event_checks.release_day(words, filed) or (None,))[0])
        out["rows"] += 1
        old = row["checks"][idx]
        new = {**old, "result": got.result, "reason": got.reason,
               "compared": {**(old.get("compared") or {}), "looked_for": got.compared.get("value"),
                            "found": got.compared.get("found")}}
        if (new["result"], new["reason"]) == (old["result"], old["reason"]):
            continue
        row["checks"][idx] = new
        out["mutated"].append(row["id"])
        results = [_to_result(c) for c in row["checks"]]
        status = event_checks.row_status(results)
        if new["result"] != old["result"]:
            out["changed"].append((row["id"], old["result"], new["result"]))
        row.setdefault("trail", []).append({
            "step": "Date re-checked", "tag": "Code", "who": "runs.recheck_dates", "at": at,
            "text": f"Date read as written: {new['result'].replace('_', ' ')}. {new['reason']}"})
        if status != row.get("status"):
            row["status"] = status
            if row.get("group") != "removed":
                row["group"] = START_GROUP.get(status, "unverified")
                row["reason"] = row_reason(status, results, row)
    return out


def exclude_again(run: dict, decided: set[str], at: str) -> dict:
    """Leave out, under the rule, the rows the window now excludes that are still in. Returns {"out": [row ids]}."""
    months = ((run.get("run") or {}).get("choices") or {}).get("time_window_months")
    out = {"out": []}
    now = _today_of(run)
    if months is None or now is None:
        return out
    for row in run.get("rows", []):
        if row.get("group") == "removed" or row["id"] in decided:
            continue
        reason = past_reason(row, now)
        if reason is None:
            candidate = past_candidate_reason(row, now)
            date_check = next((c for c in row.get("checks", []) if c.get("name") == "date_match"), None)
            if candidate and (not date_check or date_check.get("result") != "pass"):
                proposal = (f"{candidate} Proposed exclusion not applied because Date read as written is "
                            f"{(date_check or {}).get('result') or 'unavailable'}.")
                if row.get("past_exclusion_proposal") != proposal:
                    row["past_exclusion_proposal"] = proposal
                    row.setdefault("trail", []).append({"step": "Past exclusion not applied", "tag": "Code",
                        "who": "runs.recheck_dates", "text": proposal, "at": at})
            continue
        row.pop("past_exclusion_proposal", None)
        old = row.get("reason") or ""
        note = old[old.find("Source note:"):] if "Source note:" in old else ""
        row["group"], row["reason"] = "removed", f"{reason} {note}".strip()
        row.setdefault("trail", []).append({"step": "Left out", "tag": "Code", "who": "runs.recheck_dates",
                                            "text": reason, "at": at})
        run.setdefault("exclusions", []).append({"id": f"x{len(run.get('exclusions', [])) + 1}", "target": row["id"],
                                                 "rule": RULE, "reason": reason, "by": "code"})
        out["out"].append(row["id"])
    return out


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
    if run.pop("report_stale", None) is not None or run.pop("report_stale_reason", None) is not None:
        run_file.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n")
    return "rebuilt from the new state"


def _counts(run: dict, decisions: list) -> dict:
    st = current_state(run, decisions)
    rows = st["counts"]["rows"]
    return {"Excluded": rows.get("removed", 0), "Needs attention": st["unresolved"],
            "Checked and kept": rows.get("kept", 0) + rows.get("corrected", 0)}


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
        run = json.loads(f.read_text())
        dec = f.parent / "decisions.jsonl"
        decisions = [json.loads(x) for x in dec.read_text().splitlines() if x.strip()] if dec.is_file() else []
        decided = {d.get("target") for d in decisions if isinstance(d, dict)}
        before = _counts(run, decisions)
        dates = recheck_dates(run, f.parent, decided, at)
        back = rewindow(run, decided, at)
        out = exclude_again(run, decided, at)
        after = _counts(run, decisions)
        changed = bool(dates["changed"] or dates["mutated"] or back["back"] or out["out"] or before != after)
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
