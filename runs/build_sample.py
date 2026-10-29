"""Build the sample run for the screens: the REAL S1 table plus a few made-up event rows.

Usage (from the repo root):
    python -m runs.build_sample [--out-dir DIR]

Writes into runs/sample/:
    run.json                       label "Sample run", run.sample = true
    pages/*.png, pages/*.words.json  page images and words for every source
    decisions.jsonl                a few example decisions on the sample rows, including a keep on a
                                   row whose checks failed (e7) and one whose readers disagreed (e8),
                                   and a fix on e7 that code re-checks (it fails) before the person
                                   undoes it

What is real and what is not:
    - table t1 is the real Muse Spark reading of IONS ARS page 95, with the real code checks
    - every event row is written by hand for this sample and carries "sample": true
    - the made-up sources are tiny generated pages titled "Sample source"; their companies
      ("Samplebio One" ...) and tickers ("SMPL1" ...) do not exist
    - every check on every row is run by the real check functions; nothing is typed in by hand
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pymupdf

from capture import read_table
from capture.capture_page import page_words
from checks import event_checks
from runs import prices, recheck
from runs.build_entry import plain_type
from runs.build_run import (
    IONS_SOURCE_ID,
    MUSE_SPARK,
    RUNS,
    SCHEMA_VERSION,
    build_table,
    file_sha256,
    ions_source,
    render_page,
    to_contract_check,
    write_run,
)

SAMPLE_DIR = RUNS / "sample"
SAMPLE_LABEL = "Sample run"
SAMPLE_PUBLISHER = "Sample source (made up for the sample run)"
SECOND_READER = "Muse Spark Open"
MODEL_NAMES = (MUSE_SPARK, SECOND_READER)
BUILDER = "runs.build_sample"  # the code that routes and applies; a Code step's who, never a model

STARTED = "2026-10-27T15:00:00+00:00"
FINISHED = "2026-10-27T15:20:00+00:00"
CAPTURED = "2026-10-27T15:01:00+00:00"


def at(minute: int, second: int = 0) -> str:
    return f"2026-10-27T15:{minute:02d}:{second:02d}+00:00"


SAMPLE_EVENT_INSTRUCTIONS = """\
SAMPLE INSTRUCTIONS, written for the sample run. No model was called for the event rows of this run.
You are reading one page. List every dated company event on it as JSON:
{"events": [{"ticker": "...", "company": "...", "event_type": "...", "title": "...",
  "date_text": "<date as printed>", "date": "<YYYY-MM-DD or null>", "quote": "<the sentence as printed>",
  "box": [x0, y0, x1, y1]}]}
Use only words printed on the page. If the page has no date for an event, give the printed words and null.
"""

# ---------------------------------------------------------------- made-up sources

HEADER = "SAMPLE SOURCE - NOT A REAL DOCUMENT"

SAMPLE_SOURCES = [
    {
        "id": "smp-release-a", "kind": "company_release", "published_date": "2026-10-01",
        "title": "Sample source: Samplebio One news release",
        "lines": [
            "Samplebio One (SMPL1) news release",
            "Topline results from the Phase 3 STAR-1 trial are expected on March 12, 2027.",
            "The company plans to present 52-week data in Q3 2027.",
            "An FDA advisory committee meeting is scheduled for May 4, 2027.",
            "Samplebio One will present at a medical meeting later this year.",
        ],
    },
    {
        "id": "smp-filing-b", "kind": "filing", "published_date": "2026-09-15",
        "title": "Sample source: Samplebio Two quarterly report",
        "lines": [
            "Samplebio Two (SMPL2) quarterly report, page 12",
            "The FDA has set a target action date of June 30, 2027.",
            "Our partner Samplebio Three will host its investor day on September 15, 2027.",
        ],
    },
    {
        "id": "smp-calendar-c", "kind": "third_party_calendar", "published_date": "2026-10-20",
        "title": "Sample source: third-party events calendar",
        "lines": [
            "Upcoming events (third-party calendar)",
            "SMPL4 Phase 2 data readout April 20, 2027",
        ],
    },
    {
        "id": "smp-scan-d", "kind": "other", "published_date": None, "scanned": True,
        "title": "Sample source: scanned letter with no text layer",
        "lines": [
            "Samplebio Seven (SMPL7) letter to holders",
            "We look forward to sharing data later this year.",
        ],
    },
    {
        "id": "smp-release-e", "kind": "company_release", "published_date": "2026-10-10",
        "title": "Sample source: Samplebio Six news release",
        "lines": [
            "Samplebio Six (SMPL6) news release",
            "Samplebio Six will report final results on July 19, 2027.",
            "Samplebio Six expects to file its application on August 2, 2027.",
        ],
    },
]


def make_sample_pdf(src: dict, out_pdf: Path) -> None:
    """A one-page made-up document. A 'scanned' one is flattened to an image, so it has no words."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((54, 60), HEADER, fontname="helv", fontsize=12)
    y = 110
    for line in src["lines"]:
        page.insert_text((54, y), line, fontname="helv", fontsize=11)
        y += 28
    if src.get("scanned"):
        pix = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        flat = pymupdf.open()
        flat.new_page(width=612, height=792).insert_image(pymupdf.Rect(0, 0, 612, 792), pixmap=pix)
        doc.close()
        doc = flat
    doc.set_metadata({})
    doc.save(out_pdf, garbage=4, deflate=True, no_new_id=True)
    doc.close()


def sample_source(src: dict, run_dir: Path, tmp_dir: Path) -> tuple[dict, dict]:
    pdf = tmp_dir / f"{src['id']}.pdf"
    make_sample_pdf(src, pdf)
    words_doc = page_words(pdf)
    words_doc["page_number"] = 1
    image_rel, words_rel = f"pages/{src['id']}-p1.png", f"pages/{src['id']}-p1.words.json"
    render_page(pdf, run_dir / image_rel)
    (run_dir / words_rel).write_text(json.dumps(words_doc, indent=1) + "\n")
    source = {
        "id": src["id"],
        "publisher": SAMPLE_PUBLISHER,
        "kind": src["kind"],
        "title": src["title"],
        "url": None,
        "published_date": src["published_date"],
        "captured_at": CAPTURED,
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "pages": [{"page_number": 1, "image": image_rel, "width_pt": words_doc["page_width"],
                   "height_pt": words_doc["page_height"], "words": words_rel,
                   "image_sha256": file_sha256(run_dir / image_rel),
                   "words_sha256": file_sha256(run_dir / words_rel)}],
    }
    return source, words_doc


# ---------------------------------------------------------------- made-up rows


def box_of_text(text: str, words: list[dict], pad: float = 0.5) -> list[float]:
    """The box a careful reader would propose: around the printed words of `text`."""
    r = event_checks.quote_match(text, words)
    if r.result != "pass":
        raise ValueError(f"sample text is not on the page: {text!r}")
    ids = set(r.compared["word_ids"])
    ws = [w for w in words if w["id"] in ids]
    return [round(min(w["x0"] for w in ws) - pad, 2), round(min(w["y0"] for w in ws) - pad, 2),
            round(max(w["x1"] for w in ws) + pad, 2), round(max(w["y1"] for w in ws) + pad, 2)]


run_checks = recheck.row_checks

# SAMPLE token counts, fixed by hand for the sample run (no model was called for its event rows). Each Model step
# gets its step's numbers and the cost from runs/prices.py, so the screens can show cost per step; the run is
# labelled "Sample run" and every row "sample": true, so these numbers are always shown as sample numbers.
SAMPLE_PROMPT_TOKENS = 1200
SAMPLE_TOKENS = {  # step -> (prompt, completion)
    "Proposed": (SAMPLE_PROMPT_TOKENS, 150),
    "Answer received": (SAMPLE_PROMPT_TOKENS, 40),
    "Read again": (1260, 30),
    "Second reading": (SAMPLE_PROMPT_TOKENS, 140),
}


def add_sample_cost(trail: list[dict]) -> list[dict]:
    """The sample's fixed token counts and their cost on every Model step; the prompt count on Instructions sent."""
    for step in trail:
        if step["step"] == "Instructions sent":
            step["tokens"] = {"prompt": SAMPLE_PROMPT_TOKENS, "completion": None}
        elif step["tag"] == "Model":
            step.update(prices.model_cost_fields(step["who"], *SAMPLE_TOKENS[step["step"]]))
    return trail


def check_steps(results, minute: int) -> list[dict]:
    return [{"step": "Checked", "tag": "Code", "who": r.check,
             "text": f"{r.label}: {r.result.replace('_', ' ')}. {r.reason}", "at": at(minute, 10 + i)}
            for i, r in enumerate(results)]


ROW_SPECS = [
    # id, source, fields..., plus what happened in the run
    dict(id="e1", source="smp-release-a", ticker="SMPL1", company="Samplebio One", event_type="trial_readout",
         title="Phase 3 STAR-1 topline results", date_text="March 12, 2027", date="2027-03-12",
         quote="Topline results from the Phase 3 STAR-1 trial are expected on March 12, 2027.",
         group="kept", reason="Every check passed and the second reader agreed.", second="agrees"),
    dict(id="e2", source="smp-release-a", ticker="SMPL1", company="Samplebio One",
         event_type="trial_results", title="52-week data presentation",
         date_text="Q3 2027", date="2027-07-01", date_end="2027-09-30",
         quote="The company plans to present 52-week data in Q3 2027.",
         group="kept", reason="Every check passed. The source names a quarter, not a day, so the row keeps the quarter "
                              "as stated.", second="agrees"),
    dict(id="e3", source="smp-filing-b", ticker="SMPL2", company="Samplebio Two", event_type="regulatory_decision",
         title="FDA target action date", date_text="June 30, 2027", date="2027-06-30", first_date="2027-06-03",
         quote="The FDA has set a target action date of June 30, 2027.",
         group="corrected", reason="The date check caught a wrong day; the day was read again from the quote.",
         second="agrees", mistake=("code", "PERIOD_MISREAD", "Date changed from 2027-06-03 to 2027-06-30 after the date check failed.")),
    dict(id="e4", source="smp-filing-b", ticker="SMPL3", company="Samplebio Three", event_type="investor_day",
         title="Investor day", date_text="September 15, 2027", date="2027-09-15",
         first_ticker=("SMPL2", "Samplebio Two"),
         quote="Our partner Samplebio Three will host its investor day on September 15, 2027.",
         group="corrected", reason="The second reader saw that the event belongs to the partner, not the filer.",
         second="fixed", mistake=("second_reader", "WRONG_COMPANY",
                                  "Company changed from Samplebio Two (SMPL2) to Samplebio Three (SMPL3).")),
    dict(id="e5", source="smp-release-a", ticker="SMPL1", company="Samplebio One", event_type="advisory_committee",
         title="FDA advisory committee meeting", date_text="May 4, 2027", date="2027-05-04",
         first_type="regulatory_decision",
         quote="An FDA advisory committee meeting is scheduled for May 4, 2027.",
         group="corrected", reason="You changed the event type while the run was going; the checks ran again after.",
         second="agrees", mistake=("person", "WRONG_EVENT_TYPE", "Event type changed from regulatory_decision to advisory_committee.")),
    dict(id="e6", source="smp-calendar-c", ticker="SMPL4", company="Samplebio Four", event_type="trial_readout",
         title="Phase 2 data readout", date_text="April 20, 2027", date="2027-04-20",
         quote="SMPL4 Phase 2 data readout April 20, 2027",
         group="removed", reason="House rule: a third-party calendar is never a source on its own.",
         second="agrees", exclusion=("code", "third_party_calendar_only")),
    dict(id="e7", source="smp-release-e", ticker="SMPL6", company="Samplebio Six", event_type="trial_readout",
         title="Final results", date_text="July 9, 2027", date="2027-07-09",
         quote="Samplebio Six will report final results on July 9, 2027.",
         box_text="Samplebio Six will report final results on July 19, 2027.",
         group="open", reason="The quote is not printed on the page as given. Waiting for you.", second="none"),
    dict(id="e8", source="smp-release-e", ticker="SMPL6", company="Samplebio Six", event_type="regulatory_decision",
         title="Application filing", date_text="August 2, 2027", date="2027-08-02",
         quote="Samplebio Six expects to file its application on August 2, 2027.",
         group="open", reason="The two readers named different event types. Waiting for you.",
         second="disagrees", second_type="regulatory_submission"),
    dict(id="e9", source="smp-scan-d", ticker="SMPL7", company="Samplebio Seven", event_type="unknown",
         title="No event could be read from this page", date_text=None, date=None, quote=None,
         no_box=True, readable=False,
         group="unverified", reason="The page is a scan with no text layer and the reader's answer could not be read.",
         second="none"),
    dict(id="e10", source=IONS_SOURCE_ID, ticker="IONS", company="Ionis Pharmaceuticals, Inc.",
         event_type="earnings", title="Full-year 2025 results in the annual report",
         date_text="December 31, 2025", date="2025-12-31", quote="Year Ended December 31, 2025",
         no_box=True,
         group="unverified", reason="The reader proposed no place on the page, so the citation could not be checked.",
         second="none"),
    dict(id="e11", source="smp-release-a", ticker="SMPL1", company="Samplebio One",
         event_type="conference_presentation", title="Medical meeting presentation",
         date_text="later this year", date=None,
         quote="Samplebio One will present at a medical meeting later this year.",
         group="kept", reason="The checks that apply passed; no exact day or period was printed, so there was "
                              "nothing to compare.", second="agrees"),
]


def recheck_line(at_: str, row: dict, fix: dict, source: dict, words_doc: dict) -> dict:
    """The decisions line code writes after running the real checks again on a person's fix."""
    return recheck.recheck_row(at_, {**row, fix["field"]: fix["to"]}, source, words_doc, field=fix["field"],
                               before={**row, fix["field"]: fix["from"]})


def when(row: dict) -> str:
    if row.get("date_end") and row.get("date_text"):     # a period as the source wrote it
        return row["date_text"]
    if row["date"] and row.get("date_end"):
        return f"{row['date']} to {row['date_end']}"
    return row["date"] or row["date_text"]


def build_row(spec: dict, source: dict, words_doc: dict, minute: int) -> tuple[dict, list[dict]]:
    words = words_doc["words"] if words_doc else []
    box = None
    if not spec.get("no_box"):
        box = box_of_text(spec.get("box_text", spec["quote"]), words)
    row = {
        "id": spec["id"], "ticker": spec["ticker"], "company": spec["company"],
        "event_type": spec["event_type"], "title": spec["title"], "date_text": spec["date_text"],
        "date": spec["date"], "date_end": spec.get("date_end"), "quote": spec["quote"], "source_id": source["id"], "page_number": 1 if
        source["id"] != IONS_SOURCE_ID else source["pages"][0]["page_number"], "box": box,
        "proposed_by": MUSE_SPARK,
    }
    trail = [{"step": f"Sent the page to {MUSE_SPARK}", "tag": "Code", "who": BUILDER,
              "text": f"Code chose what {MUSE_SPARK} would read and sent it, with the instructions below.",
              "at": at(minute)},
             {"step": "Instructions sent", "tag": "Code", "who": BUILDER,
              "text": SAMPLE_EVENT_INSTRUCTIONS, "at": at(minute)}]
    readable = spec.get("readable", True)
    if not readable:
        trail.append({"step": "Answer received", "tag": "Model", "who": MUSE_SPARK,
                      "text": "The answer could not be understood as a list of events.", "at": at(minute, 5)})
    else:
        first = dict(row)
        if "first_date" in spec:
            first["date"] = spec["first_date"]
        if "first_ticker" in spec:
            first["ticker"], first["company"] = spec["first_ticker"]
        if "first_type" in spec:
            first["event_type"] = spec["first_type"]
        trail.append({"step": "Proposed", "tag": "Model", "who": MUSE_SPARK,
                      "text": f"{first['ticker']} {plain_type(first['event_type'])} on {when(first)}: "
                              f"“{first['quote']}”", "at": at(minute, 5)})
        trail.append({"step": "Took the reading into the row", "tag": "Code", "who": BUILDER,
                      "text": "Copied the reader's answer for this row into the row as a proposal. Nothing is "
                              "checked yet.", "at": at(minute, 6)})
        if "first_date" in spec:
            failed = event_checks.date_match(first["date_text"], first["date"], first["quote"],
                                             event_type=first["event_type"], title=first["title"])
            assert failed.result == "fail"
            trail += check_steps([failed], minute)
            trail.append({"step": "Sent back", "tag": "Code", "who": BUILDER,
                          "text": f"Sent back to {MUSE_SPARK} because {failed.label} failed.", "at": at(minute, 20)})
            trail.append({"step": "Read again", "tag": "Model", "who": MUSE_SPARK,
                          "text": f"Read the date in “{spec['date_text']}” as {spec['date']}.", "at": at(minute, 25)})
            trail.append({"step": "Fixed", "tag": "Code", "who": BUILDER,
                          "text": f"Date changed from {first['date']} to {spec['date']}, as the new reading gave.",
                          "at": at(minute, 30)})
    results = run_checks(row, source, words_doc)
    trail += check_steps(results, minute + 1)
    second = spec["second"]
    if second == "agrees":
        trail.append({"step": "Second reading", "tag": "Model", "who": SECOND_READER,
                      "text": "Read the same row.", "at": at(minute + 1, 40)})
    elif second == "fixed":
        trail.append({"step": "Second reading", "tag": "Model", "who": SECOND_READER,
                      "text": f"Read the company as {spec['company']} ({spec['ticker']}), not "
                              f"{spec['first_ticker'][1]} ({spec['first_ticker'][0]}): the event belongs to the "
                              f"partner.", "at": at(minute + 1, 40)})
        trail.append({"step": "Fixed", "tag": "Code", "who": BUILDER,
                      "text": f"Company and ticker changed from {spec['first_ticker'][1]} ({spec['first_ticker'][0]}) "
                              f"to {spec['company']} ({spec['ticker']}), as the second reading gave.",
                      "at": at(minute + 1, 50)})
    elif second == "disagrees":
        trail.append({"step": "Second reading", "tag": "Model", "who": SECOND_READER,
                      "text": f"Read the event type as {plain_type(spec['second_type'])}, not {plain_type(spec['event_type'])}.",
                      "at": at(minute + 1, 40)})
    if "first_type" in spec:
        trail.append({"step": "Changed by you", "tag": "Person", "who": "person",
                      "text": f"Event type changed from {plain_type(spec['first_type'])} to {plain_type(spec['event_type'])}.",
                      "at": at(minute + 2)})
        trail += check_steps(run_checks(row, source, words_doc), minute + 2)
    if spec["group"] == "removed":
        trail.append({"step": "Removed", "tag": "Code", "who": "source_rule",
                      "text": spec["reason"], "at": at(minute + 2, 30)})

    status = event_checks.row_status(results, readable=readable,
                                     second_reader_agrees=(False if second == "disagrees" else None))
    if second == "disagrees":
        row["disagreement"] = {"field": "event_type", "first_reader": spec["event_type"],
                               "second_reader": spec["second_type"]}
    row.update({
        "status": status, "group": spec["group"], "reason": spec["reason"], "sample": True,
        "checks": [to_contract_check(r.to_dict()) for r in results], "trail": add_sample_cost(trail),
    })
    return row, results


EXPECTED_STATUS = {"e1": "matched", "e2": "matched", "e3": "matched", "e4": "matched", "e5": "matched",
                   "e6": "failed", "e7": "failed", "e8": "undecided", "e9": "unreadable", "e10": "proposed",
                   "e11": "matched"}


def build_sample_run(run_dir: Path) -> tuple[dict, list[dict]]:
    import tempfile

    manifest = json.loads((RUNS.parent / "fixtures" / "MANIFEST.json").read_text())
    reading = json.loads(read_table.READING_PATH.read_text())
    if reading.get("fake"):
        raise ValueError("The sample run uses the real reading only.")
    run_dir.mkdir(parents=True, exist_ok=True)
    ions, ions_words = ions_source(manifest, run_dir)
    sources, words_by_source = [ions], {ions["id"]: ions_words}
    with tempfile.TemporaryDirectory() as tmp:
        for src in SAMPLE_SOURCES:
            s, wd = sample_source(src, run_dir, Path(tmp))
            sources.append(s)
            words_by_source[s["id"]] = wd
    by_id = {s["id"]: s for s in sources}
    table = build_table(reading, ions_words, table_id="t1", source_id=ions["id"],
                        page_number=manifest["page_number"])

    rows, mistakes, exclusions = [], [], []
    for i, spec in enumerate(ROW_SPECS):
        row, _ = build_row(spec, by_id[spec["source"]], words_by_source[spec["source"]], minute=1 + i)
        if row["status"] != EXPECTED_STATUS[row["id"]]:
            raise AssertionError(f"{row['id']}: status {row['status']}, expected {EXPECTED_STATUS[row['id']]}")
        rows.append(row)
        if "mistake" in spec:
            caught_by, mtype, what = spec["mistake"]
            mistakes.append({"id": f"m{len(mistakes) + 1}", "target": row["id"], "what_changed": what,
                             "caught_by": caught_by, "type": mtype})
        if "exclusion" in spec:
            by, rule = spec["exclusion"]
            exclusions.append({"id": f"x{len(exclusions) + 1}", "target": row["id"], "rule": rule,
                               "reason": row["reason"], "by": by})

    run = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": "sample", "number": 2, "label": SAMPLE_LABEL, "sample": True,
            "started_at": STARTED, "finished_at": FINISHED,
            "choices": {
                "companies": ["IONS", "SMPL1", "SMPL2", "SMPL3", "SMPL4", "SMPL6", "SMPL7"],
                "event_types": ["trial_readout", "regulatory_decision", "advisory_committee",
                                "conference_presentation", "investor_day", "earnings"],
                "time_window_months": 12, "spending_limit_usd": 5.0,
                "note_for_agent": "Sample note: skip events that only appear on a calendar.",
            },
            "spend_usd": prices.run_spend_usd(rows),  # the sum of the sample's Model step costs
            "prices": prices.snapshot(),
        },
        "sources": sources,
        "tables": [table],
        "rows": rows,
        "mistakes": mistakes,
        "exclusions": exclusions,
    }
    e7 = next(r for r in rows if r["id"] == "e7")
    e7_fix = {"field": "date", "from": "2027-07-09", "to": "2027-07-19"}
    decisions = [
        {"at": "2026-10-27T16:00:00+00:00", "target": "e8", "action": "keep",
         "note": "It is a filing, but keep it.", "fix": None, "by": "person"},
        {"at": "2026-10-27T16:01:00+00:00", "target": "e7", "action": "fix", "note": None,
         "fix": dict(e7_fix), "by": "person"},
        recheck_line("2026-10-27T16:01:30+00:00", e7, e7_fix, by_id[e7["source_id"]],
                     words_by_source[e7["source_id"]]),
        {"at": "2026-10-27T16:02:00+00:00", "target": "e7", "action": "undo", "note": None, "fix": None,
         "by": "person"},
        {"at": "2026-10-27T16:03:00+00:00", "target": "e7", "action": "keep",
         "note": "Keep it; I will check the release myself.", "fix": None, "by": "person"},
        {"at": "2026-10-27T16:04:00+00:00", "target": "e10", "action": "remove",
         "note": "A past annual result, not an upcoming event.", "fix": None, "by": "person"},
    ]
    return run, decisions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=SAMPLE_DIR)
    args = ap.parse_args(argv)
    run, decisions = build_sample_run(args.out_dir)
    out = write_run(run, args.out_dir)
    (args.out_dir / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in decisions))
    groups: dict[str, int] = {}
    for r in run["rows"]:
        groups[r["group"]] = groups.get(r["group"], 0) + 1
    print(f"Wrote {out}: {len(run['rows'])} sample rows {groups}, {len(run['mistakes'])} mistakes, "
          f"{len(run['exclusions'])} exclusions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
