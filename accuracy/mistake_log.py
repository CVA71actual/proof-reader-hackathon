"""The mistake log of a run: counts by type, model and who caught it, after the person's decisions.

    mistake_log(run, decisions)   -> {"entries", "by_type", "by_model", "by_caught_by", "counts", "total"}
    load_run(run_dir)             -> (run, decisions) from runs/<id>/

Built from the state rule (runs/state.py current_state, "mistake_list"): run.json's mistakes[] plus every fix
still in effect that is a demonstrated mistake. counts is a list of {type, label, model, caught_by, n}.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from accuracy import types as T
from accuracy.score import model_family
from runs.check_load import bind_decisions_on_load, load_bound_run
from runs.state import current_state

NO_TYPE = "NO_TYPE"


def load_run(run_dir: str | Path) -> tuple[dict, list[dict]]:
    run_dir = Path(run_dir)
    run = load_bound_run(run_dir)
    path = run_dir / "decisions.jsonl"
    decisions = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    return run, bind_decisions_on_load(run, decisions, run_dir)


def mistake_log(run: dict, decisions: list[dict] | None = None) -> dict:
    state = current_state(run, decisions or [])
    entries = []
    for m in state["mistake_list"]:
        entries.append({**m, "type": m.get("type") or NO_TYPE, "model": model_family(m.get("model"))})
    c = Counter((e["type"], e["model"], e["caught_by"]) for e in entries)
    order = {code: i for i, code in enumerate(T.CODES + (NO_TYPE,))}
    counts = [{"type": t, "label": T.label(t if t != NO_TYPE else None), "model": m, "caught_by": b, "n": n}
              for (t, m, b), n in sorted(c.items(), key=lambda kv: (order.get(kv[0][0], 99), kv[0][1],
                                                                    T.CAUGHT_BY.index(kv[0][2])))]
    return {"entries": entries, "counts": counts, "total": len(entries),
            "by_type": dict(Counter(e["type"] for e in entries)),
            "by_model": dict(Counter(e["model"] for e in entries)),
            "by_caught_by": {b: sum(1 for e in entries if e["caught_by"] == b) for b in T.CAUGHT_BY}}


def log_from_score(*scores: dict) -> list[dict]:
    """The scorer's own mistake listing (every item not found that carries a type), for a key run:
    {type, model, caught_by ('code' when a check flagged it, else None: not caught), bucket, item}."""
    out = []
    for sc in scores:
        if not sc:
            continue
        for bucket, items in sc.items():
            if bucket in ("found", "counts", "model", "models", "N", "M", "M_in_window", "window", "unreadable", "complete",
                          "unscored", "excluded_by_window") \
                    or not isinstance(items, list):
                continue
            for it in items:
                if bucket == "missed" and not it.get("type"):
                    continue
                out.append({"type": it.get("type") or NO_TYPE, "model": sc["model"],
                            "caught_by": "code" if it.get("caught") else None, "bucket": bucket, "item": it})
    return out
