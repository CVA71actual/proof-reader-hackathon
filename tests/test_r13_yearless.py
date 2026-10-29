"""Job 13: year-less dates read from the filing date, weekday / time / footnote marks around a date, day ranges, and the
date send-back for the second reader's rows."""

import json
import os
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capture import read_events
from checks.event_checks import clean_date_text, date_match, parse_day_range, yearless_day
from runs import build_entry
from server.app import create_app
from tests.conftest import FIXTURES
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond

PIN = read_events.PIN
CHOICES = {"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
           "spending_limit_usd": None, "note_for_agent": None}
SOURCE = {"id": "s", "kind": "filing", "published_date": "2026-08-05", "pages": [{"page_number": 3}]}
READING = {"prompt_text": "p", "usage": {}, "fake": False}
CALL = "The company will host a conference call on Friday, Sept. 4 at 10:00 a.m. ET."


def test_a_day_with_no_year_is_the_first_such_day_on_or_after_the_filing_date():
    assert yearless_day("Friday, Sept. 4 at 10:00 a.m. ET", "2026-08-05") == date(2026, 9, 4)
    assert yearless_day("December 12", "2026-12-20") == date(2027, 12, 12)
    assert yearless_day("December 12", "2026-12-12") == date(2026, 12, 12)
    assert yearless_day("February 29", "2026-03-01") == date(2028, 2, 29)
    assert yearless_day("Sept. 4", None) is None
    assert yearless_day("September 4, 2026", "2026-08-05") is None and yearless_day("Q3 2027", "2026-08-05") is None


def test_what_is_around_a_date_is_not_part_of_it():
    assert clean_date_text("Saturday, November 14,2026, 7:30 pm - 8:30 pm ET") == "November 14, 2026"
    assert clean_date_text("today, October 21, 2026, at 8:00 a.m. ET") == "October 21, 2026"
    assert clean_date_text("the second half of calendar year 2026") == "the second half of 2026"
    assert clean_date_text("Sep ’28 *") == "Sep ’28" and clean_date_text("(3Q’26)") == "3Q’26"


def test_a_day_range_is_two_days_of_one_month():
    assert parse_day_range("November 14 – 17, 2026") == (date(2026, 11, 14), date(2026, 11, 17))
    assert parse_day_range("November 17 - 14, 2026") is None and parse_day_range("November 14, 2026") is None


def test_date_match_compares_the_full_date_with_the_year_from_the_filing_date():
    ok = date_match("Friday, Sept. 4 at 10:00 a.m. ET", "2026-09-04", CALL, filed="2026-08-05")
    assert ok.result == "pass" and "filing date" in ok.reason
    assert date_match("Friday, Sept. 4 at 10:00 a.m. ET", "2027-09-04", CALL, filed="2026-08-05").result == "fail"
    assert date_match("Friday, Sept. 4 at 10:00 a.m. ET", "2026-09-04", CALL, "2026-09-05",
                      filed="2026-08-05").result == "fail"
    assert date_match("Friday, Sept. 4 at 10:00 a.m. ET", "2026-09-04", CALL).result == "fail", "no filing date"


def test_date_match_reads_a_range_a_weekday_and_a_footnote():
    q = "The meeting is November 14 – 17, 2026 in Boston."
    assert date_match("November 14 – 17, 2026", "2026-11-14", q, "2026-11-17").result == "pass"
    assert date_match("November 14 – 17, 2026", "2026-11-14", q, "2026-11-18").result == "fail"
    q2 = "Topline data are expected in Feb ’27*."
    assert date_match("Feb ’27*", "2027-02-01", q2, "2027-02-28", event_type="trial_results").result == "pass"


def test_a_row_with_a_year_less_day_says_so_on_its_trail_and_its_reason():
    words_doc = json.loads((FIXTURES / f"{PIN}.words.json").read_text())
    ev = {**json.loads(FakeReader.default("Protagonist"))["events"][0], "date_text": "Sept. 4", "date": "2026-09-04",
          "date_end": None}
    row = build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")
    step = next(t for t in row["trail"] if t["step"] == "Year inferred from the filing date")
    assert step["tag"] == "Code" and "2026-09-04" in step["text"] and "2026-08-05" in step["text"]


RECORDED = Path(os.environ.get("TWELVE2_DIR", Path.home() / "proof-reader-docs" / "runs" / "twelve-2"))


@pytest.mark.skipif(not (RECORDED / "run.json").is_file(), reason="twelve-2 is kept out of git")
def test_twelve_2_rows_that_failed_for_a_missing_year_are_checked_now():
    run = json.loads((RECORDED / "run.json").read_text())
    published = {s["id"]: s["published_date"] for s in run["sources"]}
    rows = [r for r in run["rows"] if any(c["name"] == "date_match" and "no day and no period" in c["reason"]
                                          for c in r["checks"])]
    assert len(rows) == 60
    after = [date_match(r["date_text"], r["date"], r["quote"], r["date_end"], event_type=r["event_type"],
                        title=r["title"], program=r["program"], filed=published[r["source_id"]]).result for r in rows]
    # Job 14: two "1Q27" rows now pass; five "today" rows could not run (no release date is passed here)
    assert after.count("pass") == 24 and after.count("fail") == 12 and after.count("could_not_run") == 24


# ---------------------------------------------------------------- send-back for the second reader


def _start(tmp_path, first, second):
    runs = tmp_path / "runs"
    runs.mkdir()
    client = TestClient(create_app(runs_dir=runs, ui_dir=runs / "no-ui", reader=first, second_reader=second,
                                   background=False))
    rid = client.post("/api/runs/new", json=CHOICES).json()["id"]
    return json.loads((runs / rid / "run.json").read_text()), json.loads((runs / rid / "job.json").read_text())


def _q1():
    return next(e for e in json.loads(FakeReader.default("Protagonist"))["events"] if e["date_text"] == "Q1 2027")


def test_a_second_reader_row_whose_date_is_not_in_its_quote_is_sent_back(tmp_path):
    first_events = json.loads(FakeReader.default("Protagonist"))["events"]
    extra = {**_q1(), "date_text": "Q1 ’27", "quote": _q1()["quote"], "program": "Another program"}
    prompts = []

    def second_answer(prompt, n):
        prompts.append(prompt)
        if "NOT printed in the quote" in prompt:
            ids = [ln.split(":")[0] for ln in prompt.splitlines() if ln.startswith("e") and ":" in ln[:6]]
            return json.dumps({"rows": [{"id": i, "quote": _q1()["quote"], "date_text": "Q1 2027"} for i in ids]})
        return json.dumps({"events": first_events + [extra]})

    run, job = _start(tmp_path, FakeReader(), FakeSecond(answer=second_answer))
    only = [r for r in run["rows"] if r.get("found_by") == "second_reader"]
    assert len(only) == 1 and sum("NOT printed in the quote" in p for p in prompts) == 1
    row = only[0]
    steps = [t["step"] for t in row["trail"]]
    assert steps.count("Sent back for the date") == 1 and steps[-1] == "Send-back result"
    assert row["date_text"] == "Q1 2027" and row["group"] == "open", "still the second reader's open row"
    assert row["reason"].endswith("the first reader did not read it. Look at it: keep it as an event, fix it, or "
                                  "remove it.")
    assert job["sent_back"]["src-ptgx-8k-2026-08-05-ex991#second"] == "done"
