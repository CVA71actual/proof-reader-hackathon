"""Job 22: the trail steps code writes for each row: routing, applying, Wonder kept, plain words.

Every model here is a fake: no test reaches the network.
"""

import json
import re

from jsonschema import Draft202012Validator

from runs import build_entry, escalate
from tests.conftest import REPO
from tests.test_escalation import (FakeWonder, job_of, reread_right, right_events, run_of, runs_dir, start,  # noqa: F401
                                   words_doc, wrong_date_events)
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond, events_answer

RUN_SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))
PROMPT_STEPS = ("Instructions sent",)     # the exact instructions a model was given: they may say JSON
BANNED = ("JSON", "_", "run folder", "dated record")


def names(row, skip=()):
    return [t["step"] for t in row["trail"] if t["step"] not in skip]


def first_event_row(run):
    return next(r for r in run["rows"] if r["date_text"] == "Q1 2027")


def trail_texts(run):
    for r in run["rows"]:
        for t in r["trail"]:
            if not t["step"].startswith(PROMPT_STEPS):
                yield r["id"], t["step"], t["text"]


def assert_plain(run, where):
    bad = [(rid, step, pat) for rid, step, text in trail_texts(run) for pat in BANNED if pat in text]
    assert not bad, f"{where}: {bad[:5]}"


def test_a_first_reader_row_trail_has_one_actor_per_step(runs_dir):
    _, rid = start(runs_dir, FakeReader(), None, None)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    row = first_event_row(run)
    steps = [(t["step"], t["tag"]) for t in row["trail"]]
    assert steps[:4] == [("Sent the page to Muse Spark", "Code"), ("Instructions sent", "Code"),
                         ("Proposed", "Model"), ("Took the reading into the row", "Code")]
    assert all(s == ("Checked", "Code") for s in steps[4:] if s[0] == "Checked")
    last_taken = max(i for i, s in enumerate(steps) if s[0] == "Took the reading into the row")
    assert all(s[0] != "Checked" for s in steps[:last_taken]), "the checks come after the row is built"
    sent = row["trail"][0]
    assert sent["who"] == "capture.read_events" and row["trail"][3]["who"] == build_entry.BUILDER
    assert_plain(run, "first reader")


def test_a_second_reader_row_trail_has_the_same_shape(runs_dir):
    events = right_events()
    other = [{**e, "event_type": "regulatory_decision" if e["date_text"] == "Q1 2027" else e["event_type"]}
             for e in events]
    _, rid = start(runs_dir, FakeReader(), FakeSecond(answer=events_answer(other)), None)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    row = first_event_row(run)
    second = [t for t in row["trail"] if t["who"] in ("capture.second_reader", "runs.compare", "Muse Spark Open")]
    assert [(t["step"], t["tag"]) for t in second] == [
        ("Sent the page image to Muse Spark Open", "Code"),
        ("Instructions sent to the second reader", "Code"),
        ("Second reading", "Model"),
        ("Compared the two readings", "Code")]
    compared = second[-1]["text"]
    assert "readers differ on 1 field" in compared and "type hint" in compared
    assert row["disagreement"]["field"] == "type_hint"
    assert row["type_hint"] == "milestone"
    assert row["second_reader_proposal"]["proposal"]["event_type"] == "regulatory_decision"
    from runs.state import current_state
    assert current_state(run, [])["targets"][row["id"]]["needs_attention"]
    assert_plain(run, "second reader")


def test_a_second_reader_only_row_has_the_routing_and_applying_steps(runs_dir):
    _, rid = start(runs_dir, FakeReader(answer=lambda p, n: json.dumps({"events": right_events()[:1]})),
                   FakeSecond(answer=events_answer([*right_events()[:1], *right_events()[1:2]])), None)
    run = run_of(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("found_by") == "second_reader")
    assert names(row)[:4] == ["Sent the page image to Muse Spark Open", "Instructions sent to the second reader",
                              "Second reading", "Took the reading into the row"]
    assert names(row)[-1] == "Compared the two readings" and "Checked" in names(row)
    assert_plain(run, "second reader only")


def test_a_row_sent_to_wonder_and_not_replaced_carries_the_wonder_steps(runs_dir, words_doc):
    still_wrong = FakeWonder(answer=lambda prompt, image, n: [{**first_event(), "date": "2027-02-01",
                                                               "box": [10, 10, 900, 60]}])
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    _, rid = start(runs_dir, first, None, still_wrong)
    run = run_of(runs_dir, rid)
    row = first_event_row(run)
    assert row["group"] == "open" and run["mistakes"] == []
    tail = [(t["step"], t["tag"], t["who"]) for t in row["trail"] if t["step"] != "Checked"
            and t["who"] in (escalate.WHO, escalate.WONDER_WHO, escalate.WONDER)]
    assert tail == [("Sent to Muse Wonder", "Code", "runs.escalate"),
                    ("Instructions sent to Muse Wonder", "Code", "capture.wonder"),
                    ("Muse Wonder", "Model", "Muse Wonder"),
                    ("Escalation result", "Code", "runs.escalate")]
    sent = next(t for t in row["trail"] if t["step"] == "Sent to Muse Wonder")
    assert sent["text"].startswith("Date period found in quote failed.")
    model = next(t for t in row["trail"] if t["step"] == "Muse Wonder")
    assert model["tokens"]["prompt"] == 4000 and model["cost_usd"] > 0
    result = next(t for t in row["trail"] if t["step"] == "Escalation result")
    assert "changes no requested date value" in result["text"]
    assert "row stays as it was" in result["text"] and "No mistake is counted" in result["text"]
    assert row["date"] == "2027-02-01"
    assert row["escalation_proposal"]["proposal"]["event_type"] == first_event()["event_type"]
    assert json.loads(row["escalation_proposal"]["raw_response"])["events"][0]["box"] == [10, 10, 900, 60]
    assert_plain(run, "wonder kept")


def first_event():
    return next(e for e in right_events() if e["date_text"] == "Q1 2027")


def test_a_wonder_call_whose_cost_passed_the_limit_still_shows_its_model_step(runs_dir, words_doc):
    first = FakeReader(cost=0.01, answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    wonder = FakeWonder(tokens=(250_000, 0), answer=reread_right(words_doc))
    _, rid = start(runs_dir, first, None, wonder, spending_limit_usd=2)
    row = first_event_row(run_of(runs_dir, rid))
    assert [t["step"] for t in row["trail"] if t["who"] in ("runs.escalate", "capture.wonder", "Muse Wonder")] == [
        "Sent to Muse Wonder", "Instructions sent to Muse Wonder", "Muse Wonder", "Escalation result"]
    result = next(t for t in row["trail"] if t["step"] == "Escalation result")
    assert "spending limit" in result["text"] and result["text"].endswith("The row stays as it was.")


def test_a_period_is_shown_as_the_source_wrote_it():
    row = {"date": "2027-01-01", "date_end": "2027-03-31", "date_text": "Q1 2027"}
    assert build_entry.when(row) == "Q1 2027"
    assert build_entry.when({**row, "date_text": None}) == "2027-01-01 to 2027-03-31"
    assert build_entry.plain_type("regulatory_decision") == "regulatory decision"
    assert build_entry.plain_field("event_type") == "event type"


def test_the_proposed_text_names_the_period_and_event_type_in_words(runs_dir):
    _, rid = start(runs_dir, FakeReader(), None, None)
    row = first_event_row(run_of(runs_dir, rid))
    text = next(t for t in row["trail"] if t["step"] == "Proposed")["text"]
    assert "2027-01-01 to 2027-03-31" not in text and "Q1 2027" in text and "_" not in text


def test_no_trail_text_in_the_committed_sample_has_json_underscores_or_run_folder():
    run = json.loads((REPO / "runs" / "sample" / "run.json").read_text())
    assert_plain(run, "runs/sample")


def test_the_committed_real_run_has_no_json_or_run_folder_wording():
    """runs/twelve-4 is a committed real run and is not rebuilt, so only the wording that was already plain is held:
    no JSON and no run folder. (Its event types were written with underscores before this job.)"""
    run = json.loads((REPO / "runs" / "twelve-4" / "run.json").read_text())
    bad = [(rid, step) for rid, step, text in trail_texts(run) for pat in ("JSON", "run folder", "dated record")
           if pat in text]
    assert not bad, bad[:5]
