"""R5: the second reader (Muse Spark Open), compared by code. Gap 7.

No test here reaches the network or a key: the readers are fakes, and the live path is checked only through the
saved live reading in fixtures/ and the route logic with the environment replaced.
"""

import copy
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import read_events, read_table, second_reader
from capture.capture_source import load_entry
from capture.reader import Reply
from capture.second_reader import BASETEN_MODEL, NO_KEY, OPENROUTER_MODEL, SecondReader
from checks import coverage
from runs import build_entry, compare, prices
from runs.state import current_state
from server import jobs
from server.app import create_app
from tests.conftest import FIXTURES, REPO
from tests.test_run_controls import FakeReader

RUNS = REPO / "runs"
RUN_SCHEMA = Draft202012Validator(json.loads((RUNS / "schema" / "run.schema.json").read_text()))
PIN = read_events.PIN
CHOICES = {"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
           "spending_limit_usd": None, "note_for_agent": None}
BOTH = {**CHOICES, "companies": ["PTGX", "IONS"]}


def fake_events():
    return json.loads(FakeReader.default("Protagonist"))["events"]


class FakeSecond:
    """A second reader that records what it was asked and answers as told. Never touches the network."""
    openrouter_allowed = True

    def __init__(self, answer=None, tokens=(10000, 5000), problem=None, route=None, model=BASETEN_MODEL):
        self.answer, self.tokens, self.problem, self.route, self.model = answer, tokens, problem, route, model
        self.prompts, self.images = [], []

    def ready(self):
        return self.problem

    def call(self, prompt, image=None):
        self.prompts.append(prompt)
        self.images.append(image)
        raw = self.answer(prompt, len(self.prompts)) if self.answer else FakeReader.default(prompt)
        return Reply(raw, self.model, prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1],
                     extra={"route": self.route} if self.route else None)


def events_answer(events):
    return lambda prompt, n: json.dumps({"events": events})


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def start(runs_dir, first=None, second=None, **choices):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=first or FakeReader(),
                                   second_reader=second, background=False))
    r = client.post("/api/runs/new", json={**CHOICES, **choices})
    assert r.status_code == 202, r.text
    return client, r.json()["id"]


def run_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())


def job_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "job.json").read_text())


# ---------------------------------------------------------------- the second reader itself


def test_the_model_string_and_endpoint_are_exactly_the_recorded_route():
    assert BASETEN_MODEL == "meta-models/muse-spark-open-1026"
    assert second_reader.BASETEN_BASE_URL == "https://inference.baseten.co/v1"
    assert OPENROUTER_MODEL == "meta/muse-spark-open" and second_reader.OPENROUTER_CAP_USD == 20.0


def test_the_route_is_baseten_first_and_openrouter_only_with_its_key_and_under_its_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    for k in ("BASETEN_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = SecondReader()
    assert (r.route(), r.ready()) == (None, NO_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    assert (r.route(), r.model) == ("openrouter", OPENROUTER_MODEL) and r.ready() is None
    r.openrouter_allowed = False
    assert r.route() is None and r.ready() == second_reader.CAP_REACHED, "the key is there; the cap is why not"
    r.openrouter_blocked = second_reader.UNPRICED
    assert r.ready() == second_reader.UNPRICED
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert r.ready() == NO_KEY
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    r.openrouter_allowed, r.openrouter_blocked = True, None
    monkeypatch.setenv("BASETEN_API_KEY", "bt-test")
    assert (r.route(), r.model) == ("baseten", BASETEN_MODEL), "Baseten stays the route even past the cap"


def test_a_failed_baseten_call_falls_back_only_when_the_fallback_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    monkeypatch.setenv("BASETEN_API_KEY", "bt-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    calls = []

    def ask(self, route, prompt, image=None):
        calls.append(route)
        return Reply(None, "m", error="The model call did not succeed (APIError, status 503).") if route == "baseten" \
            else Reply('{"events": []}', OPENROUTER_MODEL, extra={"route": "openrouter"})

    monkeypatch.setattr(SecondReader, "_ask", ask)
    assert SecondReader().call("p").extra == {"route": "openrouter"} and calls == ["baseten", "openrouter"]
    calls.clear()
    capped = SecondReader()
    capped.openrouter_allowed = False
    assert capped.call("p").error and calls == ["baseten"]
    calls.clear()
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert SecondReader().call("p").error and calls == ["baseten"]


def test_the_second_reader_takes_no_first_reader_answer():
    import inspect
    assert list(inspect.signature(SecondReader.call).parameters) == ["self", "prompt", "image"]
    assert list(inspect.signature(second_reader.reading_record).parameters) == ["reply", "prompt", "parsed",
                                                                                 "problem", "entry", "input"]


def test_the_saved_live_second_reading_of_the_ptgx_page():
    rec = json.loads((FIXTURES / f"{PIN}.events.second.json").read_text())
    entry = load_entry(PIN)
    assert rec["model"] == BASETEN_MODEL and rec["route"] == "baseten" and rec["second_reader"] is True
    assert rec["base_url"] == "https://inference.baseten.co/v1" and rec["readable"] and rec["fake"] is False
    assert rec["words_sha256"] == entry["words_sha256"] and rec["page_sha256"] == entry["page_sha256"]
    assert read_table.sha256_text(rec["prompt_text"]) == rec["prompt_sha256"]
    assert all(isinstance(rec["usage"][k], int) and rec["usage"][k] > 0 for k in ("prompt_tokens", "completion_tokens"))
    assert [e["program"] for e in rec["events"]] == ["PN-458o", "PN-8047"]
    text = json.dumps(rec)
    for name in ("BASETEN_API_KEY", "OPENROUTER_API_KEY", "MODEL_API_KEY"):
        assert not os.environ.get(name) or os.environ[name] not in text


# ---------------------------------------------------------------- comparing by code


ROW = {"id": "e1", "ticker": "PTGX", "program": "PN-8047", "indication": None, "event_type": "milestone",
       "date": "2027-01-01", "date_end": "2027-03-31", "date_text": "Q1 2027", "quote": "PN-8047 Phase 1 initiation "
       "is anticipated in Q1 2027."}


def test_events_are_matched_by_overlapping_quote_and_a_shared_name():
    other = {**ROW, "program": "PN-458o", "quote": "PN-458o Phase 1 initiation is anticipated in 2H 2027."}
    ev = {**ROW, "quote": "PN-8047 (oral) Phase 1 initiation is anticipated in Q1 2027."}
    assert compare.match_events([ROW, other], [ev]) == {0: 0}
    assert compare.match_events([ROW], [{**ev, "program": "Something else"}]) == {}, "no shared name"
    assert compare.match_events([ROW], [{**ev, "program": None}]) == {0: 0}, "a name only one side gave is fine"
    assert compare.match_events([ROW], [{**ev, "quote": "Unrelated sentence about cash."}]) == {}
    assert compare.match_events([ROW, ROW], [ev]) == {0: 0}, "a second event pairs with one row only"


def test_every_compared_field_is_a_difference_when_it_differs():
    base = {**ROW}
    assert compare.differences(ROW, base) == []
    for field, new in (("date", "2027-02-01"), ("date_end", "2027-04-30"), ("event_type", "trial_results"),
                       ("program", "PN-9999"), ("indication", "Polycythemia vera")):
        diffs = compare.differences(ROW, {**base, field: new})
        assert [(d["field"], d["second_reader"]) for d in diffs] == [(field, new)]
    assert compare.differences(ROW, {**base, "event_type": " Milestone "}) == [], "case and spacing are ignored"
    longer = {**base, "quote": base["quote"] + " Further text."}
    assert compare.differences(ROW, longer) == [], "a quote that runs longer is not a disagreement"
    assert [d["field"] for d in compare.differences(ROW, {**base, "date": "x", "event_type": "y"})] == [
        "date", "event_type"]


def test_the_table_comparison_marks_only_the_values_that_differ():
    check_ok = {"result": "pass"}
    first = {"rows": [{"label": "Net loss", "cells": [
        {"id": "t1.0.0", "column": "2025", "value": "(1,234)", "status": "matched", "checks": [check_ok]},
        {"id": "t1.0.1", "column": "2024", "value": "(2,000)", "status": "matched", "checks": [check_ok]}]}]}
    second = {"rows": [{"label": "net loss", "cells": [{"column": "December 31, 2025", "value": "(1,299)"},
                                                      {"column": "FY2024", "value": "(2,000)"}]}]}
    assert compare.apply_table(first, second) == ["t1.0.0"]
    c = first["rows"][0]["cells"]
    assert (c[0]["status"], c[0]["disagreement"]) == ("undecided", {"field": "value", "first_reader": "(1,234)",
                                                                    "second_reader": "(1,299)"})
    assert c[1]["status"] == "matched" and "disagreement" not in c[1]


# ---------------------------------------------------------------- in a run: agree, disagree, extra, unreadable


def test_two_readers_that_agree_change_nothing_and_say_agreement_is_not_a_check(runs_dir):
    first, second = FakeReader(), FakeSecond()
    _, rid = start(runs_dir, first, second)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    assert run["run"]["status"] == "done" and run["run"]["second_reader"] == {
        "model": "Muse Spark Open", "status": "read", "reason": None, "input": "image"}
    plain = run_of(runs_dir, start(runs_dir, FakeReader(), None)[1])
    assert [(r["status"], r["group"]) for r in run["rows"]] == [(r["status"], r["group"]) for r in plain["rows"]]
    assert all("disagreement" not in r for r in run["rows"])
    for row in run["rows"]:
        steps = [(t["step"], t["tag"], t["who"]) for t in row["trail"][-3:]]
        assert steps == [("Instructions sent to the second reader", "Code", "capture.second_reader"),
                         ("Second reading", "Model", "Muse Spark Open"), ("Compared", "Code", "runs.compare")]
        assert "Agreement is not a check" in row["trail"][-1]["text"]
        model_step = row["trail"][-2]
        assert model_step["tokens"]["prompt"] > 0 and model_step["cost_usd"] > 0
    assert run["run"]["turn_count"] == 2 and len(first.prompts) == len(second.prompts) == 1


def test_the_second_reader_gets_the_page_image_and_no_word_list_and_never_the_first_readers_answer(runs_dir):
    first, second = FakeReader(), FakeSecond()
    _, rid = start(runs_dir, first, second)
    entry = load_entry(PIN)
    assert second.prompts == [read_events.build_image_prompt(entry)], "the image prompt, built from the page's facts"
    assert "\nw0|" not in second.prompts[0] and "id|text|x0|y0|x1|y1" not in second.prompts[0], "no word list"
    png = (runs_dir / rid / "pages" / "p3.png").read_bytes()
    assert second.images == [png] and png[:4] == b"\x89PNG", "the page's own image, the bytes the run hashed"
    assert first.prompts != second.prompts and FakeReader.default(first.prompts[0]) not in second.prompts[0]
    assert run_of(runs_dir, rid)["run"]["second_reader"]["input"] == "image"


def test_the_trail_says_what_the_second_reader_was_given(runs_dir):
    _, rid = start(runs_dir, FakeReader(), FakeSecond())
    for row in run_of(runs_dir, rid)["rows"]:
        steps = {t["step"]: t for t in row["trail"]}
        assert steps["Second reader input"]["text"] == compare.INPUT_WORDS["image"]
        assert "does not depend on the text layer" in steps["Second reader input"]["text"]
        assert steps["Instructions sent to the second reader"]["text"] == read_events.build_image_prompt(
            load_entry(PIN))


def test_an_unreadable_image_answer_is_followed_once_by_the_words_as_a_fallback(runs_dir):
    events = fake_events()

    def answer(prompt, n):
        return "I could not read the image." if n == 1 else json.dumps({"events": events})

    second = FakeSecond(answer=answer)
    _, rid = start(runs_dir, FakeReader(), second)
    run = run_of(runs_dir, rid)
    assert len(second.prompts) == 2 and run["run"]["status"] == "done"
    assert "\nw0|" not in second.prompts[0] and "w0|" in second.prompts[1], "words only in the fallback"
    assert second.images[0] is not None and second.images[1] is not None
    assert run["run"]["second_reader"] == {"model": "Muse Spark Open", "status": "read", "reason": None,
                                           "input": "image_and_words"}
    assert run["run"]["turn_count"] == 3, "the fallback is its own counted turn"
    row = run["rows"][0]
    assert next(t for t in row["trail"] if t["step"] == "Second reader input")["text"] == \
        compare.INPUT_WORDS["image_and_words"]


def test_a_second_only_row_from_an_image_read_has_its_box_in_page_points(runs_dir):
    words = json.loads((FIXTURES / f"{PIN}.words.json").read_text())
    extra_sentence = next(s for s in coverage.sentences(words["words"]) if s["text"].startswith("Discovery"))
    x0, y0, x1, y1 = extra_sentence["box"]
    grid = [x0 * 1000 / words["page_width"], y0 * 1000 / words["page_height"],
            x1 * 1000 / words["page_width"], y1 * 1000 / words["page_height"]]
    extra = {"ticker": "PTGX", "company": "Protagonist Therapeutics, Inc.", "program": None, "indication": None,
             "event_type": "milestone", "title": "Discovery programs", "date_text": "2027", "date": "2027-01-01",
             "date_end": "2027-12-31", "quote": extra_sentence["text"], "box": grid}
    _, rid = start(runs_dir, FakeReader(), FakeSecond(answer=events_answer(fake_events() + [extra])))
    only = next(r for r in run_of(runs_dir, rid)["rows"] if r.get("found_by") == "second_reader")
    assert only["box"] == pytest.approx([x0, y0, x1, y1], abs=0.02)
    assert next(c for c in only["checks"] if c["name"] == "selector_match")["result"] == "pass"


def test_a_disagreement_on_the_date_makes_the_row_undecided_and_goes_to_a_person(runs_dir):
    events = fake_events()
    moved = copy.deepcopy(events)
    moved[1]["date"], moved[1]["date_end"] = "2027-04-01", "2027-06-30"     # Q2 for the Q1 row
    _, rid = start(runs_dir, FakeReader(), FakeSecond(answer=events_answer(moved)))
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    row = next(r for r in run["rows"] if r["date_text"] == "Q1 2027")
    assert (row["status"], row["group"]) == ("undecided", "open")
    assert row["disagreement"] == {"field": "date", "first_reader": "2027-01-01", "second_reader": "2027-04-01",
                                   "also": [{"field": "date_end", "first_reader": "2027-03-31",
                                             "second_reader": "2027-06-30"}]}
    assert row["reason"].startswith("The two readers disagree on date: Muse Spark read 2027-01-01, Muse Spark Open "
                                    "read 2027-04-01.")
    compared = row["trail"][-1]
    assert compared["step"] == "Compared" and "date: Muse Spark read 2027-01-01" in compared["text"]
    assert "date_end: Muse Spark read 2027-03-31" in compared["text"]
    assert next(r for r in run["rows"] if r["date_text"] != "Q1 2027")["status"] == "matched", "the other row agrees"
    state = current_state(run, [])
    assert state["targets"][row["id"]]["group"] == "open" and state["unresolved"] == 1


def test_a_disagreement_on_the_event_type_goes_to_judgment_needed(runs_dir):
    events = fake_events()
    other = copy.deepcopy(events)
    other[0]["event_type"] = "trial_results"
    _, rid = start(runs_dir, FakeReader(), FakeSecond(answer=events_answer(other)))
    run = run_of(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("disagreement"))
    assert row["disagreement"] == {"field": "event_type", "first_reader": "milestone",
                                   "second_reader": "trial_results"}
    assert (row["status"], row["group"]) == ("undecided", "open")


def test_a_disagreement_on_the_program_or_indication_is_a_disagreement_too(runs_dir):
    events = fake_events()
    other = copy.deepcopy(events)
    other[0]["indication"] = "Obesity"
    _, rid = start(runs_dir, FakeReader(), FakeSecond(answer=events_answer(other)))
    row = next(r for r in run_of(runs_dir, rid)["rows"] if r.get("disagreement"))
    assert row["disagreement"] == {"field": "indication", "first_reader": None, "second_reader": "Obesity"}


def test_the_second_readers_catch_is_counted_when_a_person_takes_its_value(runs_dir):
    """The first reader gave a date its quote does not hold; the second gave the printed one. The person's fix to
    the second reader's value passes its re-check where the old value failed: the catch is the second reader's."""
    events = fake_events()
    wrong = copy.deepcopy(events)
    for e in wrong:
        if e["date_text"] == "Q1 2027":
            e["date"] = "2027-02-01"                      # first reader: a day that is not the Q1 period's first
    client, rid = start(runs_dir, FakeReader(answer=lambda p, n: json.dumps({"events": wrong})),
                        FakeSecond(answer=events_answer(events)))
    run = run_of(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("disagreement"))
    assert row["status"] == "failed" and row["disagreement"]["field"] == "date"
    assert row["disagreement"] == {"field": "date", "first_reader": "2027-02-01", "second_reader": "2027-01-01"}
    state0 = client.get(f"/api/runs/{rid}").json()["state"]
    assert state0["mistakes"] == {"code": 0, "second_reader": 0, "person": 0}
    r = client.post(f"/api/runs/{rid}/decisions", json={"target": row["id"], "action": "fix", "note": None, "fix": {
        "field": "date", "from": "2027-02-01", "to": "2027-01-01"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"]["recheck"]["before"]["result"] == "fail" and body["state"]["recheck"]["result"] == "pass"
    assert body["mistakes"] == {"code": 0, "second_reader": 1, "person": 0}
    after = client.get(f"/api/runs/{rid}").json()["state"]
    assert [(m["caught_by"], m["target"]) for m in after["mistake_list"]] == [("second_reader", row["id"])]
    # a person's fix to some other value stays the person's
    undo = client.post(f"/api/runs/{rid}/decisions", json={"target": row["id"], "action": "undo", "note": None,
                                                           "fix": None})
    assert undo.json()["mistakes"] == {"code": 0, "second_reader": 0, "person": 0}


def test_the_second_readers_catch_counts_on_every_field_the_readers_differed_on(runs_dir):
    events = fake_events()
    wrong = copy.deepcopy(events)
    for e in wrong:
        if e["date_text"] == "Q1 2027":
            e["date"], e["date_end"] = "2027-04-01", "2027-06-30"           # both ends wrong in the first reading
    client, rid = start(runs_dir, FakeReader(answer=lambda p, n: json.dumps({"events": wrong})),
                        FakeSecond(answer=events_answer(events)))
    row = next(r for r in run_of(runs_dir, rid)["rows"] if r.get("disagreement"))
    assert [d["field"] for d in [row["disagreement"], *row["disagreement"]["also"]]] == ["date", "date_end"]

    def fix(field, frm, to):
        return client.post(f"/api/runs/{rid}/decisions", json={"target": row["id"], "action": "fix", "note": None,
                                                              "fix": {"field": field, "from": frm, "to": to}}).json()

    first = fix("date", "2027-04-01", "2027-01-01")        # date alone: the period still ends in June
    assert first["state"]["recheck"]["result"] == "fail" and first["mistakes"]["second_reader"] == 0
    second = fix("date_end", "2027-06-30", "2027-03-31")
    assert second["state"]["recheck"]["result"] == "pass" and second["state"]["status"] == "matched"
    assert second["mistakes"] == {"code": 0, "second_reader": 1, "person": 0}


def test_a_fix_to_a_value_neither_reader_gave_is_the_persons_catch(runs_dir):
    events = fake_events()
    first, second = copy.deepcopy(events), copy.deepcopy(events)
    for e in first:
        if e["date_text"] == "Q1 2027":
            e["date"] = "2027-02-01"
    for e in second:
        if e["date_text"] == "Q1 2027":
            e["date"] = "2027-03-01"
    client, rid = start(runs_dir, FakeReader(answer=events_answer(first)), FakeSecond(answer=events_answer(second)))
    row = next(r for r in run_of(runs_dir, rid)["rows"] if r.get("disagreement"))
    assert row["disagreement"] == {"field": "date", "first_reader": "2027-02-01", "second_reader": "2027-03-01"}
    body = client.post(f"/api/runs/{rid}/decisions", json={"target": row["id"], "action": "fix", "note": None, "fix": {
        "field": "date", "from": "2027-02-01", "to": "2027-01-01"}}).json()
    assert body["state"]["recheck"]["result"] == "pass" and body["state"]["status"] == "matched"
    assert body["mistakes"] == {"code": 0, "second_reader": 0, "person": 1}


def test_an_event_only_the_second_reader_found_is_an_open_row_with_its_own_checks(runs_dir):
    words = json.loads((FIXTURES / f"{PIN}.words.json").read_text())["words"]
    extra_sentence = next(s for s in coverage.sentences(words) if s["text"].startswith("Discovery"))
    extra = {"ticker": "PTGX", "company": "Protagonist Therapeutics, Inc.", "program": None, "indication": None,
             "event_type": "milestone", "title": "Discovery programs", "date_text": "2027", "date": "2027-01-01",
             "date_end": "2027-12-31", "quote": extra_sentence["text"], "word_ids": extra_sentence["word_ids"],
             "box": extra_sentence["box"]}
    _, rid = start(runs_dir, FakeReader(), FakeSecond(answer=events_answer(fake_events() + [extra])))
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    only = [r for r in run["rows"] if r.get("found_by") == "second_reader"]
    assert len(only) == 1
    row = only[0]
    assert (row["group"], row["proposed_by"]) == ("open", "Muse Spark Open")
    assert row["status"] in ("undecided", "failed") and row["reason"].endswith("Proposed by the second reader only: "
                                                                           "the first reader did not read it. Look at it: "
                                                                           "keep it as an event, fix it, or remove it.")
    assert [c["name"] for c in row["checks"]] == ["quote_match", "selector_match", "date_match", "source_rule",
                                                  "type_match"]
    assert [(t["step"], t["tag"], t["who"]) for t in row["trail"][:2]] == [
        ("Instructions sent", "Code", "capture.second_reader"), ("Proposed", "Model", "Muse Spark Open")]
    assert row["trail"][-1]["step"] == "Compared"
    state = current_state(run, [])
    assert state["targets"][row["id"]]["group"] == "open"


def test_a_sentence_only_the_second_reader_turned_into_a_row_replaces_the_not_read_row(runs_dir, monkeypatch):
    monkeypatch.setattr(jobs, "today", lambda: __import__("datetime").date(2026, 10, 29))
    events = fake_events()
    client, rid = start(runs_dir, FakeReader(answer=events_answer(events[:1])), FakeSecond())
    run = run_of(runs_dir, rid)
    assert [r.get("found_by") for r in run["rows"]] == [None, "second_reader"]
    assert not any(r.get("found_by") == "coverage" for r in run["rows"]), "the open 'not read' row is not left beside it"
    plain = run_of(runs_dir, start(runs_dir, FakeReader(answer=events_answer(events[:1])), None)[1])
    assert [r.get("found_by") for r in plain["rows"]] == [None, "coverage"], "without a second reader it stays"


def test_an_unreadable_second_answer_compares_nothing_and_says_so(runs_dir):
    first, second = FakeReader(), FakeSecond(answer=lambda p, n: "I could not read it.")
    _, rid = start(runs_dir, first, second)
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "done"
    assert run["run"]["second_reader"]["status"] == "unreadable" and run["run"]["second_reader"]["reason"]
    assert all("disagreement" not in r and r["status"] == "matched" for r in run["rows"])
    assert len(second.prompts) == 2, "the image read, then the words as the one fallback"
    assert run["run"]["second_reader"]["input"] == "image_and_words"


def test_no_second_reader_key_means_no_second_read_and_the_run_still_finishes(runs_dir):
    _, rid = start(runs_dir, FakeReader(), FakeSecond(problem=NO_KEY))
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "done" and run["run"]["turn_count"] == 1
    assert run["run"]["second_reader"] == {"model": None, "status": "not_run", "reason": NO_KEY}


def test_a_run_with_one_reader_carries_no_second_reader_field(runs_dir):
    _, rid = start(runs_dir, FakeReader(), None)
    assert "second_reader" not in run_of(runs_dir, rid)["run"]


def test_table_pages_are_read_and_compared_by_the_second_reader_too(runs_dir):
    table = json.loads(FakeReader.default("Consolidated"))
    changed = copy.deepcopy(table)
    changed["rows"][0]["cells"][0]["value"] = "999,999"
    seen = []

    def answer(prompt, n):
        seen.append(prompt)
        return json.dumps(changed) if "Protagonist" not in prompt else FakeReader.default(prompt)

    client, rid = start(runs_dir, FakeReader(), FakeSecond(answer=answer), **BOTH)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    cells = [c for t in run["tables"] for r in t["rows"] for c in r["cells"]]
    marked = [c for c in cells if c.get("disagreement")]
    assert len(marked) == 1 and marked[0]["disagreement"]["second_reader"] == "999,999"
    assert marked[0]["status"] in ("undecided", "failed") and run["run"]["turn_count"] == 4
    state = client.get(f"/api/runs/{rid}").json()["state"]
    assert state["targets"][marked[0]["id"]]["group"] in ("unverified", "open")


# ---------------------------------------------------------------- run controls cover the second read


def test_the_second_read_is_a_turn_and_resume_gives_it_a_fresh_budget(runs_dir, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 1)
    first, second = FakeReader(), FakeSecond()
    client, rid = start(runs_dir, first, second)
    m = run_of(runs_dir, rid)["run"]
    assert (m["status"], m["turn_count"]) == ("paused_turn_limit", 1) and not second.prompts
    assert client.post(f"/api/runs/{rid}/resume").status_code == 202
    m = run_of(runs_dir, rid)["run"]
    assert (m["status"], m["turns_total"], m["second_reader"]["status"]) == ("done", 2, "read")
    assert len(first.prompts) == 1 and len(second.prompts) == 1, "neither page is read twice"


def test_the_spending_limit_covers_the_second_read(runs_dir):
    first, second = FakeReader(cost=0.60), FakeSecond()
    _, rid = start(runs_dir, first, second, spending_limit_usd=0.5)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and m["status_reason"].startswith("Stopped at the spending limit")
    assert len(first.prompts) == 1 and not second.prompts


def test_the_second_reads_cost_is_counted_with_its_own_price(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=0.10), FakeSecond(tokens=(1_000_000, 500_000)))
    spend = run_of(runs_dir, rid)["run"]["spend_usd"]
    open_price = prices.PRICES_PER_1M_USD["Muse Spark Open"]
    assert spend == pytest.approx(0.10 + open_price["input"] + 0.5 * open_price["output"])


def test_a_second_reader_with_no_price_stops_a_run_that_has_a_spending_limit(runs_dir):
    _, rid = start(runs_dir, FakeReader(cost=0.01), FakeSecond(model="some-unpriced-model"), spending_limit_usd=5)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and "some-unpriced-model" in m["status_reason"]


def test_openrouter_spend_is_recorded_in_job_json_and_stops_the_fallback_at_twenty_dollars(runs_dir, monkeypatch):
    second = FakeSecond(route="openrouter", tokens=(10_000_000, 10_000_000), model=OPENROUTER_MODEL)
    _, rid = start(runs_dir, FakeReader(), second, companies=["PTGX", "IONS"])
    job = job_of(runs_dir, rid)
    one_call = prices.cost_usd("Muse Spark Open", 10_000_000, 10_000_000)
    assert one_call > 0 and job["openrouter_spend_usd"] == pytest.approx(2 * one_call)
    monkeypatch.setattr(jobs, "OPENROUTER_CAP_USD", one_call * 0.5)
    second2 = FakeSecond(route="openrouter", tokens=(10_000_000, 10_000_000), model=OPENROUTER_MODEL)
    _, rid2 = start(runs_dir, FakeReader(), second2, companies=["PTGX", "IONS"])
    assert job_of(runs_dir, rid2)["openrouter_spend_usd"] == pytest.approx(2 * one_call)
    assert second2.openrouter_allowed is False, "the job switched the fallback off once the cap was reached"


# ---------------------------------------------------------------- the two-reader PTGX run, built from the saved live readings


def test_the_committed_two_reader_run_is_what_the_builder_makes(tmp_path):
    reading = json.loads(read_events.READING_PATH.read_text())
    second = json.loads((FIXTURES / f"{PIN}.events.second.json").read_text())
    run = json.loads((RUNS / "ptgx-r5" / "run.json").read_text())
    fresh = build_entry.build_entry_run(reading, tmp_path / "ptgx-r5", finished_at=run["run"]["finished_at"],
                                        second_reading=second)
    assert fresh == run
    RUN_SCHEMA.validate(run)
    assert run["run"]["label"] == "PTGX · two readers" and run["run"]["second_reader"]["status"] == "read"
    both = (reading["usage"]["prompt_tokens"] + second["usage"]["prompt_tokens"],
            reading["usage"]["completion_tokens"] + second["usage"]["completion_tokens"])
    steps = [t for r in run["rows"] for t in r["trail"] if t["tag"] == "Model"]
    assert sum(t["tokens"]["prompt"] for t in steps) == both[0] and sum(t["tokens"]["completion"] for t in steps) == both[1]
    assert run["run"]["spend_usd"] == pytest.approx(sum(t["cost_usd"] for t in steps))


def test_the_live_second_reader_agreed_with_the_first_on_both_ptgx_events():
    run = json.loads((RUNS / "ptgx-r5" / "run.json").read_text())
    assert [r["status"] for r in run["rows"]] == ["matched", "matched"]
    assert all("disagreement" not in r for r in run["rows"])
    assert all(r["trail"][-1]["text"].endswith("Agreement is not a check: the checks above decide.") for r in run["rows"])


def test_a_two_reader_build_refuses_a_second_reading_of_another_page():
    reading = json.loads(read_events.READING_PATH.read_text())
    second = json.loads((FIXTURES / f"{PIN}.events.second.json").read_text())
    with pytest.raises(ValueError):
        build_entry.build_entry_run(reading, Path("/tmp/never-built"), second_reading={**second, "words_sha256": "0" * 64})
    with pytest.raises(ValueError):
        build_entry.build_entry_run(reading, Path("/tmp/never-built"),
                                    second_reading={**second, "prompt_text": second["prompt_text"] + "x"})


# ---------------------------------------------------------------- Job 6: the OpenRouter cap fails closed (Codex P2-2, P2-3)


@pytest.fixture
def openrouter_only(monkeypatch, tmp_path):
    """The real SecondReader with only an OpenRouter key; its network call replaced. Returns the list of calls."""
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    return []


def _stub_ask(monkeypatch, calls, tokens):
    def ask(self, route, prompt, image=None):
        calls.append(route)
        return Reply(FakeReader.default(prompt), OPENROUTER_MODEL, prompt_tokens=tokens[0],
                     completion_tokens=tokens[1], extra={"route": route})

    monkeypatch.setattr(SecondReader, "_ask", ask)


def test_reaching_the_cap_skips_the_next_pages_second_read_and_the_run_still_finishes(runs_dir, monkeypatch,
                                                                                      openrouter_only):
    """Codex P2-3: with only an OpenRouter key, the page after the one that reached the cap used to fail the whole
    run with 'No second-reader key was found'. It is skipped, with the reason, and the run finishes."""
    calls = openrouter_only
    one = prices.cost_usd("Muse Spark Open", 10_000_000, 0)
    monkeypatch.setattr(jobs, "OPENROUTER_CAP_USD", one / 2)             # the first call reaches the cap
    _stub_ask(monkeypatch, calls, (10_000_000, 0))
    _, rid = start(runs_dir, FakeReader(), SecondReader(), companies=["PTGX", "IONS"])
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["status"] == "done", run["run"]["status_reason"]
    assert calls == ["openrouter"], "one fallback call, then none"
    assert job["second"]["src-ptgx-8k-2026-08-05-ex991"]["status"] == "read"
    assert job["second"]["src-ions-ars-2026"] == {"status": "not_run", "reason": second_reader.CAP_REACHED,
                                                  "model": None, "input": None}
    sr = run["run"]["second_reader"]
    assert sr["status"] == "read" and second_reader.CAP_REACHED in sr["reason"]
    assert job["openrouter_spend_usd"] == pytest.approx(one)


def test_a_fallback_call_with_no_token_counts_cannot_stay_under_the_cap_so_the_route_is_closed(runs_dir, monkeypatch,
                                                                                               openrouter_only):
    """Codex P2-2: no usage in the reply used to leave openrouter_spend_usd at 0.0 forever. The next fallback call is
    refused instead, with no user spending limit set."""
    calls = openrouter_only
    _stub_ask(monkeypatch, calls, (None, None))
    _, rid = start(runs_dir, FakeReader(), SecondReader(), companies=["PTGX", "IONS"])
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["status"] == "done" and calls == ["openrouter"]
    assert job["openrouter_unpriced_calls"] == 1 and job["openrouter_spend_usd"] == 0.0
    assert job["second"]["src-ions-ars-2026"]["reason"] == second_reader.UNPRICED
    assert run["run"]["choices"]["spending_limit_usd"] is None


def test_the_cap_is_refreshed_before_readiness_is_asked(runs_dir, monkeypatch, openrouter_only):
    seen = []
    real_ready = SecondReader.ready

    def ready(self):
        seen.append(self.openrouter_allowed)
        return real_ready(self)

    monkeypatch.setattr(SecondReader, "ready", ready)
    one = prices.cost_usd("Muse Spark Open", 10_000_000, 0)
    monkeypatch.setattr(jobs, "OPENROUTER_CAP_USD", one / 2)
    _stub_ask(monkeypatch, openrouter_only, (10_000_000, 0))
    start(runs_dir, FakeReader(), SecondReader(), companies=["PTGX", "IONS"])
    assert seen == [True, False], "allowed for the first page, closed when the second page asks"


def test_a_priced_openrouter_run_under_the_cap_is_not_stopped_by_any_of_this(runs_dir, monkeypatch, openrouter_only):
    _stub_ask(monkeypatch, openrouter_only, (1000, 500))
    _, rid = start(runs_dir, FakeReader(), SecondReader(), companies=["PTGX", "IONS"])
    job = job_of(runs_dir, rid)
    assert openrouter_only == ["openrouter", "openrouter"] and job["openrouter_unpriced_calls"] == 0
    assert run_of(runs_dir, rid)["run"]["second_reader"] == {"model": "Muse Spark Open", "status": "read",
                                                              "reason": None, "input": "image"}


def test_baseten_stays_available_after_the_openrouter_cap_is_reached(monkeypatch, tmp_path):
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    monkeypatch.setenv("BASETEN_API_KEY", "bt-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    r = SecondReader()
    r.openrouter_allowed, r.openrouter_blocked = False, second_reader.CAP_REACHED
    assert r.ready() is None and r.route() == "baseten"
