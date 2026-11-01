"""Two Operator steps on a saved run, from the command line: the same functions as the server's routes (Job 51f).

    python -m server.run_steps record_operator_stop RUN_ID [--runs-dir DIR]
        A run left "running" by a worker stopped from outside: reads runs/<id>/OPERATOR_STOP.json (its fields are in
        server/jobs.py's notes), adds the potentially billed calls to unpriced_calls, writes a Person step and sets the
        status to needs_input. Same as POST /api/runs/{id}/operator-stop. It never resumes the run and never accepts
        the unknown cost: a person does that on resume (accept_unknown_cost true, and a higher spending limit if the
        run has one). Run again after a crash between its two writes, it finishes run.json without counting twice.

    python -m server.run_steps record_source_freeze RUN_ID --index FILE --index-sha256 HEX [--runs-dir DIR]
        A run started before its source set was recorded: after the Operator has checked the sources index's SHA-256
        by hand, record the index (its path and name: the run's own index, which its resumes read) and every page
        file's hash, once. Refused unless FILE hashes to HEX and every page
        file matches the hash the index lists for it. Same as POST /api/runs/{id}/source-freeze.

Exit status 0 when the step was recorded, 2 when it was refused (the reason is printed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from runs import build_run, sources
from server import jobs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.run_steps", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    steps = ap.add_subparsers(dest="step", required=True)
    stop = steps.add_parser("record_operator_stop")
    stop.add_argument("run_id")
    stop.add_argument("--runs-dir", type=Path, default=build_run.RUNS)
    freeze = steps.add_parser("record_source_freeze")
    freeze.add_argument("run_id")
    freeze.add_argument("--index", type=Path, required=True)
    freeze.add_argument("--index-sha256", required=True)
    freeze.add_argument("--runs-dir", type=Path, default=build_run.RUNS)
    args = ap.parse_args(argv)

    if not (args.runs_dir / args.run_id / "run.json").is_file():
        print(f"There is no run {args.run_id} under {args.runs_dir}.", file=sys.stderr)
        return 2
    try:
        if args.step == "record_operator_stop":
            run = jobs.record_operator_stop(args.runs_dir, args.run_id)
        else:
            run = jobs.record_source_freeze(args.runs_dir, args.run_id, sources.load_index(args.index),
                                            args.index_sha256)
    except jobs.JobError as e:
        print(e.message, file=sys.stderr)
        return 2
    except sources.SourcesError as e:
        print(str(e), file=sys.stderr)
        return 2
    meta = run["run"]
    print(f"{meta['id']}: {meta['status']} ({meta['status_reason']})")
    print(f"  {meta['trail'][-1]['step']}: {meta['trail'][-1]['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
