"""Job 12, the fix wave from the live twelve-company read: boxes located by code, one date send-back, relative periods
counted from the filing date. (The Wonder budget is tested in tests/test_escalation.py.)"""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from capture import read_events
from checks.event_checks import date_match, relative_period
from checks.selector_match import selector_match
from runs import build_entry
from server.app import create_app
from tests.conftest import FIXTURES, REPO
from tests.test_run_controls import FakeReader

PIN = read_events.PIN
CHOICES = {"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
           "spending_limit_usd": None, "note_for_agent": None}
SOURCE = {"id": "s", "kind": "filing", "published_date": "2026-08-05", "pages": [{"page_number": 3}]}
READING = {"prompt_text": "p", "usage": {}, "fake": False}


@pytest.fixture
def words_doc():
    return json.loads((FIXTURES / f"{PIN}.words.json").read_text())


def right_events():
    return json.loads(FakeReader.default("Protagonist"))["events"]


# ---------------------------------------------------------------- relative periods


def test_relative_periods_are_counted_from_the_filing_date():
    assert relative_period("by year-end", "2026-08-05") == (date(2026, 9, 1), date(2026, 12, 31))
    assert relative_period("this year", "2026-08-05") == (date(2026, 1, 1), date(2026, 12, 31))
    assert relative_period("later this year", "2026-08-05") == (date(2026, 8, 5), date(2026, 12, 31))
    assert relative_period("this quarter", "2026-08-05") == (date(2026, 7, 1), date(2026, 9, 30))
    assert relative_period("next quarter", "2026-08-05") == (date(2026, 10, 1), date(2026, 12, 31))
    assert relative_period("next quarter", "2026-11-05") == (date(2027, 1, 1), date(2027, 3, 31))
    assert relative_period("year-end", None) is None and relative_period("Q3 2027", "2026-08-05") is None


def test_date_match_compares_a_relative_period_with_the_rows_dates():
    q = "We expect topline data by year-end."
    assert date_match("by year-end", "2026-10-01", q, "2026-12-31", filed="2026-08-05").result == "pass"
    assert date_match("by year-end", "2027-01-01", q, "2027-03-31", filed="2026-08-05").result == "fail"
    assert date_match("by year-end", None, q, filed="2026-08-05").result == "pass"
    assert date_match("by year-end", None, "Data are expected soon.", filed="2026-08-05").result == "fail"
    assert date_match("by year-end", None, q).result == "fail", "with no filing date there is no year to count from"


def test_a_row_with_a_relative_period_gets_dates_and_a_trail_step(words_doc):
    quote = next(e["quote"] for e in right_events())
    first = quote.split()[:3]
    ev = {**right_events()[0], "date_text": "year-end", "date": None, "date_end": None,
          "quote": quote + " year-end"}      # not on the page: only the date is under test, the quote may fail
    row = build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")
    step = next(t for t in row["trail"] if t["step"] == "Year inferred from the filing date")
    assert step["tag"] == "Code" and "2026-09-01 to 2026-12-31" in step["text"]
    assert (row["date"], row["date_end"]) == ("2026-09-01", "2026-12-31")


# ---------------------------------------------------------------- the box, located by code


def _event(words_doc, **kw):
    return {**right_events()[0], **kw}


def test_a_wrong_box_is_replaced_by_the_words_found_on_the_page(words_doc):
    ev = _event(words_doc, box=[0, 0, 5, 5])
    row = build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")
    step = next(t for t in row["trail"] if t["step"] == "Box located by code")
    assert step["tag"] == "Code" and "only a hint" in step["text"]
    box_check = next(c for c in row["checks"] if c["name"] == "selector_match")
    assert box_check["result"] == "pass" and row["box"] != [0, 0, 5, 5]


def test_no_box_at_all_is_located_too(words_doc):
    ev = _event(words_doc, box=None, word_ids=None)
    row = build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")
    assert any(t["step"] == "Box located by code" for t in row["trail"])
    assert next(c for c in row["checks"] if c["name"] == "selector_match")["result"] == "pass"


def test_a_box_that_is_right_is_left_as_the_reader_drew_it(words_doc):
    ev = _event(words_doc)
    row = build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")
    assert row["box"] == [float(v) for v in ev["box"]]
    assert not any(t["step"] == "Box located by code" for t in row["trail"])


def test_a_quote_that_is_not_on_the_page_is_not_given_a_box(words_doc):
    ev = _event(words_doc, quote="Words nobody printed on this page", box=[0, 0, 5, 5])
    row = build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")
    assert row["box"] == [0.0, 0.0, 5.0, 5.0]
    assert not any(t["step"] == "Box located by code" for t in row["trail"])


RECORDED = REPO / "runs" / "twelve-1"


@pytest.mark.skipif(not (RECORDED / "run.json").is_file(), reason="runs/twelve-1 is not in this checkout")
def test_recorded_rows_that_failed_the_box_and_whose_quote_is_printed_become_located():
    run = json.loads((RECORDED / "run.json").read_text())
    pages = {s["id"]: s["pages"][0] for s in run["sources"]}
    seen = 0
    for row in run["rows"]:
        result = {c["name"]: c["result"] for c in row["checks"]}
        if result.get("selector_match") != "fail" or result.get("quote_match") != "pass":
            continue
        page = pages[row["source_id"]]
        doc = json.loads((RECORDED / page["words"]).read_text())
        box = build_entry.locate_box(row["quote"], row["box"], doc["words"])
        assert box is not None, row["id"]
        assert selector_match(row["quote"], box, doc["words"], doc["page_width"], doc["page_height"],
                              quote_lines=True).result == "pass", row["id"]
        seen += 1
    assert seen == 8, "the recorded run has rows that failed the box with their quote on the page"


# ---------------------------------------------------------------- the date send-back


def _start(tmp_path, reader):
    runs = tmp_path / "runs"
    runs.mkdir()
    client = TestClient(create_app(runs_dir=runs, ui_dir=runs / "no-ui", reader=reader, background=False))
    rid = client.post("/api/runs/new", json=CHOICES).json()["id"]
    return json.loads((runs / rid / "run.json").read_text()), json.loads((runs / rid / "job.json").read_text())


def _reader(send_back, calls):
    """Reads the page with the Q1 2027 event's date text moved off the quote; answers the send-back with `send_back`."""
    events = right_events()
    for e in events:
        if e["date_text"] == "Q1 2027":
            e["date_text"] = "Q1 ’27"

    def answer(prompt, n):
        calls.append("send-back" if "NOT printed in the quote" in prompt else "read")
        if calls[-1] == "read":
            return json.dumps({"events": events})
        ids = [line.split(":")[0] for line in prompt.splitlines() if line.startswith("e") and ":" in line[:5]]
        return json.dumps({"rows": [send_back(i) for i in ids]})
    return FakeReader(answer=answer)


def _q1_quote():
    return next(e["quote"] for e in right_events() if e["date_text"] == "Q1 2027")


def test_one_send_back_asks_for_the_sentence_that_states_the_date(tmp_path):
    calls = []
    run, job = _start(tmp_path, _reader(lambda i: {"id": i, "quote": _q1_quote(), "date_text": "Q1 2027"}, calls))
    assert calls == ["read", "send-back"], "one call for the page"
    row = next(r for r in run["rows"] if r["quote"] == _q1_quote())
    assert (row["date_text"], row["status"]) == ("Q1 2027", "matched")
    steps = [t["step"] for t in row["trail"]]
    assert steps.count("Sent back for the date") == 1 and "Sentence for the date" in steps
    assert next(t for t in row["trail"] if t["step"] == "Send-back result")["text"].endswith("the checks decide: matched.")
    assert [m for m in run["mistakes"] if m["target"] == row["id"] and m["caught_by"] == "code"]
    assert job["sent_back"] == {"src-ptgx-8k-2026-08-05-ex991": "done"}
    assert run["run"]["turn_count"] == 2


def test_an_answer_with_no_sentence_leaves_the_row_failed(tmp_path):
    calls = []
    run, _ = _start(tmp_path, _reader(lambda i: {"id": i, "quote": None, "date_text": None}, calls))
    row = next(r for r in run["rows"] if r["date_text"] == "Q1 ’27")
    assert row["status"] == "failed" and row["trail"][-1]["text"].endswith("The row stays as it was.")
    assert calls.count("send-back") == 1


def test_a_sentence_not_printed_on_the_page_is_not_taken(tmp_path):
    calls = []
    run, _ = _start(tmp_path, _reader(lambda i: {"id": i, "quote": "A sentence nobody printed", "date_text": "Q1 2027"},
                                      calls))
    row = next(r for r in run["rows"] if r["date_text"] == "Q1 ’27")
    assert row["status"] == "failed" and row["quote"] == _q1_quote()
    assert "not printed on the page" in row["trail"][-1]["text"]


def test_a_page_with_no_date_failure_is_not_sent_back(tmp_path):
    calls = []
    _start(tmp_path, FakeReader(answer=lambda p, n: (calls.append(1), FakeReader.default(p))[1]))
    assert len(calls) == 1
