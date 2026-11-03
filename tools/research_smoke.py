"""LIVE smoke test of the S1 research loop: ONE company, real Muse Spark Responses API calls and real public fetches.

    NOT RUN BY THE TESTS. It spends money. Run it only with the Operator's go and CVA's stated cap.

    python -m tools.research_smoke --ticker PTGX --cap-usd 3 --i-have-the-operator-go [--runs-dir DIR]

It creates one research run (mode "research", the default research assignment, the given spending cap; every other
limit off) in --runs-dir (default: a new folder under /tmp, never the served glass runs), drives it to its end in
this process, and prints the per-company outcome, spend, request and search counts, any kill condition, and the
draft audit. The key is read from MODEL_API_KEY or ~/.config/proof-reader/env and is never printed. SEC fetches
need SEC_USER_AGENT; HTML pages need Google Chrome (CHROME_BIN), rendered from an isolated snapshot.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--cap-usd", type=float, required=True, help="the run's spending cap (the only limit on)")
    ap.add_argument("--i-have-the-operator-go", action="store_true", dest="go",
                    help="required: confirms the Operator's go and CVA's cap for this paid run")
    ap.add_argument("--runs-dir", type=Path, default=None)
    ap.add_argument("--as-of", default=None, help="the run's reference date (default: today)")
    args = ap.parse_args(argv)
    if not args.go:
        print("Refused: this is a paid live run. Pass --i-have-the-operator-go only with the Operator's go and CVA's cap.")
        return 2
    if not (0 < args.cap_usd <= 30):
        print("Refused: --cap-usd must be above $0 and at most the $30 CVA approved.")
        return 2
    from capture.responses_reader import LiveResponsesReader
    from runs import research, research_audit
    from server import jobs

    runs_dir = args.runs_dir or Path(tempfile.mkdtemp(prefix="proof-reader-research-smoke-"))
    runs_dir.mkdir(parents=True, exist_ok=True)
    if "proof-reader-glass" in str(runs_dir.resolve()):
        print("Refused: the smoke run never writes into the served glass checkout.")
        return 2
    reader = LiveResponsesReader()
    problem = reader.ready()
    if problem:
        print(problem)
        return 2
    choices = jobs.validate_choices({"mode": "research", "companies": [args.ticker], "spending_limit_usd": args.cap_usd})
    run = jobs.create_run(runs_dir, choices, as_of=args.as_of)
    rid = run["run"]["id"]
    print(f"Run {rid} in {runs_dir}: researching {args.ticker} with a ${args.cap_usd:.2f} cap ...", flush=True)
    research.run_research(runs_dir, rid, reader, research.ResearchTools())
    doc = json.loads((runs_dir / rid / "run.json").read_text())
    meta = doc["run"]
    r = meta["research"]
    print(f"Status: {meta['status']} — {meta['status_reason']}")
    print(f"Spend: ${meta['spend_usd'] or 0:.4f}; unpriced calls: {meta.get('unpriced_calls', 0)}")
    for t, c in r["companies"].items():
        print(f"{t}: {c['status']} — requests {c['requests']}, searches {c['searches']}, fetches {c['fetches']}, "
              f"sources {c['sources']}, proposals {c['proposals']}, empty answers {c['empty_answers']}, "
              f"spend ${c['spend_usd']:.4f}")
    for k in r.get("kill_conditions") or []:
        print("KILL CONDITION:", k["detail"])
    for x in r.get("reservation_exceeded") or []:
        print("RESERVATION EXCEEDED:", x)
    from report.data import load_run_dir
    run_doc, decisions = load_run_dir(runs_dir / rid)
    print(research_audit.format_audit(research_audit.draft_audit(runs_dir / rid, run_doc, decisions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
