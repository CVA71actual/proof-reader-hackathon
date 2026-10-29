"""Job 8: the round 3 findings (tests/test_redteam_round3.py), with the cases around them.

Pairing of two readers' events is by a contiguous run of words, never a bag of words; every value the second reader gives
for a cell is kept; a run is done only if every call, the last one included and the second reader's included, stayed inside
the spending limit; a reply is priced by the model it names.
"""

import json

import pytest
from fastapi.testclient import TestClient

from capture import read_events
from capture.reader import Reply
from runs import compare
from server import jobs
from server.app import create_app
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond

BASE = ("PN-8047 (oral small molecule hepcidin functional mimetic): IND-enabling studies are ongoing, with Phase 1 "
        "initiation anticipated in Q1 2027.")


def row(quote, **kw):
    return {"quote": quote, "program": "PN-8047", "indication": None, "event_type": "milestone", **kw}


# ---------------------------------------------------------------- pairing by a contiguous run of words


@pytest.mark.parametrize("other", [
    BASE,
    BASE.lower(),
    "  " + BASE.replace(" ", "   ") + "  ",
    BASE[:-1],                                            # no full stop
    BASE + " More words follow the sentence.",           # the other quote runs on
    "IND-enabling studies are ongoing, with Phase 1 initiation anticipated in Q1 2027.",   # a run inside it
    "Phase 1 initiation anticipated in Q1 2027",
])
def test_one_quote_a_contiguous_run_of_the_other_pairs(other):
    assert compare.match_events([row(BASE)], [row(other)]) == {0: 0}
    assert compare.match_events([row(other)], [row(BASE)]) == {0: 0}, "either way round"


@pytest.mark.parametrize("other", [
    "Q1 2027 initiation Phase 1 anticipated in with",                     # the same words, reordered
    "initiation anticipated Phase 1 in Q1 2027 ongoing studies are",
    "IND-enabling studies are ongoing, with Phase 2 initiation anticipated in Q1 2027.",   # one word changed
    "PN-8047 (oral small molecule) IND-enabling studies are ongoing with Phase 1 initiation anticipated in Q1 2027.",
    "Phase 1 initiation Q1 2027",                                          # words dropped from the middle
    "",
    None,
])
def test_a_bag_of_words_or_a_near_copy_never_pairs(other):
    assert compare.match_events([row(BASE)], [row(other)]) == {}


def test_two_readers_that_overlap_in_most_words_but_not_in_a_run_are_two_events_not_agreement():
    a = "Results are expected in Q3 2027 and the filing follows in Q4 2027."
    b = "The filing follows in Q4 2027 and results are expected in Q3 2027."
    assert compare.match_events([row(a)], [row(b)]) == {}


def test_a_reordered_second_reader_quote_is_a_new_row_and_the_first_row_is_not_marked_as_agreed():
    first = {**row(BASE), "date": "2027-01-01", "date_end": "2027-03-31", "status": "matched", "reason": "ok",
             "checks": [{"result": "pass"}], "trail": []}
    second = {**row("Q1 2027 initiation Phase 1 anticipated in with"), "date": "2027-01-01", "date_end": "2027-03-31"}
    reading = {"prompt_text": "p", "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    rows, new = compare.apply_events([first], [second], reading, {}, {}, "2026-10-29T00:00:00+00:00", 2)
    assert len(new) == 1 and new[0]["found_by"] == "second_reader" and new[0]["group"] == "open"
    assert new[0]["checks"] == [] and "page was not captured" in new[0]["reason"]
    assert "disagreement" not in first and first["status"] == "matched"
    assert all("read the same" not in t["text"] for t in first["trail"])


# ---------------------------------------------------------------- every value the second reader gives for a cell


CHECK = {"result": "pass"}


def first_table(value="(1,234)"):
    return {"rows": [{"label": "Net loss", "cells": [
        {"id": "t1.0.0", "column": "2025", "value": value, "status": "matched", "checks": [CHECK]}]}]}


def second_table(*values, column="2025", label="Net loss"):
    return {"rows": [{"label": label, "cells": [{"column": column, "value": v}]} for v in values]}


def test_a_later_conflicting_value_is_a_disagreement_even_when_an_earlier_one_agrees():
    t = first_table()
    assert compare.apply_table(t, second_table("(1,234)", "(9,999)")) == ["t1.0.0"]
    assert t["rows"][0]["cells"][0]["disagreement"] == {"field": "value", "first_reader": "(1,234)",
                                                        "second_reader": "(9,999)"}


def test_an_earlier_conflicting_value_is_a_disagreement_even_when_a_later_one_agrees():
    t = first_table()
    assert compare.apply_table(t, second_table("(9,999)", "(1,234)")) == ["t1.0.0"]
    assert t["rows"][0]["cells"][0]["disagreement"]["second_reader"] == "(9,999)"


def test_the_first_conflicting_value_is_the_one_recorded_when_there_are_several():
    t = first_table()
    compare.apply_table(t, second_table("(1,234)", "(7,000)", "(8,000)"))
    assert t["rows"][0]["cells"][0]["disagreement"]["second_reader"] == "(7,000)"


@pytest.mark.parametrize("values", [("(1,234)",), ("(1,234)", "(1,234)"), (" (1,234) ",)])
def test_values_that_all_agree_mark_nothing(values):
    t = first_table()
    assert compare.apply_table(t, second_table(*values)) == []
    assert "disagreement" not in t["rows"][0]["cells"][0] and t["rows"][0]["cells"][0]["status"] == "matched"


def test_the_column_is_the_same_however_the_year_is_printed_and_a_cell_only_the_second_reader_has_is_ignored():
    t = first_table()
    assert compare.apply_table(t, second_table("(9,999)", column="December 31, 2025")) == ["t1.0.0"]
    t2 = first_table()
    assert compare.apply_table(t2, second_table("(9,999)", column="2024")) == [], "another column: not this cell"
    assert compare.apply_table(first_table(), second_table("(9,999)", label="Other row")) == []


def test_a_failed_first_reading_stays_failed_with_the_disagreement():
    t = first_table()
    t["rows"][0]["cells"][0]["checks"] = [{"result": "fail"}]
    compare.apply_table(t, second_table("(9,999)"))
    assert t["rows"][0]["cells"][0]["status"] == "failed"
    assert t["rows"][0]["cells"][0]["disagreement"]["second_reader"] == "(9,999)"


# ---------------------------------------------------------------- the spending limit, the last call and the second reader


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def start(runs_dir, first, second=None, **choices):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=first, second_reader=second,
                                   wonder_reader=None, sam_reader=None, background=False))
    body = {"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
            "spending_limit_usd": 0.5, "note_for_agent": None, **choices}
    r = client.post("/api/runs/new", json=body)
    assert r.status_code == 202, r.text
    return client, r.json()["id"]


def meta(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())["run"]


def test_a_run_whose_every_call_stayed_inside_the_limit_is_done(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=0.10))
    m = meta(runs_dir, rid)
    assert m["status"] == "done" and m["finished_at"] and m["spend_usd"] == pytest.approx(0.10)


def test_a_call_that_lands_exactly_on_the_limit_stayed_inside_it(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=0.50))
    assert meta(runs_dir, rid)["status"] == "done"


def test_the_last_call_that_crosses_the_limit_stops_the_run_with_the_plain_reason(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=0.60))
    m = meta(runs_dir, rid)
    assert (m["status"], m["finished_at"]) == ("needs_input", None)
    assert m["status_reason"] == ("Stopped at the spending limit ($0.60 of $0.50 used): spending limit reached. "
                                  "Resume with a higher spending_limit_usd to read more.")
    assert m["spend_usd"] == pytest.approx(0.60)


def test_a_run_stopped_by_its_last_call_finishes_when_resumed_with_a_higher_limit_and_reads_nothing_again(runs_dir):
    first = FakeReader(cost=0.60)
    client, rid = start(runs_dir, first)
    assert client.post(f"/api/runs/{rid}/resume").status_code == 409, "the same limit does not help"
    assert client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 1.0}).status_code == 202
    m = meta(runs_dir, rid)
    assert m["status"] == "done" and m["finished_at"] and len(first.prompts) == 1, "no page was read twice"
    assert m["choices"]["spending_limit_usd"] == 1.0


def test_a_limit_crossed_by_the_second_reader_after_a_cheap_first_read_stops_the_run(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=0.30), FakeSecond(tokens=(1_500_000, 0)))
    m = meta(runs_dir, rid)
    assert m["status"] == "needs_input" and m["spend_usd"] == pytest.approx(0.60)
    assert m["status_reason"].startswith("Stopped at the spending limit") and m["finished_at"] is None


def test_the_second_readers_cost_is_counted_before_the_next_page_so_the_next_page_is_not_read(runs_dir):
    first = FakeReader(cost=0.30)
    _, rid = start(runs_dir, first, FakeSecond(tokens=(1_500_000, 0)), companies=["PTGX", "IONS"])
    m = meta(runs_dir, rid)
    assert m["status"] == "needs_input" and len(first.prompts) == 1, "IONS was never asked for"
    assert m["spend_usd"] == pytest.approx(0.60)


def test_a_second_reader_that_stays_inside_the_limit_leaves_the_run_done(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=0.10), FakeSecond(tokens=(500_000, 0)))
    m = meta(runs_dir, rid)
    assert m["status"] == "done" and m["spend_usd"] == pytest.approx(0.20)


def test_no_limit_means_no_stop_however_much_is_spent(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=50.0), spending_limit_usd=None)
    assert meta(runs_dir, rid)["status"] == "done"


# ---------------------------------------------------------------- a reply is priced by the model it names


class Names:
    """A reader that advertises one model and whose replies name another."""

    def __init__(self, advertised, replied, calls):
        self.model, self.replied, self.calls = advertised, replied, calls

    def ready(self):
        return None

    def call(self, prompt, image=None):
        self.calls.append(prompt)
        return Reply(FakeReader.default(prompt), self.replied, prompt_tokens=10, completion_tokens=10)


def test_a_reply_that_names_an_unpriced_model_stops_the_run_naming_that_model(runs_dir):
    calls = []
    _, rid = start(runs_dir, Names("muse-spark-1.3", "not-a-priced-model", calls))
    m = meta(runs_dir, rid)
    job = json.loads((runs_dir / rid / "job.json").read_text())
    assert m["status"] == "needs_input" and m["finished_at"] is None and m["spend_usd"] is None
    assert "no price for not-a-priced-model" in m["status_reason"] and "could not be counted" in m["status_reason"]
    assert job["unpriced_calls"] == 1 and job["unpriced_models"] == ["not-a-priced-model"]


def test_the_unpriced_reply_stops_the_run_before_the_next_call(runs_dir):
    calls = []
    _, rid = start(runs_dir, Names("muse-spark-1.3", "not-a-priced-model", calls), companies=["PTGX", "IONS"])
    assert len(calls) == 1, "the IONS page was not read"
    assert meta(runs_dir, rid)["status"] == "needs_input"


def test_a_reply_naming_a_priced_model_is_priced_by_that_model_not_the_advertised_one(runs_dir):
    calls = []
    _, rid = start(runs_dir, Names("not-a-priced-model", "muse-spark-1.3", calls), spending_limit_usd=None)
    m = meta(runs_dir, rid)
    assert m["status"] == "done" and m["spend_usd"] == pytest.approx(0.000050)   # 10 in at $1/M + 10 out at $4/M


def test_an_unpriced_reply_without_a_limit_is_not_a_problem(runs_dir):
    calls = []
    _, rid = start(runs_dir, Names("muse-spark-1.3", "not-a-priced-model", calls), spending_limit_usd=None)
    m = meta(runs_dir, rid)
    assert m["status"] == "done" and m["spend_usd"] is None


def test_resume_refuses_an_unpriced_run_even_with_a_higher_limit(runs_dir):
    client, rid = start(runs_dir, Names("muse-spark-1.3", "not-a-priced-model", []))
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99})
    assert r.status_code == 409 and "could not be counted" in r.json()["detail"]
