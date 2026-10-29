"""Job 4 (R4c): the event-type check, dated-sentence coverage, program + indication on rows.

Gap 3 and the "no model judging a model" line: both checks are plain code. The coverage scan shows a person what the
reader skipped; the type check says when a row's kind of event does not fit its own quote.
"""

import copy
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from accuracy import types as T
from accuracy.mistake_log import log_from_score
from accuracy.score import score_events
from capture import read_events
from checks import coverage
from checks.common import COULD_NOT_RUN, FAIL, NOT_NEEDED, PASS
from checks.event_checks import quote_match, row_status, type_match
from runs import build_entry, recheck
from runs.calendar import calendar_entries
from runs.coverage import TITLE, possible_event_rows
from runs.state import current_state
from server import jobs
from server.app import create_app
from tests.conftest import FIXTURES, REPO
from tests.test_run_controls import FakeReader

RUNS = REPO / "runs"
RUN_SCHEMA = Draft202012Validator(json.loads((RUNS / "schema" / "run.schema.json").read_text()))
PIN = "ptgx_8k_2026-08-05_ex991_p3"
TODAY = date(2026, 8, 5)       # the day the page was filed


@pytest.fixture(scope="module")
def ptgx_words_doc():
    return json.loads((FIXTURES / f"{PIN}.words.json").read_text())


@pytest.fixture(scope="module")
def ptgx_run():
    return json.loads((RUNS / "ptgx-r4a" / "run.json").read_text())


@pytest.fixture(scope="module")
def reading():
    return json.loads((FIXTURES / f"{PIN}.events.json").read_text())


def w(i, text, x=0.0, y=0.0):
    return {"id": f"w{i}", "text": text, "x0": x, "y0": y, "x1": x + 10, "y1": y + 10}


def page(text):
    return [w(i, t, x=i * 12.0) for i, t in enumerate(text.split())]


# ---------------------------------------------------------------- Event type matches the quote


def test_a_quote_with_the_types_own_words_passes():
    r = type_match("milestone", "Phase 1 initiation is anticipated in Q1 2027.")
    assert (r.result, r.check, r.label) == (PASS, "type_match", "Event type matches the quote")
    assert r.reason == "The quote's words (initiation) fit milestone."


@pytest.mark.parametrize("event_type,quote,words,says", [
    ("milestone", "Topline data are expected in Q3 2027.", "data, topline", "trial results"),
    ("trial_results", "A PDUFA action date is set for March 12, 2027.", "pdufa, action date", "regulatory decision"),
    ("regulatory_decision", "Phase 2 enrollment completed in Q4 2026.", "enrolled", "milestone"),
    ("launch", "The company will present at the ASH conference in December 2026.", "present, conference", "conference"),
])
def test_a_quote_whose_words_point_to_another_kind_of_event_fails_with_a_plain_reason(event_type, quote, words, says):
    r = type_match(event_type, quote)
    group = event_type.replace("_", " ")
    assert r.result == FAIL
    assert r.reason.startswith("The quote's words (") and f"point to {says}, not {group}." in r.reason
    assert r.compared["value"] == event_type and says in r.compared["found"]


def test_a_type_is_not_failed_for_having_another_types_words_as_well():
    assert type_match("milestone", "Phase 1 initiation is expected; data follow in 2028.").result == PASS


def test_nothing_to_compare_is_not_a_tick_and_does_not_hold_a_row_back():
    none = type_match("milestone", "The company expects growth in 2027.")
    assert none.result == NOT_NEEDED and "nothing to compare" in none.reason
    assert type_match("earnings", "Net loss for the quarter ended June 30.").result == NOT_NEEDED
    assert type_match("unknown", "Anything at all.").result == NOT_NEEDED
    assert type_match("milestone", None).result == COULD_NOT_RUN
    assert type_match("milestone", "  ").result == COULD_NOT_RUN


def test_words_are_whole_words():
    assert type_match("trial_results", "The datacenter opens in 2027.").result == NOT_NEEDED
    assert type_match("filing", "We will submit an NDA.").result == PASS


def test_the_real_ptgx_rows_pass_the_type_check(ptgx_run):
    for row in ptgx_run["rows"]:
        check = next(c for c in row["checks"] if c["name"] == "type_match")
        assert check["result"] == "pass" and check["code_ref"] == {"file": "checks/event_checks.py",
                                                                    "function": "type_match"}
        assert row["status"] == "matched"


def test_a_fix_to_the_wrong_kind_of_event_fails_its_recheck(ptgx_run, ptgx_words_doc):
    row = ptgx_run["rows"][0]
    source = ptgx_run["sources"][0]
    line = recheck.recheck_row("2026-10-29T00:00:00+00:00", {**row, "event_type": "trial_results"}, source,
                               ptgx_words_doc, field="event_type", before=row)
    rc = line["recheck"]
    assert rc["result"] == "pass" or rc["result"] == "fail"
    failed = [c for c in rc["checks"] if c["result"] == "fail"]
    assert [c["name"] for c in failed] == ([] if rc["result"] == "pass" else ["type_match"])
    # the quote holds 'initiation', a milestone word and no results word: the new type does not fit it
    assert rc["result"] == "fail" and "not trial results" in failed[0]["reason"]
    assert rc["before"]["result"] == "pass"


# ---------------------------------------------------------------- Dated-sentence coverage


def test_sentences_are_cut_at_sentence_ends_not_abbreviations_or_bullets():
    got = coverage.sentences(page("Acme Inc. reports. · First item in Q3 2027. · Second item. Dr. Smith spoke."))
    assert [s["text"] for s in got] == ["Acme Inc. reports.", "First item in Q3 2027.", "Second item.",
                                        "Dr. Smith spoke."]
    assert got[1]["word_ids"][0] == "w4" and len(got[1]["box"]) == 4


def test_a_run_of_words_with_no_sentence_end_is_not_a_sentence():
    table = page(" ".join(["12,345"] * (coverage.MAX_WORDS + 5)) + " end.")
    assert coverage.sentences(table) == []


@pytest.mark.parametrize("text,date_text,start", [
    ("Results are expected on March 12, 2027.", "March 12, 2027", "2027-03-12"),
    ("Data are expected in Q3 2027.", "Q3 2027", "2027-07-01"),
    ("Phase 1 starts in the second half of 2027.", "second half of 2027", "2027-07-01"),
    ("We expect to launch in mid-2027.", "mid-2027", "2027-05-01"),
    ("Filing planned for March 2027.", "March 2027", "2027-03-01"),
    ("Approval is anticipated in 2028.", "2028", "2028-01-01"),
    ("A PDUFA date of 2027-02-01 was set.", "2027-02-01", "2027-02-01"),
])
def test_dated_sentences_reads_dates_and_periods_with_the_checks_own_parsers(text, date_text, start):
    [s] = coverage.dated_sentences(page(text), TODAY)
    assert (s["date_text"], s["date"]) == (date_text, start)


def test_only_dates_after_today_count():
    assert coverage.dated_sentences(page("Revenue for the quarter ended June 30, 2026 was up."), TODAY) == []
    assert coverage.dated_sentences(page("Data came in Q1 2026."), TODAY) == []
    assert coverage.dated_sentences(page("Nothing is dated here."), TODAY) == []
    assert len(coverage.dated_sentences(page("Data came in Q1 2026 and are due in Q2 2027."), TODAY)) == 1
    assert coverage.dated_sentences(page("Data are due on August 5, 2026."), TODAY) == [], "today itself is not after today"


def test_the_ptgx_page_names_two_future_events(ptgx_words_doc):
    found = coverage.dated_sentences(ptgx_words_doc["words"], TODAY)
    assert [s["date_text"] for s in found] == ["second half of 2027", "Q1 2027"]
    assert [s["text"].split()[0] for s in found] == ["PN-458o", "PN-8047"]
    for s in found:
        assert quote_match(s["text"], ptgx_words_doc["words"]).result == PASS


def test_the_rows_the_reader_proposed_cover_their_sentences(ptgx_words_doc, ptgx_run):
    found = coverage.dated_sentences(ptgx_words_doc["words"], TODAY)
    quotes = [r["quote"] for r in ptgx_run["rows"]]
    assert coverage.uncovered(found, quotes) == []
    assert len(coverage.uncovered(found, [])) == 2
    assert [s["text"].split()[0] for s in coverage.uncovered(found, quotes[:1])] == [
        "PN-8047" if "PN-458o" in quotes[0] else "PN-458o"]


def test_a_quote_that_holds_almost_all_of_the_sentence_covers_it():
    sentence = "PN-8047 Phase 1 initiation is anticipated in Q1 2027 following IND clearance by regulators."
    assert coverage.sentence_covered(sentence, ["PN-8047 Phase 1 initiation is anticipated in Q1 2027 following IND"
                                                " clearance"])
    assert not coverage.sentence_covered(sentence, ["Something unrelated about PN-8047."])
    assert coverage.sentence_covered("Phase 1 in Q1 2027.", ["PN-8047: Phase 1 in Q1 2027. More text"])


# ---------------------------------------------------------------- the open rows


def one_event_reading(reading, keep=0):
    r = copy.deepcopy(reading)
    r["events"] = [r["events"][keep]]
    return r


def test_a_skipped_dated_sentence_becomes_an_open_row_in_the_run(reading, tmp_path):
    run = build_entry.build_entry_run(one_event_reading(reading), tmp_path / "x", finished_at="2026-10-29T00:00:00+00:00")
    RUN_SCHEMA.validate(run)
    assert [r.get("found_by") for r in run["rows"]] == [None, "coverage"]
    row = run["rows"][1]
    assert row["title"].startswith("Possible event not read: ") and row["title"].endswith(row["quote"])
    assert (row["status"], row["group"], row["checks"], row["event_type"]) == ("undecided", "open", [], "unknown")
    assert row["date_text"] in row["quote"] and row["date"] and row["box"] and len(row["box"]) == 4
    assert "The reader did not turn this sentence into a row" in row["reason"]
    assert row["id"] == "e2" and row["page_number"] == 3 and row["sample"] is False
    first, found = row["trail"]
    assert (first["step"], first["tag"], first["who"]) == ("Instructions sent", "Code", "checks.coverage")
    assert first["text"].startswith("No model was asked.") and found["step"] == "Found"
    assert not any(t["tag"] == "Model" for t in row["trail"])
    words = json.loads((FIXTURES / f"{PIN}.words.json").read_text())["words"]
    assert quote_match(row["quote"], words).result == PASS


def test_the_open_row_is_unresolved_never_on_the_calendar_and_a_person_can_decide_it(reading, tmp_path):
    run = build_entry.build_entry_run(one_event_reading(reading), tmp_path / "x", finished_at="2026-10-29T00:00:00+00:00")
    state = current_state(run, [])
    cov = run["rows"][1]["id"]
    assert state["targets"][cov]["group"] == "open" and state["unresolved"] == 1
    assert cov not in {e["id"] for e in calendar_entries(run, state)}
    keep = {"at": "2026-10-29T00:00:00+00:00", "target": cov, "action": "keep", "note": None, "fix": None,
            "by": "person"}
    after = current_state(run, [keep])
    assert after["targets"][cov]["group"] == "kept" and after["targets"][cov]["decided_by_person"] is True
    assert cov in {e["id"] for e in calendar_entries(run, after)}


def test_a_complete_reading_adds_no_open_rows(ptgx_run):
    assert [r.get("found_by") for r in ptgx_run["rows"]] == [None, None]


def test_the_committed_run_is_what_the_builder_makes(reading, ptgx_run, tmp_path):
    fresh = build_entry.build_entry_run(reading, tmp_path / "ptgx-r4a", finished_at=ptgx_run["run"]["finished_at"])
    assert fresh == ptgx_run


def test_the_open_row_carries_the_page_number_of_its_source_and_the_run_company(ptgx_words_doc, ptgx_run):
    rows = possible_event_rows(ptgx_words_doc, [], ptgx_run["sources"][0], first_index=7,
                               at="2026-10-29T00:00:00+00:00", today=TODAY, ticker="PTGX", company="Protagonist")
    assert [r["id"] for r in rows] == ["e7", "e8"] and {r["ticker"] for r in rows} == {"PTGX"}
    assert all(r["title"].startswith(TITLE) for r in rows)


def test_a_run_started_from_the_new_run_panel_shows_what_the_reader_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "today", lambda: date(2026, 10, 29))
    events = json.loads(FakeReader.default("Protagonist"))["events"]

    def one(prompt, n):
        return json.dumps({"events": events[:1]})

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(answer=one),
                                   background=False))
    meta = client.post("/api/runs/new", json={"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES),
                                              "time_window_months": None, "spending_limit_usd": None,
                                              "note_for_agent": None}).json()
    run = json.loads((runs_dir / meta["id"] / "run.json").read_text())
    RUN_SCHEMA.validate(run)
    assert [r.get("found_by") for r in run["rows"]] == [None, "coverage"]
    full = client.post("/api/runs/new", json={"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES),
                                              "time_window_months": None, "spending_limit_usd": None,
                                              "note_for_agent": None}).json()
    assert full["status"] in ("queued", "done")


# ---------------------------------------------------------------- scoring: the surfaced miss, aliases, indication


KEY = {"key_version": 1, "sealed": False, "complete": True, "source_sha256": "0" * 64, "page_number": 3, "cells": [],
       "events": [{"ticker": "PTGX", "program": "PN-8047", "event_type": "milestone", "date_text": "Q1 2027",
                   "quote": "PN-8047 (oral small molecule hepcidin functional mimetic): IND-enabling studies are "
                            "ongoing, with Phase 1 initiation anticipated in Q1 2027.",
                   "aliases": ["hepcidin mimetic"]},
                  {"ticker": "PTGX", "program": "PN-458o", "event_type": "milestone",
                   "date_text": "second half of 2027",
                   "quote": "PN-458o (dual GLP/GIP agonist peptide): IND-enabling studies are ongoing with Phase 1 "
                            "initiation anticipated in the second half of 2027."}]}


def _reader_row(i, **kw):
    base = {"id": f"e{i}", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
            "date_text": "Q1 2027", "status": "matched", "checks": [], "proposed_by": "Muse Spark", "page_number": 3}
    return {**base, **kw}


def _coverage_row(i, quote):
    return {"id": f"e{i}", "ticker": "PTGX", "program": None, "event_type": "unknown", "date_text": "second half of 2027",
            "status": "undecided", "checks": [], "found_by": "coverage", "proposed_by": "Dated-sentence coverage (code)",
            "page_number": 3, "quote": quote, "title": TITLE + quote}


def test_a_missed_event_the_coverage_check_surfaced_is_typed_and_caught_by_code():
    rows = [_reader_row(1), _coverage_row(2, KEY["events"][1]["quote"])]
    sc = score_events(rows, KEY)
    assert sc["counts"]["found"] == 1 and sc["counts"]["missed"] == 1 and sc["counts"]["invented"] == 0
    [miss] = sc["missed"]
    assert (miss["type"], miss["caught"], miss["surfaced_as"]) == (T.MISSED_DATED_SENTENCE, True, "e2")
    assert sc["model"] == "Muse Spark", "the coverage check is not a model"
    logged = log_from_score(sc)
    assert [(e["type"], e["caught_by"]) for e in logged] == [("MISSED_DATED_SENTENCE", "code")]


def test_a_missed_event_nothing_surfaced_stays_untyped_and_not_caught():
    sc = score_events([_reader_row(1)], KEY)
    [miss] = sc["missed"]
    assert (miss["type"], miss["caught"], miss["surfaced_as"]) == (None, False, None)


def test_open_rows_are_never_scored_as_invented_events():
    sc = score_events([_reader_row(1), _reader_row(2, program="PN-458o", date_text="second half of 2027"),
                       _coverage_row(3, "Some other dated sentence in Q3 2028.")], KEY)
    assert sc["counts"]["found"] == 2 and sc["counts"]["invented"] == 0 and sc["counts"]["unscored"] == 0


def test_the_indication_is_matched_through_the_keys_aliases():
    """The reader names the drug where the key names the indication, or the reverse: either aliases."""
    rows = [_reader_row(1, program="Rilzabrutinib", indication="Hepcidin mimetic")]
    assert score_events(rows, KEY)["counts"]["found"] == 1
    rows = [_reader_row(1, program="hepcidin mimetic", indication=None)]
    assert score_events(rows, KEY)["counts"]["found"] == 1
    rows = [_reader_row(1, program="Something else", indication="Not an alias")]
    assert score_events(rows, KEY)["counts"]["invented"] == 1


def test_the_reader_is_asked_for_the_program_and_the_indication(ptgx_words_doc):
    prompt = read_events.build_prompt(ptgx_words_doc, build_entry.load_entry(PIN))
    assert '"indication": "<the disease or condition the program is for, as printed, or null>"' in prompt
    assert '"program" is the drug or program name exactly as printed; "indication" is the disease or condition' in prompt


def test_a_row_carries_program_and_indication(reading, ptgx_words_doc, ptgx_run):
    event = {**reading["events"][0], "indication": "Polycythemia vera"}
    row = build_entry.build_row(1, event, reading, ptgx_run["sources"][0], ptgx_words_doc, "2026-10-29T00:00:00+00:00")
    RUN_SCHEMA.validate({**ptgx_run, "rows": [row]})
    assert (row["program"], row["indication"]) == (reading["events"][0]["program"], "Polycythemia vera")
    assert build_entry.build_row(1, reading["events"][0], reading, ptgx_run["sources"][0], ptgx_words_doc,
                                 "2026-10-29T00:00:00+00:00")["indication"] is None


def test_the_new_mistake_type_has_plain_words():
    assert T.MISSED_DATED_SENTENCE in T.CODES and T.label(T.MISSED_DATED_SENTENCE) == (
        "A dated sentence the reader did not turn into a row")
