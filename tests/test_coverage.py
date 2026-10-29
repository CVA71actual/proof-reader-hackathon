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
from tests.conftest import FIXTURES, REPO, without_trails
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
    assert type_match("conference", "The company will present; data follow in 2028.").result == FAIL      # data comes first in TYPE_PRIORITY
    assert type_match("milestone", "Phase 1 initiation is expected; data follow in 2028.").result == NOT_NEEDED


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
    assert without_trails(fresh) == without_trails(ptgx_run)


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


# ---------------------------------------------------------------- Job 6: repeated tokens (Codex P2), published date, quote boxes, type tie


def test_a_short_quote_does_not_cover_a_sentence_that_repeats_its_words():
    """Codex's reproduction: one 'Q3 2027 is planned and' is 5 of the 14 tokens, not a cover."""
    sentence = "Q3 2027 is planned and Q3 2027 is planned and Q3 2027 is planned."
    assert coverage.sentence_covered(sentence, ["Q3 2027 is planned and"]) is False
    assert coverage.sentence_covered(sentence, [sentence]) is True
    assert coverage.sentence_covered(sentence, ["Q3 2027 is planned and Q3 2027 is planned and Q3 2027 is planned"])
    assert coverage.sentence_covered(sentence, ["Q3 2027 is planned and Q3 2027 is planned and Q3 2027"]) is True
    assert coverage.sentence_covered(sentence, ["x Q3 2027 is planned and Q3 2027 is planned and"]) is False, \
        "10 of 14 tokens is under 80%"


def test_a_quote_that_is_only_a_piece_of_a_longer_sentence_leaves_it_uncovered():
    sentence = "The Company expects results from the study in Q3 2027 and will report them at a medical meeting."
    assert coverage.sentence_covered(sentence, ["results from the study in Q3 2027"]) is False
    assert coverage.sentence_covered(sentence, [sentence + " More."]) is True


def test_the_repeated_token_sentence_raises_a_not_read_row(ptgx_run, ptgx_words_doc):
    words = page("Q3 2027 is planned and Q3 2027 is planned and Q3 2027 is planned.")
    doc = {"words": words, "page_width": 612.0, "page_height": 792.0}
    short = [{"quote": "Q3 2027 is planned and", "status": "matched"}]
    rows = possible_event_rows(doc, short, ptgx_run["sources"][0], first_index=1, at="2026-10-29T00:00:00+00:00",
                               today=TODAY, ticker="PTGX", company="Protagonist")
    assert len(rows) == 1 and rows[0]["quote"].startswith("Q3 2027 is planned and Q3 2027")


def test_coverage_counts_dates_after_the_sources_published_date_not_the_run_day(reading, tmp_path):
    """The PTGX filing is dated 2026-08-05: its Q1 2027 and 2H 2027 are upcoming whatever day the run is made; a run
    passed an explicit later date sees them as past."""
    one = one_event_reading(reading)
    run = build_entry.build_entry_run(one, tmp_path / "x", finished_at="2030-01-01T00:00:00+00:00")
    assert [r.get("found_by") for r in run["rows"]] == [None, "coverage"]
    assert "after 2026-08-05" in run["rows"][1]["trail"][0]["text"]
    from datetime import date as d
    late = build_entry.build_entry_run(one, tmp_path / "y", finished_at="2030-01-01T00:00:00+00:00",
                                       as_of=d(2028, 1, 1))
    assert [r.get("found_by") for r in late["rows"]] == [None], "both periods are before 2028-01-01"


def test_a_job_uses_the_published_date_or_the_date_it_was_given(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "today", lambda: date(2031, 1, 1))     # the run day: far after every date on the page
    events = json.loads(FakeReader.default("Protagonist"))["events"]
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    def make(as_of=None):
        client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", second_reader=None,
                                       reader=FakeReader(answer=lambda p, n: json.dumps({"events": events[:1]})),
                                       background=False, coverage_as_of=as_of))
        rid = client.post("/api/runs/new", json={"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES),
                                                  "time_window_months": None, "spending_limit_usd": None,
                                                  "note_for_agent": None}).json()["id"]
        return [r.get("found_by") for r in json.loads((runs_dir / rid / "run.json").read_text())["rows"]]

    assert make() == [None, "coverage"], "the run day (2031) is not the day that counts"
    assert make(date(2028, 1, 1)) == [None]


def test_the_ptgx_rows_still_pass_their_box_check_by_the_quote_rule(ptgx_run):
    for row in ptgx_run["rows"]:
        check = next(c for c in row["checks"] if c["name"] == "selector_match")
        assert check["result"] == "pass" and check["reason"].startswith("Every word of the quote is inside the box")


def _box(words, pad=0.5):
    return [min(w["x0"] for w in words) - pad, min(w["y0"] for w in words) - pad,
            max(w["x1"] for w in words) + pad, max(w["y1"] for w in words) + pad]


def _lines(*lines, width=40.0, height=10.0, gap=4.0):
    """Words in lines: each line a list of texts; returns the words with ids and boxes."""
    out, y = [], 100.0
    for li, texts in enumerate(lines):
        x = 50.0
        for wi, t in enumerate(texts):
            out.append({"id": f"l{li}w{wi}", "text": t, "x0": x, "y0": y, "x1": x + width, "y1": y + height})
            x += width + gap
        y += height + 6.0
    return out


def _sel(quote, box, words):
    from checks.selector_match import selector_match
    return selector_match(quote, box, words, 612.0, 792.0, quote_lines=True)


def test_a_quote_box_passes_with_other_words_on_the_quotes_first_and_last_lines():
    ws = _lines(["Earlier", "sentence", "ends.", "Results", "are", "due"], ["in", "Q3", "2027.", "Next", "one", "starts"])
    run = [w for w in ws if w["text"] in ("Results", "are", "due", "in", "Q3", "2027.")]
    quote = " ".join(w["text"] for w in run)
    wide = [min(w["x0"] for w in ws), min(w["y0"] for w in run) - 0.5, max(w["x1"] for w in ws), max(w["y1"] for w in run) + 0.5]
    r = _sel(quote, wide, ws)
    assert r.result == PASS and "stays on the quote's lines" in r.reason
    assert _sel(quote, _box(run), ws).result == PASS


def test_a_quote_box_fails_when_it_misses_a_word_or_leaves_the_quotes_lines():
    ws = _lines(["First", "line", "here."], ["Results", "are", "due", "in", "Q3", "2027."], ["Third", "line", "here."])
    run = [w for w in ws if w["id"].startswith("l1")]
    quote = " ".join(w["text"] for w in run)
    assert _sel(quote, _box(run), ws).result == PASS
    short = _sel(quote, _box(run[:-1]), ws)
    assert short.result == FAIL and short.reason.startswith("The box does not hold every word of the quote (missing 2027.")
    above = _box(run)
    above[1] = min(w["y0"] for w in ws if w["id"].startswith("l0")) - 0.5
    assert _sel(quote, above, ws).reason == "The box reaches above the first line of the quote."
    below = _box(run)
    below[3] = max(w["y1"] for w in ws if w["id"].startswith("l2")) + 0.5
    assert _sel(quote, below, ws).reason == "The box reaches below the last line of the quote."


def test_a_two_line_quote_may_end_mid_line_and_start_mid_line():
    ws = _lines(["Before", "the", "Results", "are", "due"], ["in", "Q3", "2027.", "After", "it"])
    run = [w for w in ws if w["text"] in ("Results", "are", "due", "in", "Q3", "2027.")]
    quote = " ".join(w["text"] for w in run)
    full_width = [min(w["x0"] for w in ws), _box(run)[1], max(w["x1"] for w in ws), _box(run)[3]]
    assert _sel(quote, full_width, ws).result == PASS


def test_table_values_keep_the_strict_box_rule():
    from checks.selector_match import selector_match
    ws = _lines(["Net", "loss", "(1,234)", "(2,000)"])
    cell = ws[2]
    assert selector_match("(1,234)", _box([cell]), ws, 612.0, 792.0).result == PASS
    wide = [cell["x0"] - 0.5, cell["y0"] - 0.5, ws[3]["x1"] + 0.5, cell["y1"] + 0.5]
    assert selector_match("(1,234)", wide, ws, 612.0, 792.0).result == FAIL, "a value box that reaches the next cell"
    assert _sel("(1,234)", wide, ws).result == PASS, "the same box for a quote on that line is allowed"


def test_a_quote_not_printed_on_the_page_falls_back_to_the_strict_check():
    ws = _lines(["Some", "words", "here."])
    r = _sel("Words that are not there.", _box(ws), ws)
    assert r.result == FAIL and "Every word of the quote" not in r.reason


def test_a_quote_printed_twice_is_checked_against_the_run_the_box_covers():
    ws = _lines(["Results", "in", "Q3", "2027."], ["Other", "text."], ["Results", "in", "Q3", "2027."])
    second = [w for w in ws if w["id"].startswith("l2")]
    assert _sel("Results in Q3 2027.", _box(second), ws).result == PASS


def test_milestone_and_trial_results_words_together_decide_nothing_and_one_family_decides():
    both = "Phase 1 initiation is expected and topline data follow in 2028."
    assert type_match("milestone", both).result == NOT_NEEDED and type_match("trial_results", both).result == NOT_NEEDED
    assert "both milestone and trial results" in type_match("milestone", both).reason
    only_milestone = "Phase 1 initiation is anticipated in Q1 2027."
    assert type_match("milestone", only_milestone).result == PASS
    assert type_match("trial_results", only_milestone).result == FAIL
    only_results = "Topline results are expected in Q3 2027."
    assert type_match("trial_results", only_results).result == PASS and type_match("milestone", only_results).result == FAIL


def _key(events):
    return {"key_version": 1, "sealed": False, "complete": True, "source_sha256": "0" * 64, "page_number": 3,
            "cells": [], "events": events}


CLDX = [
    {"ticker": "CLDX", "program": "Barzolvolimab", "indication": "CSU", "event_type": "milestone",
     "date_text": "summer 2026", "quote": "q1", "aliases": ["barzolvolimab"], "indication_aliases": ["chronic spontaneous urticaria"]},
    {"ticker": "CLDX", "program": "Barzolvolimab", "indication": "PN", "event_type": "milestone",
     "date_text": "late 2026", "quote": "q2", "aliases": ["barzolvolimab"], "indication_aliases": ["prurigo nodularis"]},
    {"ticker": "CLDX", "program": "Barzolvolimab", "indication": "AD", "event_type": "milestone",
     "date_text": "fourth quarter of 2026", "quote": "q3", "aliases": ["barzolvolimab"],
     "indication_aliases": ["atopic dermatitis"]},
]


def _erow(i, **kw):
    base = {"id": f"e{i}", "ticker": "CLDX", "program": "Barzolvolimab", "indication": None, "event_type": "milestone",
            "date_text": "summer 2026", "status": "matched", "checks": [], "proposed_by": "Muse Spark",
            "page_number": 3, "quote": "x"}
    return {**base, **kw}


def test_one_drug_with_three_indications_is_three_events_matched_by_date_and_indication():
    rows = [_erow(1, date_text="summer 2026"), _erow(2, date_text="late 2026"),
            _erow(3, date_text="fourth quarter of 2026")]
    sc = score_events(rows, _key(CLDX))
    assert sc["counts"]["found"] == 3 and sc["counts"]["invented"] == 0 and sc["counts"]["missed"] == 0
    assert [i["expected"]["indication"] for i in sc["found"]] == ["CSU", "PN", "AD"]


def test_a_row_matches_by_its_indication_when_it_names_no_program_that_the_key_knows():
    rows = [_erow(1, program="Some brand", indication="Prurigo nodularis", date_text="late 2026"),
            _erow(2, program=None, indication="CSU", date_text="summer 2026"),
            _erow(3, program="x", indication="chronic spontaneous urticaria", date_text="fourth quarter of 2026")]
    sc = score_events(rows, _key(CLDX))
    # row 3's indication names the CSU event, which row 2 already matched: a second row for it
    assert [(i["id"], i["expected"]["indication"]) for i in sc["found"]] == [("e1", "PN"), ("e2", "CSU")]
    assert sc["counts"]["invented"] == 1 and sc["invented"][0]["id"] == "e3"
    assert sc["invented"][0]["type"] == T.DUPLICATE
    assert sc["counts"]["missed"] == 1 and sc["missed"][0]["expected"]["indication"] == "AD"


def test_the_period_decides_between_events_of_the_same_drug():
    sc = score_events([_erow(1, date_text="Q4 2026"), _erow(2, date_text="Q4 2026")], _key(CLDX))
    assert sc["counts"]["found"] == 1 and sc["found"][0]["expected"]["indication"] == "AD"
    assert (sc["counts"]["invented"], sc["counts"]["wrong_date"]) == (1, 0) and sc["invented"][0]["type"] == T.DUPLICATE
    assert sc["counts"]["missed"] == 2


def test_the_old_one_program_one_event_keys_score_as_before():
    sc = score_events([_erow(1, program="PN-8047", ticker="PTGX", date_text="Q1 2027")],
                      _key([{"ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
                             "date_text": "Q1 2027", "quote": "q"}]))
    assert sc["counts"]["found"] == 1


def test_a_milestone_read_as_trial_results_is_found_with_a_note_when_the_quote_holds_both_families():
    key = _key([{**CLDX[0], "date_text": "summer 2026"}])
    both = "Phase 2 enrollment completed; topline data are expected in summer 2026."
    sc = score_events([_erow(1, event_type="trial_results", quote=both)], key)
    assert sc["counts"]["found"] == 1 and sc["counts"]["wrong_type"] == 0
    assert sc["found"][0]["note"] == ("The quote holds words of both milestone and trial results, so the event type "
                                      "the reader gave (trial_results) is not held against it; the key says milestone.")
    only = "Topline data are expected in summer 2026."
    sc2 = score_events([_erow(1, event_type="trial_results", quote=only)], key)
    assert sc2["counts"]["wrong_type"] == 1 and sc2["counts"]["found"] == 0, "one family's words: the type counts"
    other = score_events([_erow(1, event_type="launch", quote=both)], key)
    assert other["counts"]["wrong_type"] == 1, "only milestone versus trial results is a tie"


def test_a_key_may_carry_the_same_drug_on_several_events_and_is_still_checked(words_doc):
    from accuracy.key import check_key
    twice = _key([CLDX[0], {**CLDX[0]}])
    assert any("twice" in p["problem"] for p in check_key(twice, {"words": [{"id": "w", "text": "x", "x0": 0, "y0": 0,
                                                                              "x1": 1, "y1": 1}]}))
