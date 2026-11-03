"""Job 13: year-less dates read from the filing date, weekday / time / footnote marks around a date, day ranges, and the
date send-back for the second reader's rows."""

import json
import os
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capture import read_events
from checks.event_checks import clean_date_text, date_match, is_yearless_day, parse_date_phrase, parse_day_range
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


# Codex 5b / DATE-001 (2026-11-02) replaces Job 13's rule. Job 13 counted a year-less day from the filing date (the
# first such day on or after it), so "December 12" filed 2026-12-20 rolled to 2027-12-12 and "March 18" under a
# November 2026 filing became 2027-03-18: a year the source never printed. The adopted rule: a day with no printed year
# is never dated by code; its timing stays unresolved (no ISO bounds) and that is not a failed claim. The old
# expectations below were changed on purpose, not to make the suite pass.
def test_a_day_with_no_year_is_never_given_one():
    for text in ("Friday, Sept. 4 at 10:00 a.m. ET", "December 12", "February 29", "March 18", "Sept. 4"):
        assert is_yearless_day(text), text
        assert parse_date_phrase(text, "2026-11-02") == ("day", None, None), text
        assert parse_date_phrase(text) == ("day", None, None), text
    assert not is_yearless_day("September 4, 2026") and not is_yearless_day("Q3 2027")
    assert not is_yearless_day("February 30") and not is_yearless_day("Smarch 4")


def test_what_is_around_a_date_is_not_part_of_it():
    assert clean_date_text("Saturday, November 14,2026, 7:30 pm - 8:30 pm ET") == "November 14, 2026"
    assert clean_date_text("today, October 21, 2026, at 8:00 a.m. ET") == "October 21, 2026"
    assert clean_date_text("the second half of calendar year 2026") == "the second half of 2026"
    assert clean_date_text("Sep ’28 *") == "Sep ’28" and clean_date_text("(3Q’26)") == "3Q’26"


def test_a_day_range_is_two_days_of_one_month():
    assert parse_day_range("November 14 – 17, 2026") == (date(2026, 11, 14), date(2026, 11, 17))
    assert parse_day_range("November 17 - 14, 2026") is None and parse_day_range("November 14, 2026") is None


def test_date_match_keeps_a_year_less_day_unresolved_and_does_not_fail_it():
    # Codex 5b / DATE-001: formerly 2026-09-04 passed "with the year counted from the filing date" and 2027-09-04
    # failed. Neither year is printed, so neither can be established: the row's day cannot be compared.
    for given in ("2026-09-04", "2027-09-04"):
        res = date_match("Friday, Sept. 4 at 10:00 a.m. ET", given, CALL, filed="2026-08-05")
        assert res.result == "could_not_run" and "prints no year" in res.reason
        assert res.compared["timing"] == {"wording": "Friday, Sept. 4 at 10:00 a.m. ET", "start": None, "end": None,
                                          "precision": "unresolved"}
    none = date_match("Friday, Sept. 4 at 10:00 a.m. ET", None, CALL, filed="2026-08-05")
    assert none.result == "not_needed" and none.compared["timing"]["start"] is None
    assert date_match("March 18", None, "Data are expected on March 18.", filed="2026-11-02").result == "not_needed"
    # still literal: one day is not a period, and the words must be printed
    assert date_match("Friday, Sept. 4 at 10:00 a.m. ET", "2026-09-04", CALL, "2026-09-05",
                      filed="2026-08-05").result == "fail"
    assert date_match("March 18", None, "Data are expected in spring.", filed="2026-11-02").result == "fail"


def test_date_match_reads_a_range_a_weekday_and_a_footnote():
    q = "The meeting is November 14 – 17, 2026 in Boston."
    assert date_match("November 14 – 17, 2026", "2026-11-14", q, "2026-11-17").result == "pass"
    assert date_match("November 14 – 17, 2026", "2026-11-14", q, "2026-11-18").result == "fail"
    q2 = "Topline data are expected in Feb ’27*."
    assert date_match("Feb ’27*", "2027-02-01", q2, "2027-02-28", event_type="trial_results").result == "pass"


def test_a_row_with_a_year_less_day_says_so_on_its_trail_and_its_reason():
    # Codex 5b / DATE-001: formerly the trail said "Year inferred from the filing date" and the row was dated
    # 2026-09-04. The reader's proposed day is a guess at the year: the row keeps the printed words and no date.
    words_doc = json.loads((FIXTURES / f"{PIN}.words.json").read_text())
    ev = {**json.loads(FakeReader.default("Protagonist"))["events"][0], "date_text": "Sept. 4", "date": "2026-09-04",
          "date_end": None}
    row = build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")
    assert not any(t["step"] == "Year inferred from the filing date" for t in row["trail"])
    step = next(t for t in row["trail"] if t["step"] == "Year not printed")
    assert step["tag"] == "Code" and "2026-09-04" in step["text"] and "not kept" in step["text"]
    assert row["date_text"] == "Sept. 4" and row["date"] is None and row["date_end"] is None
    from runs.calendar import timing_state
    assert timing_state(row) == "timing_unresolved"


RECORDED = Path(os.environ.get("TWELVE2_DIR", Path.home() / "proof-reader-docs" / "runs" / "twelve-2"))


@pytest.mark.skipif(not (RECORDED / "run.json").is_file(), reason="twelve-2 is kept out of git")
def test_twelve_2_rows_that_failed_for_a_missing_year_are_checked_now():
    saved = RECORDED / "run.json"
    before = saved.read_bytes()
    run = json.loads(before)
    published = {s["id"]: s["published_date"] for s in run["sources"]}
    rows = [r for r in run["rows"] if any(c["name"] == "date_match" and "no day and no period" in c["reason"]
                                          for c in r["checks"])]
    assert len(rows) == 60
    after = [date_match(r["date_text"], r["date"], r["quote"], r["date_end"], event_type=r["event_type"],
                        title=r["title"], program=r["program"], filed=published[r["source_id"]]).result for r in rows]
    # Job 14: two "1Q27" rows now pass; five "today" rows could not run (no release date is passed here)
    # Job 18: "today at 8:00 a.m. ET" is a today-word now, so four more rows (e86, e186, e206, e255) go from fail to
    # could not run for the same reason
    # Job 21: date_match takes a person's chosen date and clause continuation, so 13 rows go from could not run to pass
    # DATE-LITERAL-001 retires semantic clause inference (old snapshot: 37/8/15).
    # These are literal-date outcomes only, not accepted event counts.
    # Codex 5b / DATE-001 (2026-11-02): e4 and e8 ("Friday, Sept. 4 at 10:00 a.m. ET", dated 2026-09-04) passed only
    # because the year was counted from the filing date; no year is printed, so they go from pass to could not run.
    assert after.count("pass") == 42 and after.count("fail") == 7 and after.count("could_not_run") == 11
    assert saved.read_bytes() == before
    from runs.state import current_state
    assert current_state(run, [])["counts"]["review"]["accepted"] == 0


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
