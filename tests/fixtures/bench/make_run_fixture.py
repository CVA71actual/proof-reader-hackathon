"""Build the synthetic PTGX run folder used by tests/test_bench_score.py (no model, network or paid call).

    python -m tests.fixtures.bench.make_run_fixture            # writes tests/fixtures/bench/run-ptgx-synthetic/

The folder is a real run folder (run.json, decisions.jsonl, pages/): one tiny generated page, made the same way
as runs/sample (runs.build_sample.sample_source), with every row's quote printed on it, so runs.check_load and
runs.state load it exactly as the UI and exports do. Every row is marked "sample": true. The facts on the page are
made up for the scorer's test cases and are NOT PTGX disclosures; the benchmark file ptgx.yaml holds the public facts.

Cases the rows cover (see tests/test_bench_score.py):
    e1  found             PN-881 Phase 2b psoriasis initiation, early Q1 2027 (upcoming); saved with no date fields,
                          the loader derives January 2027 from the date check, as it did on the real run
    e2  found             PN-8047 Phase 1 initiation, Q1 2027 (upcoming)
    e3  misplaced         Takeda Capital Markets Day, Dec 11 2026, but the research proposal says "context" -> Past
    e4  misplaced         PN-477o Phase 1 initiation, but dated second half of 2027 (benchmark: 1H 2027)
    e5, e6  duplicate     two rows for the J&J Enterprise Business Review on Dec 8 2026
    e7, e8  duplicate     two rows with the same program + action + period that match no benchmark item (extras)
    e9  unmatched         a routine earnings call, not in the benchmark
    e10..e13  found       four named investor conferences (Wolfe, Truist, UBS, Jefferies) on four dates: each
                          benchmark item names its conference, so each is one separate found item, no duplicates
    e14, e15  duplicate   two more PN-881 Phase 2b psoriasis rows with the same Q1 2027 period (same program +
                          action + period as each other; e1's derived period is January, so e1 is not in the group)
    e16  distinct         PN-881 Phase 1 completion, Q1 2027: shares the program, the period and the word
                          "psoriasis" with e14/e15 but is a different event (action differs), never a duplicate
    (missed items are simply absent: ICONIC-PsA 2, TGA, ACR, the OLE, the Takeda payments...)
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from checks import event_checks                                             # noqa: E402
from runs import recheck                                                    # noqa: E402
from runs.build_run import to_contract_check                                # noqa: E402
from runs.build_sample import box_of_text, sample_source                    # noqa: E402

OUT = Path(__file__).resolve().parent / "run-ptgx-synthetic"
RUN_ID = "run-ptgx-synthetic"
STARTED = "2026-11-03T09:00:00+00:00"
TICKER, COMPANY = "PTGX", "Protagonist Therapeutics, Inc."

SOURCE = {
    "id": "src-bench-ptgx", "kind": "company_release", "published_date": "2026-11-02",
    "title": "Bench fixture page (made-up PTGX update)",
    "lines": [
        "PN-881: initiation of the Phase 2b psoriasis program is expected in early Q1 2027.",
        "PN-8047: Phase 1 initiation is anticipated in Q1 2027.",
        "Takeda will host its Capital Markets Day on December 11, 2026.",
        "PN-477o: Phase 1 initiation is planned for the second half of 2027.",
        "J&J will hold its Enterprise Business Review on December 8, 2026.",
        "Johnson & Johnson business review webcast is scheduled for December 8, 2026.",
        "Rusfertide commercial launch preparations continue, with launch expected in Q4 2026.",
        "The rusfertide launch is expected in the fourth quarter of 2026.",
        "The company will host its third quarter earnings call on November 5, 2026.",
        "Management will present at the Wolfe Research Healthcare Conference on November 10, 2026.",
        "Management will join a fireside chat at the Truist Securities Healthcare Conference on November 12, 2026.",
        "The company will present at the UBS Global Healthcare Conference on November 16, 2026.",
        "Management will present at the Jefferies London Healthcare Conference on November 17, 2026.",
        "The PN-881 Phase 2b psoriasis study is planned to start in Q1 2027.",
        "Initiation of the Phase 2b study of PN-881 in psoriasis is expected in the first quarter of 2027.",
        "Completion of the PN-881 Phase 1 study is expected in Q1 2027, supporting the psoriasis program.",
    ],
}

# (id, title, program, type_hint, action, placement_proposal, date_text, date, date_end, quote line index, relevance)
ROWS = [
    ("e1", "PN-881 Phase 2b psoriasis program initiation", "PN-881", "trial initiation", "initiate Phase 2b",
     "upcoming", "early Q1 2027", None, None, 0,      # as the real research run saved it: the loader derives the period
     "Wholly-owned oral IL-17 antagonist advancing into Phase 2b."),
    ("e2", "PN-8047 Phase 1 initiation", "PN-8047", "trial initiation", "initiate Phase 1",
     "upcoming", "Q1 2027", "2027-01-01", "2027-03-31", 1, "Hepcidin mimetic entering the clinic."),
    ("e3", "Takeda Capital Markets Day", None, "partner event", "host capital markets day",
     "context", "December 11, 2026", "2026-12-11", None, 2, "Partner Takeda may discuss rusfertide."),
    ("e4", "PN-477o Phase 1 initiation", "PN-477o", "trial initiation", "initiate Phase 1",
     "upcoming", "the second half of 2027", "2027-07-01", "2027-12-31", 3, "Oral obesity candidate."),
    ("e5", "J&J Enterprise Business Review", None, "partner event", "host business review",
     "upcoming", "December 8, 2026", "2026-12-08", None, 4, "Partner J&J may update on icotrokinra."),
    ("e6", "Johnson & Johnson business review webcast", None, "partner event", "host business review",
     "upcoming", "December 8, 2026", "2026-12-08", None, 5, "Partner J&J webcast."),
    ("e7", "Rusfertide commercial launch", "rusfertide", "launch", "commercial launch",
     "upcoming", "Q4 2026", "2026-10-01", "2026-12-31", 6, "First commercial product."),
    ("e8", "Rusfertide launch", "rusfertide", "launch", "commercial launch",
     "upcoming", "the fourth quarter of 2026", "2026-10-01", "2026-12-31", 7, "Launch timing restated."),
    ("e9", "Third quarter earnings call", None, "earnings", "host earnings call",
     "upcoming", "November 5, 2026", "2026-11-05", None, 8, "Routine quarterly call."),
    ("e10", "Wolfe Research Healthcare Conference presentation", None, "conference", "present at conference",
     "upcoming", "November 10, 2026", "2026-11-10", None, 9, "Investor conference appearance."),
    ("e11", "Truist Securities Healthcare Conference fireside chat", None, "conference", "present at conference",
     "upcoming", "November 12, 2026", "2026-11-12", None, 10, "Investor conference appearance."),
    ("e12", "UBS Global Healthcare Conference presentation", None, "conference", "present at conference",
     "upcoming", "November 16, 2026", "2026-11-16", None, 11, "Investor conference appearance."),
    ("e13", "Jefferies London Healthcare Conference presentation", None, "conference", "present at conference",
     "upcoming", "November 17, 2026", "2026-11-17", None, 12, "Investor conference appearance."),
    ("e14", "PN-881 Phase 2b psoriasis study start", "PN-881", "trial initiation", "initiate Phase 2b",
     "upcoming", "Q1 2027", "2027-01-01", "2027-03-31", 13, "Second row for the same program start."),
    ("e15", "Phase 2b study of PN-881 in psoriasis", "PN-881", "trial initiation", "initiate Phase 2b",
     "upcoming", "the first quarter of 2027", "2027-01-01", "2027-03-31", 14, "Restated program start."),
    ("e16", "PN-881 Phase 1 study completion", "PN-881", "trial completion", "complete Phase 1",
     "upcoming", "Q1 2027", "2027-01-01", "2027-03-31", 15, "Phase 1 completion supports the psoriasis program."),
]


def build(out_dir: Path = OUT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pages").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        source, words_doc, consumed = sample_source(SOURCE, out_dir, Path(tmp))
    rows = []
    for rid, title, program, hint, action, placement, date_text, date, date_end, line, relevance in ROWS:
        quote = SOURCE["lines"][line]
        row = {
            "id": rid, "ticker": TICKER, "company": COMPANY, "event_type": "unknown", "type_hint": hint,
            "relevance": relevance, "conditions": [], "subject_company": COMPANY, "relation": "wholly-owned",
            "title": title, "date_text": date_text, "date": date, "date_end": date_end, "quote": quote,
            "program": program, "indication": "psoriasis" if rid in ("e1", "e14", "e15") else None,
            "source_id": source["id"], "page_number": 1, "box": box_of_text(quote, words_doc["words"]),
            "proposed_by": "bench fixture (no model)", "sample": True,
            "research": {"label": "Bench fixture — made-up row", "revision": 1, "call_id": "bench",
                         "ticker": TICKER, "action": action, "modality": None, "relevance_kind": "interpretation",
                         "placement_proposal": placement, "prior_row_ids": [], "evidence": [],
                         "primary_span_index": 0, "near_duplicates": [], "revisions": []},
        }
        results = recheck.row_checks(row, source, words_doc, consumed_artifact=consumed)
        status = event_checks.row_status(results)
        row.update({"status": status, "group": "unverified", "reason": "Bench fixture row; checks recorded.",
                    "checks": [to_contract_check(r.to_dict()) for r in results], "trail": []})
        rows.append(row)
    run = {
        "schema_version": 1,
        "run": {"id": RUN_ID, "number": 1, "label": "PTGX · bench scorer fixture (synthetic)", "sample": True,
                "started_at": STARTED, "finished_at": STARTED, "as_of": "2026-11-03",
                "choices": {"companies": [TICKER], "event_types": [], "time_window_months": 6,
                            "spending_limit_usd": None, "note_for_agent": None},
                "spend_usd": 0.0, "status": "done", "pages_read": 1, "pages_total": 1},
        "sources": [source], "tables": [], "rows": rows, "mistakes": [], "exclusions": [],
    }
    (out_dir / "run.json").write_text(json.dumps(run, indent=1) + "\n")
    (out_dir / "decisions.jsonl").write_text("")
    (out_dir / "PROVENANCE.txt").write_text(
        "Generated by tests/fixtures/bench/make_run_fixture.py. Synthetic page and rows for the bench scorer tests; "
        "not PTGX disclosures. No model, network or paid call.\n")
    return run


if __name__ == "__main__":
    run = build()
    for r in run["rows"]:
        print(r["id"], r["status"], [c["result"] for c in r["checks"]], r["date"], r["date_end"])
    print("wrote", OUT, hashlib.sha256((OUT / "run.json").read_bytes()).hexdigest()[:12])
