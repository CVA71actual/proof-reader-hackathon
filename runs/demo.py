"""The demo in one command: list the runs, print one run's numbers, serve the app.

    python -m runs.demo                      the saved runs, then ptgx-r5's numbers, then the app at http://127.0.0.1:8777/
    python -m runs.demo --run ptgx-r4a       another run's numbers, and the app opens on that run
    python -m runs.demo --no-serve           print and stop (nothing is served)
    python -m runs.demo --port 8800          another port

It reads saved files only: runs/<id>/run.json and decisions.jsonl (the state rule), and runs/<id>/accuracy.json when the run
has one and its hashes hold (accuracy/export.py). No model is called and no key is read. The numbers are labelled by what the
answer key says it is: "Sealed set" only when the key says sealed, otherwise "Unsealed (author key)", as the screens do. The app
is bound to this machine (127.0.0.1) and nothing here changes that.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from accuracy import export
from runs import build_run
from runs.calendar import calendar_entries
from runs.state import current_state
from runs.twelve import summary

DEMO_RUN = "ptgx-r5"
HOST = "127.0.0.1"
PORT = 8777


def _load(run_dir: Path) -> tuple[dict, dict]:
    run = json.loads((run_dir / "run.json").read_text())
    f = run_dir / "decisions.jsonl"
    decisions = [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.is_file() else []
    return run, current_state(run, decisions)


def run_ids(runs_dir: Path) -> list[str]:
    return sorted(p.parent.name for p in Path(runs_dir).glob("*/run.json"))


def list_lines(runs_dir: Path) -> list[str]:
    lines = []
    for rid in run_ids(runs_dir):
        run, state = _load(Path(runs_dir) / rid)
        m = run["run"]
        rows = state["counts"]["rows"]
        what = "sample" if m.get("sample") else (m.get("status") or "saved")
        lines.append(f"  {rid:<12} #{m['number']:<2} {m['label']}  [{what}]  rows: kept {rows['kept']}, corrected "
                     f"{rows['corrected']}, open {rows['open']}")
    return lines


def accuracy_lines(run_dir: Path) -> list[str]:
    """The saved scoring in the screens' words, or the plain reason there is none to show."""
    data, problem = export.read_if_intact(run_dir)
    if problem:
        return [f"  Accuracy: not shown. {problem}"]
    if data is None:
        return ["  Accuracy: no scoring is saved for this run."]
    doc = json.loads(data)
    sealed = doc["key"].get("sealed") is True
    label = "Sealed set" if sealed else "Unsealed (author key)"
    out = []
    for name, m in doc["models"].items():
        bits = []
        for key, word, n in (("values", "values", "N"), ("events", "events", "M"), ("planted", "planted", None)):
            part = m.get(key)
            if part:
                bits.append(f"{word} {part['caught'] if key == 'planted' else part['found']}/"
                            f"{part['total'] if key == 'planted' else part[n]}")
        out.append(f"  {label}: {name} — " + (" · ".join(bits) if bits else "nothing scored"))
    a = doc["agreement"]
    if a["rows_compared"]:
        out.append(f"  The two readers agreed on {a['rows_agreed']} of {a['rows_compared']} rows"
                   + (f"; both agreed on {a['both_wrong']['rows']} the key says are wrong"
                      if a["both_wrong"]["rows"] is not None else "")
                   + ". Agreement is not evidence: the checks decide.")
    out.append(f"  Key: {'sealed' if sealed else 'an author key, not sealed'}"
               f"{', complete' if doc['key'].get('complete') else ', partial'}; scored {doc['date']} on product "
               f"{doc['product_tip'] or 'unknown'}" + (" (uncommitted changes)" if doc.get("product_dirty") else "")
               + (". This run is sample data." if doc.get("sample") else "."))
    return out


def main(argv: list[str] | None = None, serve=None) -> int:
    """`serve`: how to serve the app (default uvicorn.run); a test passes its own."""
    ap = argparse.ArgumentParser(prog="python -m runs.demo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=None, help="the run the app opens on (default: unchanged)")
    ap.add_argument("--runs-dir", type=Path, default=build_run.RUNS)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-serve", action="store_true")
    args = ap.parse_args(argv)

    ids = run_ids(args.runs_dir)
    shown = args.run or DEMO_RUN
    if shown not in ids:
        print(f"There is no run {shown!r}. Saved runs: {', '.join(ids) or 'none'}.", file=sys.stderr)
        return 2
    print("Saved runs:")
    print("\n".join(list_lines(args.runs_dir)))
    run_dir = args.runs_dir / shown
    run, state = _load(run_dir)
    print(f"\n{shown}")
    for line in summary(run, state, calendar_entries(run, state)):
        print(line)
    for line in accuracy_lines(run_dir):
        print(line)
    if args.no_serve:
        return 0
    from server.app import create_app

    app = create_app(runs_dir=args.runs_dir, default_run=args.run)
    print(f"\nServing the app at http://{HOST}:{args.port}/  (this machine only; Ctrl-C stops it)")
    if args.run:
        print(f"Open http://{HOST}:{args.port}/#/{args.run}/audit")
    if serve is None:
        import uvicorn

        serve = lambda a, host, port: uvicorn.run(a, host=host, port=port, log_level="warning")
    serve(app, HOST, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
