"""LIVE smoke test of the S1 research loop: ONE company, real Muse Spark Responses API calls and real public fetches.

    NOT RUN BY THE TESTS. It spends money. Run it only with the Operator's go and CVA's stated cap.

    python -m tools.research_smoke --ticker PTGX --cap-usd 3 --i-have-the-operator-go [--runs-dir DIR]

REVIEW ONLY (RESEARCHER-FULL-001 S2): review an EXISTING run's saved draft, without researching again:

    python -m tools.research_smoke --review RUN_DIR --cap-usd 1 --i-have-the-operator-go [--runs-dir DIR]

RUN_DIR is never modified: it is copied to --runs-dir first (default: a new folder under /tmp) and the copy is reviewed.
--cap-usd is the review's budget on top of what the run already spent (the copy's cap becomes spent + cap). Claude
Opus 5.5 (ANTHROPIC_API_KEY, the anthropic SDK; the declared fallback Muse Spark Open on Baseten runs only if Claude is
unavailable or refuses the key) reviews each company's draft with Brave Search (BRAVE_API_KEY) as its own search;
disputed findings go to Muse Wonder (MODEL_API_KEY). Keys are never printed.

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
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--review", type=Path, default=None, metavar="RUN_DIR",
                    help="review an existing run's saved draft (copied first; never modified); no new research")
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
    if args.review is not None:
        return review_main(args)
    if not args.ticker:
        print("Refused: give --ticker (research) or --review RUN_DIR (review only).")
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


class _NoResearch:
    """The research reader of a review-only job: research must not run, so any request is an error."""
    model = "none"
    name = "no research"

    def ready(self):
        return None

    def respond(self, req):
        raise RuntimeError("A review-only run makes no research request.")


def review_existing(src: Path, runs_dir: Path, cap_usd: float, tools=None) -> tuple[Path, dict]:
    """Copy the run folder `src` into `runs_dir` and review the COPY's saved draft (job.review_only). The review's
    budget is `cap_usd` on top of the run's spend so far. Returns (the copy's folder, run.research of the copy)."""
    import shutil
    from runs import research
    from server import jobs

    src, runs_dir = Path(src).resolve(), Path(runs_dir).resolve()
    if not (src / "run.json").is_file():
        raise ValueError(f"{src} is not a run folder (no run.json).")
    dest = runs_dir / src.name
    if dest == src or src in dest.parents or dest.exists():
        raise ValueError(f"Refused: the copy {dest} must be a new folder outside {src}.")
    runs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    doc = json.loads((dest / "run.json").read_text())
    meta = doc["run"]
    if (meta.get("choices") or {}).get("mode") != "research":
        raise ValueError("Refused: this is not a research run.")
    spent = meta.get("spend_usd") or 0
    note = (f"Review-only copy of {src} made {jobs.now_utc()}: the original cap was "
            f"{meta['choices'].get('spending_limit_usd')}; this review may spend ${cap_usd:.2f} more (cap "
            f"{spent + cap_usd:.6f}). The original folder is not modified.")
    meta["choices"]["spending_limit_usd"] = round(spent + cap_usd, 6)
    if meta.get("status") == "done":          # a done run is never written again: only this copy is reopened
        meta["status"], meta["status_reason"] = "needs_input", "Reopened for a review-only test (copy)."
    (dest / "run.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    job = research.make_job_class()(dest, _NoResearch(), tools or research.ResearchTools())
    with jobs.RUN_LOCK:
        jobs.ACTIVE_RUNS.add(dest.name)
    try:
        job.review_only(note=note)
    finally:
        with jobs.RUN_LOCK:
            jobs.ACTIVE_RUNS.discard(dest.name)
            jobs._RESERVED.pop(dest.name, None)
    return dest, json.loads((dest / "run.json").read_text())["run"]["research"]


def reviewer_line(research: dict) -> str:
    """The reviewer that RAN (from the run's record), not the configured one."""
    entries = [e for e in (research.get("review") or {}).values() if isinstance(e, dict) and "stage" in e]
    ran = list(dict.fromkeys((e.get("reviewer_ran") or {}).get("name") or e.get("reviewer") or "unknown"
                             for e in entries)) or ["none (no review ran)"]
    acc = research.get("reviewer_access") or {}
    why = ""
    choices = [e.get("reviewer_choice") or {} for e in entries]
    fb = next((c for c in choices if c.get("fallback_used")), None)
    if fb:
        refused = fb.get("primary_refused") or {}
        status = refused.get("http_status") or acc.get("http_status")
        reason = refused.get("reason") or acc.get("reason")
        why = (f" (fallback: {reason})" if reason else
               f" (fallback: {fb.get('primary')} " + (f"refused {status}" if status else
                                                      f"unavailable: {fb.get('primary_unavailable')}") + ")")
    return f"Reviewer: {' and '.join(ran)}{why}"


def review_main(args) -> int:
    from runs import research
    from capture import research_reviewer as rv
    tools = research.ResearchTools()
    model, choice = rv.choose_reviewer(tools.reviewer, tools.reviewer_fallback)      # a free model-list check only
    if model.ready():
        print(f"reviewer: {model.ready()}")
        return 2
    print(f"Configured reviewer: {rv.identity(tools.reviewer)['name']} (declared fallback "
          f"{rv.identity(tools.reviewer_fallback)['name']}); which one runs is decided at the start of the run.")
    for what, problem in (("Brave Search", tools.brave.ready()), ("adjudicator", tools.adjudicator.ready())):
        if problem:
            print(f"{what}: {problem}")
    runs_dir = args.runs_dir or Path(tempfile.mkdtemp(prefix="proof-reader-review-smoke-"))
    if "proof-reader-glass" in str(Path(runs_dir).resolve()):
        print("Refused: the smoke run never writes into the served glass checkout.")
        return 2
    print(f"Reviewing a copy of {args.review} in {runs_dir} with ${args.cap_usd:.2f} for the review ...", flush=True)
    try:
        dest, r = review_existing(args.review, runs_dir, args.cap_usd, tools)
    except ValueError as e:
        print(e)
        return 2
    meta = json.loads((dest / "run.json").read_text())["run"]
    print(f"Copy: {dest}. Spend now ${meta['spend_usd'] or 0:.4f}; unpriced calls: {meta.get('unpriced_calls', 0)}")
    print(reviewer_line(r))
    print(f"Run label: {r['label']}")
    for t, e in (r.get("review") or {}).items():
        u = e.get("usage") or {}
        rec = e.get("review") or {}
        print(f"{t}: {e['status']} — {e['reason']}")
        print(f"  label {e['label']}; clean {e['clean']}; review spend ${u.get('spend_usd', 0):.4f} "
              f"({u.get('model_requests', 0)} model requests, {u.get('search_requests', 0)} searches, "
              f"{u.get('fetches', 0)} fetches, {u.get('unpriced_calls', 0)} unpriced)")
        for f in rec.get("findings") or []:
            rows = ", ".join(f"{x['row_id']} rev {x['revision_id']}" for x in f["row_revision_ids"]) or "no row"
            print(f"  finding {f['finding_id']} {f['kind']} on {rows}: {f['text'][:300]}")
        for x in rec.get("rejected") or []:
            print(f"  rejected attempt: {'; '.join(x.get('problems') or [])[:300]}")
        for lim in rec.get("limitations") or []:
            print(f"  limitation: {lim}")
        adj = e.get("adjudication") or {}
        print(f"  adjudication: {adj.get('status')} — {adj.get('reason')}")
        for o in adj.get("outcomes") or []:
            print(f"    {o.get('dispute_id')}: {o.get('outcome')} — {(o.get('reason') or '')[:200]}")
    for x in r.get("reservation_exceeded") or []:
        print("RESERVATION EXCEEDED:", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
