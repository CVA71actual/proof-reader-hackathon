"""Job 9 item 4: the live twelve-company run, as tests that wait for it.

Two groups, each skipped with its reason until what it needs exists:

* the REAL index (capture/fetch_filings.py, PR #41): `runs/sources_index.twelve.json`, or the file named by the environment
  variable TWELVE_INDEX, and the captured filings under ~/proof-reader-docs/filings. They check the index is usable by the job
  (every page's files are there and match their hashes; the companies are the twelve; the stems and ids are unique) and run a
  few companies end to end with a fake reader that "reads" each real page's own dated sentences: no model, no key, no cost.
  TWELVE_DRY_ALL=1 runs all of them (slow: it captures every indexed page).
* the RECORDED run, `runs/twelve-1/`, written once by
      python -m runs.twelve --id twelve-1 --companies all --window 6 --limit 5.00
  They check it has the layout below, is a real (sample=false) finished run, names only indexed pages, is served by the
  decisions server with every page hash holding, and that its calendar and numbers are the state rule's.

The layout of runs/twelve-1/ (TWELVE_LAYOUT): run.json, decisions.jsonl, job.json, pages/ (one <id>.png and <id>.words.json
per page read, hashed in run.json), and, when they were made, report/ (one PDF, one Word file) and accuracy.json (only if a key
exists for its pages: none of the twelve companies' pages is keyed in this repo, so there is normally none).
"""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture.reader import Reply
from checks import coverage
from report.build import build as build_report
from runs import sources, twelve
from runs.calendar import calendar_entries
from runs.state import current_state
from server import jobs
from server.app import create_app
from tests.conftest import REPO

RUNS = REPO / "runs"
RUN_SCHEMA = Draft202012Validator(json.loads((RUNS / "schema" / "run.schema.json").read_text()))
TWELVE = {"IONS", "PTGX", "ABVX", "ROIV", "ZLAB", "CLDX", "VKTX", "TSHA", "MNPR", "ENTA", "RYTM", "RVMD"}
TWELVE_LAYOUT = {"files": {"run.json", "decisions.jsonl", "job.json"}, "optional": {"accuracy.json"},
                 "folders": {"pages", "report"}}
RECORDED = RUNS / "twelve-1"


def _index_path() -> Path | None:
    named = os.environ.get("TWELVE_INDEX")
    for p in ([Path(named)] if named else []) + [RUNS / "sources_index.twelve.json", RUNS / "sources_index.json"]:
        if p.is_file():
            return p
    return None


INDEX = _index_path()
needs_index = pytest.mark.skipif(
    INDEX is None, reason="the twelve-company index is not here: runs/sources_index.twelve.json arrives with the capture PR "
                          "(#41); set TWELVE_INDEX=<file> to try another")
needs_run = pytest.mark.skipif(not (RECORDED / "run.json").is_file(),
                               reason="runs/twelve-1/ is written by the live run (Job 9 item 4), after the capture PR lands")


def _sha(f: Path) -> str:
    return hashlib.sha256(f.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def index():
    idx = sources.load_index(INDEX)
    missing = [p["page_path"] for t in idx.tickers for pg in idx.pages(t) for p in [pg["record"]]
               if not Path(p["page_path"]).is_file() or not Path(p["words_path"]).is_file()]
    if missing:
        pytest.skip(f"{len(missing)} indexed page files are not under ~/proof-reader-docs/filings (first: {missing[0]})")
    return idx


# ---------------------------------------------------------------- the real index


@needs_index
def test_the_index_lists_the_twelve_companies_and_the_ones_with_no_page_are_recorded(index):
    assert set(index.tickers) | set(index.empty) == TWELVE
    assert not set(index.tickers) & set(index.empty)
    assert len(index.tickers) >= 1


@needs_index
def test_every_indexed_page_is_there_and_matches_the_hash_the_index_recorded(index):
    n = 0
    for t in index.tickers:
        for pg in index.pages(t):
            rec = pg["record"]
            assert _sha(Path(rec["page_path"])) == rec["page_sha256"], (t, rec["id"], "page")
            assert _sha(Path(rec["words_path"])) == rec["words_sha256"], (t, rec["id"], "words")
            assert rec["ticker"] == t and rec["kind"] in sources.KINDS and rec["page_number"] >= 1
            n += 1
    assert n >= 1


@needs_index
def test_ids_source_ids_and_file_stems_are_unique_across_the_whole_index(index):
    recs = [pg["record"] for t in index.tickers for pg in index.pages(t)]
    for what in ("id", "source_id"):
        assert len({r[what] for r in recs}) == len(recs), what
    assert len({r["file_stem"].lower() for r in recs}) == len(recs), "no two pages share a captured file name"


@needs_index
def test_all_means_every_company_that_has_a_page_and_names_nobody_else(index):
    got = jobs.validate_choices({"companies": ["all"], "event_types": ["milestone"], "time_window_months": 6,
                                 "spending_limit_usd": 5.0, "note_for_agent": None}, index)["companies"]
    assert got == index.tickers and not set(got) & set(index.empty)
    assert jobs.label_for(1, {"companies": got, "time_window_months": 6}) == f"{len(got)} companies · 6 months"
    for t in index.empty:
        with pytest.raises(ValueError, match=f"The captured-pages file lists {t} but holds no page for it yet"):
            jobs.validate_choices({"companies": [t], "event_types": [], "time_window_months": None,
                                   "spending_limit_usd": None, "note_for_agent": None}, index)


def reads_the_page(prompt, n):
    """A fake first reader that reads the page in the prompt: each sentence naming a date after the filing date becomes an
    event whose quote is that sentence. No model; the page's own words are all it uses."""
    words = [dict(zip(("id", "text", "x0", "y0", "x1", "y1"), line.split("|")))
             for line in prompt.split("Words:\n", 1)[1].splitlines() if line.count("|") == 5]
    for w in words:
        for k in ("x0", "y0", "x1", "y1"):
            w[k] = float(w[k])
    head = re.search(r"^Read (.+?) \((\w+)\), [^\n]+? exhibit ", prompt, re.MULTILINE)
    filed = re.search(r"filed\s+(\d{4})-(\d{2})-(\d{2})", prompt)
    from datetime import date
    today = date(int(filed[1]), int(filed[2]), int(filed[3]))
    events = [{"ticker": head[2], "company": head[1], "program": None, "indication": None, "event_type": "milestone",
               "title": s["text"][:60], "date_text": s["date_text"], "date": s["date"], "date_end": s["date_end"],
               "quote": s["text"], "word_ids": s["word_ids"], "box": s["box"]}
              for s in coverage.dated_sentences(words, today)]
    import json as _json
    return _json.dumps({"events": events})


class Reader:
    from tests.test_run_controls import FakeReader as _Base

    def __new__(cls):
        return cls._Base(answer=reads_the_page)


def dry_run(tmp_path, index, companies):
    runs = tmp_path / "runs"
    runs.mkdir()
    code = twelve.main(["--companies", companies, "--window", "6", "--runs-dir", str(runs), "--index", str(INDEX),
                        "--id", "twelve-dry", "--turns", "10000", "--no-second", "--no-wonder", "--no-report"],
                       readers=(Reader(), None, None, None))
    return code, runs


@needs_index
def test_a_few_companies_run_end_to_end_on_their_real_pages_with_a_fake_reader(tmp_path, index, capsys):
    pick = [t for t in ("MNPR", "TSHA", "CLDX") if t in index.tickers] or index.tickers[:2]
    code, runs = dry_run(tmp_path, index, ",".join(pick))
    out = capsys.readouterr().out
    run = json.loads((runs / "twelve-dry" / "run.json").read_text())
    RUN_SCHEMA.validate(run)
    pages = [pg for t in pick for pg in index.pages(t)]
    assert code == 0 and run["run"]["status"] == "done", run["run"]["status_reason"]
    assert [s["id"] for s in run["sources"]] == [pg["source_id"] for pg in pages], "every indexed page, in index order"
    assert run["run"]["sample"] is False and run["run"]["label"] == f"{len(pick)} companies · 6 months" or len(pick) == 1
    c = TestClient(create_app(runs_dir=runs, ui_dir=runs / "no-ui", reader=Reader(), second_reader=None,
                              wonder_reader=None, sam_reader=None))
    assert c.get("/api/runs/twelve-dry").status_code == 200, "every captured page file matches its hash in run.json"
    assert run["run"]["turns_total"] == len(pages), "one first read per page and nothing else was called"
    # GROUP-AXIS-001: the denominator is explicit human acceptance.
    assert "pages read:" in out and "cost per accepted row" in out


@needs_index
@pytest.mark.skipif(not os.environ.get("TWELVE_DRY_ALL"), reason="set TWELVE_DRY_ALL=1 to capture and read every indexed page")
def test_every_indexed_page_captures_and_reads_offline(tmp_path, index, capsys):
    code, runs = dry_run(tmp_path, index, "all")
    run = json.loads((runs / "twelve-dry" / "run.json").read_text())
    RUN_SCHEMA.validate(run)
    total = sum(len(index.pages(t)) for t in index.tickers)
    assert code == 0 and run["run"]["status"] == "done" and len(run["sources"]) == total
    assert set(run["run"]["choices"]["companies"]) == set(index.tickers)


# ---------------------------------------------------------------- the recorded run


@needs_run
def test_the_recorded_run_has_the_layout_and_nothing_else():
    names = {p.name for p in RECORDED.iterdir() if p.is_file()}
    folders = {p.name for p in RECORDED.iterdir() if p.is_dir()}
    assert TWELVE_LAYOUT["files"] <= names and names <= TWELVE_LAYOUT["files"] | TWELVE_LAYOUT["optional"], names
    assert "pages" in folders and folders <= TWELVE_LAYOUT["folders"], folders
    run = json.loads((RECORDED / "run.json").read_text())
    for src in run["sources"]:
        for page in src["pages"]:
            assert (RECORDED / page["image"]).is_file() and (RECORDED / page["words"]).is_file()
    on_disk = {p.name for p in (RECORDED / "pages").iterdir()}
    named = {Path(pg[k]).name for s in run["sources"] for pg in s["pages"] for k in ("image", "words")}
    assert on_disk == named, "pages/ holds the pages run.json names and no others"


@needs_run
def test_the_recorded_run_is_a_real_run_named_twelve_1_and_says_how_it_ended():
    from runs.check_load import load_bound_run
    run = load_bound_run(RECORDED)
    RUN_SCHEMA.validate(run)
    m = run["run"]
    assert m["id"] == "twelve-1" and m["sample"] is False and "fake" not in m["label"].lower()
    assert m["choices"]["spending_limit_usd"] == 5.0 and m["choices"]["time_window_months"] == 6
    assert m["status"] in ("done", "needs_input"), "it ended, one way or the other"
    if m["status"] == "done":
        assert m["finished_at"]
    else:        # it ran into its spending limit: the call that crossed it stopped the run
        assert m["finished_at"] is None and m["status_reason"].startswith("Stopped at the spending limit")
        assert m["spend_usd"] > m["choices"]["spending_limit_usd"]


@needs_run
def test_the_recorded_runs_numbers_are_the_ones_written_in_the_record():
    """What the live run of 2026-10-29 04:46-05:47 UTC made, pinned: a change here means the record changed."""
    from runs.check_load import load_bound_run
    saved = RECORDED / "run.json"
    before = saved.read_bytes()
    recorded = json.loads(before)
    run = load_bound_run(RECORDED)
    m = run["run"]
    assert (len(run["sources"]), len(run["rows"]), m["turns_total"], m["resumes"]) == (105, 95, 245, 6)
    assert m["spend_usd"] == pytest.approx(5.055042) and m["label"] == "11 companies · 6 months"
    assert m["status"] == "needs_input" and "($5.06 of $5.00 used)" in m["status_reason"]
    assert "ABVX" not in m["choices"]["companies"] and len(m["choices"]["companies"]) == 11
    assert not m.get("skipped_pages"), "no page broke the run"
    assert m["second_reader"] == {"model": "Muse Spark Open", "status": "read", "reason": None, "input": "image"}
    assert (m["escalation"]["rows_sent"], m["escalation"]["rows_replaced"]) == (35, 3)
    state = current_state(run, [])
    # Saved outcomes stay unchanged; the current literal checker is a separate view.
    assert sum(r["group"] in ("kept", "corrected") for r in recorded["rows"]) == 33
    assert state["counts"]["rows"] == {"kept": 21, "corrected": 2, "removed": 31, "unverified": 14, "open": 27}
    assert (state["unresolved"], state["mistakes"], state["excluded"]) == (
        64, {"code": 3, "second_reader": 0, "person": 0}, {"code": 35, "person": 0})
    assert state["counts"]["review"] == {"accepted": 0, "proposed": 64, "excluded": 31}
    assert state["cost_per_kept_row_usd"] is None  # no current human acceptance
    assert saved.read_bytes() == before
    read = {}
    for src in run["sources"]:
        read[src["id"].split("-")[1].upper()] = read.get(src["id"].split("-")[1].upper(), 0) + 1
    assert read == {"IONS": 19, "PTGX": 55, "VKTX": 28, "RVMD": 3}, "the limit stopped it inside RVMD"


@needs_run
@needs_index
def test_the_recorded_run_reads_only_pages_the_index_lists(index):
    run = json.loads((RECORDED / "run.json").read_text())
    indexed = {pg["source_id"]: pg["record"] for t in index.tickers for pg in index.pages(t)}
    assert run["sources"], "it read something"
    for src in run["sources"]:
        rec = indexed[src["id"]]
        assert src["published_date"] == rec["filing_date"] and src["pages"][0]["page_number"] == rec["page_number"]
    assert set(run["run"]["choices"]["companies"]) <= set(index.tickers)
    assert not set(run["run"]["choices"]["companies"]) & set(index.empty)


@needs_run
def test_the_decisions_server_serves_the_recorded_run_with_every_page_hash_holding_and_its_calendar_is_the_state_rule():
    c = TestClient(create_app(runs_dir=RUNS, ui_dir=RUNS / "no-ui"))
    body = c.get("/api/runs/twelve-1")
    assert body.status_code == 200, body.text
    from runs.check_load import load_bound_run
    run = load_bound_run(RECORDED)
    f = RECORDED / "decisions.jsonl"
    decisions = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    from runs.check_load import bind_decisions_on_load
    decisions = bind_decisions_on_load(run, decisions, RECORDED)
    state = current_state(run, decisions)
    assert body.json()["state"]["counts"] == state["counts"]
    cal = c.get("/api/runs/twelve-1/calendar").json()["entries"]
    assert [e["id"] for e in cal] == [e["id"] for e in calendar_entries(run, state)]
    assert all(e["group"] in ("kept", "corrected") for e in cal)
    # GROUP-AXIS-001: preserve the recorded mechanical-group calculation,
    # but do not present it as the current cost per human-accepted result.
    recorded = json.loads((RECORDED / "run.json").read_text())
    recorded_kept = sum(r["group"] in ("kept", "corrected") for r in recorded["rows"])
    assert recorded_kept == 33  # 30 kept + 3 corrected in the unchanged saved JSON.
    assert recorded["run"]["spend_usd"] == pytest.approx(5.055042)
    legacy_denominator = state["counts"]["rows"]["kept"] + state["counts"]["rows"]["corrected"]
    assert legacy_denominator == 23  # refreshed mechanical view, not accepted results
    # Earlier checker snapshot was 21 and 0.240716; do not use either as today's
    # human-accepted cost metric. The raw saved count of 33 remains pinned above.
    assert recorded["run"]["spend_usd"] / legacy_denominator == pytest.approx(0.219784, abs=0.000001)
    accepted = sum(t["accepted"] for t in state["targets"].values() if t["kind"] == "row")
    assert state["counts"]["review"]["accepted"] == accepted
    spend = run["run"]["spend_usd"]
    expect = None if spend is None or accepted <= 0 else round(spend / accepted, 6)
    assert state["cost_per_kept_row_usd"] == expect


@needs_run
def test_the_recorded_runs_report_and_accuracy_file_are_the_ones_the_code_made():
    from accuracy import export
    if (RECORDED / "report").is_dir():
        assert len(list((RECORDED / "report").glob("*.pdf"))) == 1 and len(list((RECORDED / "report").glob("*.docx"))) == 1
    data, problem = export.read_if_intact(RECORDED)
    assert problem is None, problem          # no accuracy.json is fine; one that does not hold its hashes is not


@needs_run
def test_the_recorded_runs_job_log_ends_where_the_run_does_and_holds_every_stop():
    job = json.loads((RECORDED / "job.json").read_text())
    run = json.loads((RECORDED / "run.json").read_text())
    last = job["log"][-1]
    assert last["status"] == run["run"]["status"] and last["spend_usd"] == run["run"]["spend_usd"]
    stops = [l["status"] for l in job["log"] if l["status"] in ("paused_turn_limit", "needs_input", "failed", "done")
             and l.get("reason")]
    assert stops == ["paused_turn_limit"] * 6 + ["needs_input"], "six turn-limit pauses, resumed; then the spending limit"
    assert not job.get("page_failures") and not job.get("skipped") and not job.get("unpriced_calls")
    assert job["openrouter_spend_usd"] == 0.0, "the fallback route was never used"


def test_the_layout_is_written_down_where_the_next_person_will_look():
    text = (REPO / "runs" / "README.md").read_text()
    section = text.split("### The recorded twelve-company run", 1)[1].split("\n### ", 1)[0]
    for name in ("run.json", "decisions.jsonl", "job.json", "pages/", "report/", "accuracy.json", "--id twelve-1"):
        assert name in section, name
