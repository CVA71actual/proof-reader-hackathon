"""Red Team round 11. Correctness audit on product main 2bd17e8.

A failure is a finding: the assertion is the result the README requires of
"matched". No model call, no network, no key. The rows are the committed
twelve-4 run, read as saved.
"""

import json
from pathlib import Path

from runs.state import current_state

RUN = Path(__file__).resolve().parents[2] / "runs" / "twelve-4" / "run.json"


def test_a_check_that_could_not_run_is_not_marked_matched():
    """README (runs/README.md, Statuses): matched means no check failed, none
    was left unrun, and at least one passed.

    twelve-4 e27 and e52 are undecided in run.json. The second reader filled
    one field the first reader left empty, and Date read as written could not
    run. current_state then marks both matched and kept.
    """
    run = json.loads(RUN.read_text())
    state = current_state(run, [])
    for rid in ("e27", "e52"):
        row = next(r for r in run["rows"] if r["id"] == rid)
        date = next(c for c in row["checks"] if c["name"] == "date_match")
        assert date["result"] == "could_not_run", rid
        got = state["targets"][rid]
        assert got["status"] != "matched", rid
        assert got["group"] != "kept", rid
