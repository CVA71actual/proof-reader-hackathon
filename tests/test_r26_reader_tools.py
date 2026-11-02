"""Job 26: the first reader calls the checks as tools while it reads. Every reader here is a fake: no network."""

import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import read_events, reader_tools
from capture.capture_source import load_entry
from capture.reader import Reply
from server import jobs
from server.app import create_app
from tests.conftest import REPO
from tests.test_run_controls import MODEL, PRICE_IN, PTGX_ONLY, run_json, runs_dir  # noqa: F401 (fixture)

SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))
EVENTS = read_events.build_fake_events(read_events.load_words(), load_entry(read_events.PIN))
FINAL = json.dumps({"events": EVENTS})
CALL_TOKENS = 1000


def tool_call(name, **args):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def ask(*calls):
    return Reply(None, MODEL, prompt_tokens=CALL_TOKENS, completion_tokens=0, tool_calls=list(calls))


def final():
    return Reply(FINAL, MODEL, prompt_tokens=CALL_TOKENS, completion_tokens=0)


def request(*calls):
    return Reply(json.dumps({"tool_calls": [{"name": n, "arguments": a} for n, a in calls]}), MODEL,
                 prompt_tokens=CALL_TOKENS, completion_tokens=0)


class ToolReader:
    """Plays a script: each step is a Reply or a function(messages, tools) -> Reply. Records what it was sent."""

    model = MODEL

    def __init__(self, *script):
        self.script, self.sent = list(script), []

    def ready(self):
        return None

    def call(self, prompt, image=None):
        raise AssertionError("a reader_tools read goes through chat()")

    def chat(self, messages, tools=None, json_mode=False):
        self.sent.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "json_mode": json_mode})
        step = self.script.pop(0)
        return step(messages, tools) if callable(step) else step


def run(runs_dir, reader, **choices):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader, background=False))
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "reader_tools": True, **choices})
    assert r.status_code == 202, r.text
    doc = run_json(runs_dir, r.json()["id"])
    SCHEMA.validate(doc)
    return doc


def steps(row, prefix):
    return [t for t in row["trail"] if t["step"].startswith(prefix)]


def test_the_choice_defaults_to_false_and_is_recorded():
    assert jobs.validate_choices(PTGX_ONLY)["reader_tools"] is False
    assert jobs.validate_choices({**PTGX_ONLY, "reader_tools": True})["reader_tools"] is True
    with pytest.raises(ValueError):
        jobs.validate_choices({**PTGX_ONLY, "reader_tools": "yes"})


def test_a_tool_call_that_passes(runs_dir):
    reader = ToolReader(ask(tool_call("quote_on_page", quote=EVENTS[0]["quote"])), final())
    doc = run(runs_dir, reader)
    m = doc["run"]
    assert m["choices"]["reader_tools"] is True
    assert m["reader_tools"] == {"path": "native", "model_calls": 2, "tool_calls": 1, "tool_calls_run": 1,
                                 "rereads": 0, "failed_answers": 0}
    assert m["turns_total"] == 2 and m["spend_usd"] == pytest.approx(2 * CALL_TOKENS * PRICE_IN / 1_000_000)
    assert reader.sent[0]["tools"] == reader_tools.TOOLS and "quote_on_page" in reader.sent[0]["messages"][0]["content"]
    # the model got the tool's answer in the OpenAI "tool" message
    answer = json.loads(reader.sent[1]["messages"][-1]["content"])
    assert reader.sent[1]["messages"][-1]["role"] == "tool"
    assert answer["result"] == "pass" and answer["found"] == EVENTS[0]["quote"] and set(answer) == {"result", "reason", "found"}
    row = doc["rows"][0]
    called, answered = steps(row, "Called the check")[0], steps(row, "Check answered")[0]
    assert (called["step"], called["tag"], called["who"]) == ("Called the check: Quote found on page", "Model", "Muse Spark")
    assert answered["step"] == "Check answered: The quote is printed on the page, word for word."
    assert (answered["tag"], answered["who"]) == ("Code", "checks.event_checks.quote_match")
    assert not steps(doc["rows"][1], "Called the check")        # only the row it was about
    # the code's own checks still ran on every row, and the hand-over is as before
    assert all(steps(r, "Checked") for r in doc["rows"]) and {r["status"] for r in doc["rows"]} == {"matched"}


def test_a_fail_triggers_one_reread_and_the_checks_still_run(runs_dir):
    bad = EVENTS[0]["quote"] + " and more words that are not printed"
    corrected = dict(EVENTS[0])
    reader = ToolReader(
        ask(tool_call("quote_on_page", quote=bad)),
        ask(tool_call("quote_on_page", quote=EVENTS[0]["quote"])),       # the re-read: a corrected quote
        final())
    doc = run(runs_dir, reader)
    first_answer = json.loads(reader.sent[1]["messages"][-1]["content"])
    assert first_answer["result"] == "fail" and first_answer["note"] == reader_tools.reread_note(first_answer["reason"])
    assert doc["run"]["reader_tools"]["rereads"] == 1 and doc["run"]["reader_tools"]["failed_answers"] == 1
    # the corrected quote's call belongs to the same row: the row's trail shows both calls
    assert len(steps(doc["rows"][0], "Called the check")) == 2
    assert [t["text"].split(";")[0] for t in steps(doc["rows"][0], "Check answered")] == ["fail", "pass"]
    assert corrected["quote"] == doc["rows"][0]["quote"] and doc["rows"][0]["status"] == "matched"


BAD = "not on the page"


def box_():
    return reader_tools.ToolBox(read_events.load_words()["words"], "2026-08-05")


def test_two_failing_checks_in_one_batch_say_the_same_thing_and_count_no_reread():
    box = box_()
    box.begin_turn()
    a1, h1 = box.call("quote_on_page", {"quote": BAD})
    a2, h2 = box.call("quote_on_page", {"quote": BAD})
    assert h1 == h2 == reader_tools.reread_note(a1["reason"])
    assert h1.startswith("A check failed: ") and h1.endswith("Re-read this row once, then hand over.")
    assert "one re-read" not in h2 and box.reread_count == 0 and box.summary()["rereads"] == 0


def test_a_model_that_returns_without_retrying_counts_no_reread(runs_dir):
    reader = ToolReader(ask(tool_call("quote_on_page", quote=BAD)), final())
    doc = run(runs_dir, reader)
    assert doc["run"]["reader_tools"]["rereads"] == 0 and doc["run"]["reader_tools"]["failed_answers"] == 1


def test_a_changed_proposal_in_the_hand_over_counts_as_the_reread():
    box = box_()
    box.begin_turn()
    box.call("quote_on_page", {"quote": BAD + " extra"})
    box.note_final([BAD, "an unrelated row"])
    assert box.reread_count == 1


def test_a_model_that_retries_counts_one_reread_and_a_second_fail_says_so():
    box = box_()
    box.begin_turn()
    box.call("quote_on_page", {"quote": BAD})
    box.begin_turn()
    a, hint = box.call("quote_on_page", {"quote": BAD})        # the re-read attempt, failing again
    assert box.reread_count == 1 and hint == reader_tools.reread_note(a["reason"], used=True)
    assert "had its one re-read" in hint


def test_a_third_attempt_is_refused():
    box = box_()
    for _ in range(2):
        box.begin_turn()
        box.call("quote_on_page", {"quote": BAD})
    box.begin_turn()
    a, hint = box.call("quote_on_page", {"quote": BAD})
    assert a["reason"] == reader_tools.REREAD_USED and hint is None
    assert box.reread_count == 1 and box.summary()["tool_calls_run"] == 2


def test_the_cap_of_three_calls_per_event_and_thirty_per_page(runs_dir):
    q = EVENTS[0]["quote"]
    box = reader_tools.ToolBox(read_events.load_words()["words"], "2026-08-05")
    got = [box.call("quote_on_page", {"quote": q})[0] for _ in range(4)]
    assert [g["result"] for g in got] == ["pass", "pass", "pass", "could_not_run"]
    assert got[3]["reason"] == reader_tools.OVER_EVENT and box.summary()["tool_calls"] == 4
    assert box.summary()["tool_calls_run"] == 3
    page = reader_tools.ToolBox(read_events.load_words()["words"], "2026-08-05")
    # PIVOT: exercise the real mechanical tool, not the retired type oracle.
    # Distinct synthetic quotes keep the per-event cap separate from the page cap.
    last = [page.call("date_as_written", {"quote": f"Item {i}: Q1 2027", "date_text": "Q1 2027",
                 "date": "2027-01-01", "date_end": "2027-03-31"})[0] for i in range(31)]
    assert [a["result"] for a in last[:30]] == ["pass"] * 30 and last[30]["reason"] == reader_tools.OVER_PAGE
    assert page.summary()["tool_calls_run"] == 30
    # through the job: the fourth ask about one event is answered with the refusal, not run
    reader = ToolReader(ask(*[tool_call("quote_on_page", quote=q) for _ in range(4)]), final())
    doc = run(runs_dir, reader)
    contents = [json.loads(m["content"]) for m in reader.sent[1]["messages"] if m["role"] == "tool"]
    assert [c["reason"] for c in contents][-1] == reader_tools.OVER_EVENT
    assert doc["run"]["reader_tools"]["tool_calls"] == 4 and doc["run"]["reader_tools"]["tool_calls_run"] == 3


def test_date_tool_uses_literal_check_and_retired_type_tool_cannot_accept_meaning(runs_dir):
    e = EVENTS[1]
    args = dict(date_text=e["date_text"], date=e["date"], date_end=e["date_end"], quote=e["quote"],
                event_type=e["event_type"], program=e["program"])
    reader = ToolReader(ask(tool_call("date_as_written", **args), tool_call("event_type_for_words", quote=e["quote"])),
                        final())
    doc = run(runs_dir, reader)
    answers = [json.loads(m["content"]) for m in reader.sent[1]["messages"] if m["role"] == "tool"]
    # PIVOT / DATE-LITERAL-001: source-date comparison survives; type inference does not.
    assert answers[0]["result"] == "pass" and "date bounds only" in answers[0]["reason"]
    assert answers[1]["result"] == "could_not_run" and answers[1]["found"] is None
    assert "does not infer event meaning" in answers[1]["reason"]
    assert "event_type_for_words" not in {t["function"]["name"] for t in reader_tools.TOOLS}
    names = [t["step"] for t in doc["rows"][1]["trail"] if t["step"].startswith("Called")]
    assert names == ["Called the check: Date period found in quote", "Called the check: Event meaning needs review"]
    refusal = next(t for t in doc["rows"][1]["trail"]
                   if t["step"].startswith("Check answered: Code does not infer event meaning"))
    assert refusal["tag"] == "Code" and refusal["who"] == "capture.reader_tools"
    from runs.state import current_state
    from runs.calendar import calendar_entries
    state = current_state(doc, [])
    assert all(not state["targets"][r["id"]]["accepted"] for r in doc["rows"])
    assert calendar_entries(doc, state) == []


def test_the_json_request_path_when_the_tools_parameter_is_rejected(runs_dir):
    rejected = Reply(None, MODEL, error="The model call did not succeed (BadRequestError, status 400).",
                     extra={"tools_rejected": True})
    q = EVENTS[0]["quote"]
    reader = ToolReader(rejected, request(("quote_on_page", {"quote": q})), final())
    doc = run(runs_dir, reader)
    assert doc["run"]["status"] == "done"
    assert doc["run"]["reader_tools"]["path"] == "json_request" and doc["run"]["reader_tools"]["tool_calls"] == 1
    assert reader.sent[0]["tools"] and reader.sent[1]["tools"] is None and reader.sent[1]["json_mode"] is True
    assert '"tool_calls"' in reader.sent[1]["messages"][0]["content"]
    answers = json.loads(reader.sent[2]["messages"][-1]["content"])["tool_answers"]
    assert answers[0]["name"] == "quote_on_page" and answers[0]["result"] == "pass"
    assert steps(doc["rows"][0], "Called the check") and steps(doc["rows"][0], "Check answered")
    assert doc["run"]["turns_total"] == 3          # the rejected call, the request, the final answer: each one a turn


def test_a_model_that_never_calls_a_tool_reads_as_usual(runs_dir):
    doc = run(runs_dir, ToolReader(final()))
    assert doc["run"]["reader_tools"]["tool_calls"] == 0 and doc["run"]["turns_total"] == 1
    assert doc["rows"] and not steps(doc["rows"][0], "Called the check")


def test_tools_off_uses_the_plain_call(runs_dir):
    from tests.test_run_controls import FakeReader
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(), background=False))
    r = client.post("/api/runs/new", json=PTGX_ONLY)
    doc = run_json(runs_dir, r.json()["id"])
    assert doc["run"]["choices"]["reader_tools"] is False and "reader_tools" not in doc["run"]


def test_the_spending_limit_stops_the_loop(runs_dir):
    reader = ToolReader(*[ask(tool_call("quote_on_page", quote=f"q{i}")) for i in range(5)], final())
    cost = CALL_TOKENS * PRICE_IN / 1_000_000
    doc = run(runs_dir, reader, spending_limit_usd=cost * 2.5)
    assert doc["run"]["status"] == "needs_input" and doc["run"]["turns_total"] == 3 and not doc["rows"]
