"""Red Team round 9. A failure is a finding: each assertion is the result the code should have given.

Branch base: product main 8f21d8c7bed488309934c4014e41785137e42558.
Reader tools (the first four tests): product PR #78 at ac0bf75841ffe6af045a3c28452e781b09a3b7f4.
Picture pages (the last four): product PR #80 at f8d7bf08eac20d80a52edd241d08fc03d2ea017c.
Those two PRs both edit server/jobs.py, so each test runs only on the tree that contains its module.
No model call, no network, no key. The readers are fakes.
"""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from capture.reader import Reply
from server.app import create_app
from tests.test_run_controls import MODEL, PRICE_IN, PTGX_ONLY, RUNS, run_json

try:
    from capture import reader_tools
except ImportError:
    reader_tools = None

try:
    from runs import picture
except ImportError:
    picture = None

tools = pytest.mark.skipif(reader_tools is None, reason="Reader tools are PR #78 (ac0bf75), not this tree.")
slides = pytest.mark.skipif(picture is None, reason="Picture pages are PR #80 (f8d7bf0), not this tree.")

CALL_TOKENS = 1000


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    shutil.copytree(RUNS / "sample", d / "sample")
    return d


def _events():
    from capture import read_events
    from capture.capture_source import load_entry
    return read_events.build_fake_events(read_events.load_words(), load_entry(read_events.PIN))


def _tool_call(name, **args):
    return {"id": f"c-{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _ask(*calls, prompt_tokens=CALL_TOKENS, completion_tokens=0):
    return Reply(None, MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                 tool_calls=list(calls))


def _final(events, prompt_tokens=CALL_TOKENS, completion_tokens=0):
    return Reply(json.dumps({"events": events}), MODEL, prompt_tokens=prompt_tokens,
                 completion_tokens=completion_tokens)


class _Script:
    """Plays a script of Reply objects through chat(). No network."""

    model = MODEL

    def __init__(self, *script):
        self.script = list(script)

    def ready(self):
        return None

    def call(self, prompt, image=None):
        raise AssertionError("a reader_tools read goes through chat()")

    def chat(self, messages, tools=None, json_mode=False):
        return self.script.pop(0)


class _NeverStops(_Script):
    """A model that asks for a check on every call and never hands over events."""

    def __init__(self, quote):
        super().__init__()
        self.quote = quote
        self.calls = 0

    def chat(self, messages, tools=None, json_mode=False):
        self.calls += 1
        return _ask(_tool_call("quote_on_page", quote=self.quote))


def _run(runs_dir, reader):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader, background=False))
    got = client.post("/api/runs/new", json={**PTGX_ONLY, "reader_tools": True})
    assert got.status_code == 202, got.text
    return run_json(runs_dir, got.json()["id"])


def _answered(row):
    return [t for t in row["trail"] if t["step"].startswith("Check answered")]


# ---------------------------------------------------------------- reader tools (PR #78)


@tools
def test_a_model_that_never_stops_asking_is_held_at_twelve_calls(runs_dir):
    quote = _events()[0]["quote"]
    reader = _NeverStops(quote)
    doc = _run(runs_dir, reader)
    # One page, and the model never returned events. The page cap is MAX_MODEL_CALLS priced turns,
    # not a fresh set of that many for every unreadable parse.
    assert doc["run"]["turns_total"] <= reader_tools.MAX_MODEL_CALLS


@tools
def test_a_call_with_no_token_count_does_not_erase_the_priced_calls(runs_dir):
    events = _events()
    quote = events[0]["quote"]
    reader = _Script(
        _ask(_tool_call("quote_on_page", quote=quote)),
        _ask(_tool_call("event_type_for_words", quote=quote), prompt_tokens=None, completion_tokens=None),
        _final(events),
    )
    doc = _run(runs_dir, reader)
    priced = 2 * CALL_TOKENS * PRICE_IN / 1_000_000
    assert doc["run"]["spend_usd"] == pytest.approx(priced)
    recorded = [s.get("cost_usd") for row in doc["rows"] for s in row["trail"]
                if s.get("tag") == "Model" and s.get("step") == "Proposed"]
    assert recorded and all(c is not None for c in recorded)
    assert sum(recorded) == pytest.approx(priced)


@tools
def test_a_type_the_tool_found_is_not_stored_as_a_different_matched_type(runs_dir):
    events = [dict(e) for e in _events()]
    events[0] = dict(events[0], event_type="other")
    reader = _Script(_ask(_tool_call("event_type_for_words", quote=events[0]["quote"])), _final(events))
    doc = _run(runs_dir, reader)
    row = doc["rows"][0]
    assert any("found: milestone" in t["text"] for t in _answered(row))
    assert row["event_type"] == "milestone" or row["status"] != "matched"


@tools
def test_a_check_of_another_event_is_not_the_re_read_of_the_first(runs_dir):
    events = _events()
    bad = events[0]["quote"] + " NOT PRINTED"
    reader = _Script(
        _ask(_tool_call("quote_on_page", quote=bad)),
        _ask(_tool_call("quote_on_page", quote=events[1]["quote"]),
             _tool_call("quote_on_page", quote=events[0]["quote"])),
        _final(events),
    )
    doc = _run(runs_dir, reader)
    own = next(r for r in doc["rows"] if r["quote"] == events[0]["quote"])
    other = next(r for r in doc["rows"] if r["quote"] == events[1]["quote"])
    assert not any("NOT PRINTED" in (t.get("text") or "") for t in other["trail"])
    assert any(t["text"].startswith("fail") for t in _answered(own))


# ---------------------------------------------------------------- picture pages (PR #80)


def _tall(i, height, y):
    return {"id": f"w{i}", "text": f"t{i}", "x0": 20.0 + i, "y0": y, "x1": 28.0 + i, "y1": y + height,
            "block": 0, "line": 0, "word": i}


@slides
def test_a_page_with_enough_printed_words_is_not_a_picture():
    n = picture.MIN_REAL_WORDS
    hidden_n = n * 4
    words = [_tall(i, 8.0, 200.0) for i in range(n)] + [_tall(100 + i, 0.56, 10.0) for i in range(hidden_n)]
    doc = {"page_width": 612.0, "page_height": 792.0, "words": words}
    assert len(picture.real_words(words)) >= picture.MIN_REAL_WORDS
    assert (hidden_n / len(words)) >= picture.HIDDEN_SHARE
    assert not picture.is_picture_page(doc)


@slides
def test_words_rebuilt_from_a_region_stay_inside_the_crop_that_was_read():
    from capture import sam
    from runs import scan
    found = sam.parse_boxes("<|box;x1=-5;y1=0;x2=40;y2=20;w=100;h=100|>")
    assert len(found) == 1
    box = sam.to_points(found[0], 612.0, 792.0)
    crop = scan.crop_box({"id": "r1", "concept": "title", "box": box}, 612.0, 792.0)
    words = picture.region_words("Phase 2 initiation", box, 0, 0)
    assert words and crop[0] >= 0.0 and crop[2] <= 612.0
    assert all(crop[0] <= w["x0"] and w["x1"] <= crop[2] and crop[1] <= w["y0"] and w["y1"] <= crop[3]
               for w in words)


@slides
def test_a_quote_cannot_jump_from_one_region_to_the_next():
    from checks.event_checks import quote_match
    title = {"id": "r1", "concept": "title", "box": [40.0, 100.0, 300.0, 140.0]}
    body = {"id": "r2", "concept": "text block", "box": [40.0, 500.0, 300.0, 560.0]}
    doc = picture.build_words(612.0, 792.0, 3, [
        {"region": title, "text": "Phase 2b initiation"},
        {"region": body, "text": "in psoriasis"},
    ])
    assert doc["words_from"] == picture.WORDS_FROM
    assert quote_match("initiation in", doc["words"]).result == "fail"


@slides
def test_a_picture_page_row_the_window_removes_keeps_the_source_note(tmp_path):
    from capture import read_events
    from capture.capture_source import load_entry
    from runs import sources
    from server import jobs
    from tests.conftest import FIXTURES
    from tests.test_scan import FakeSam
    from tests.test_second_reader import FakeSecond

    entry = load_entry(read_events.PIN)
    words = {"page_number": 3, "page_width": 612.0, "page_height": 792.0, "units": "pt",
             "words": [_tall(i, 0.56, 399.3) for i in range(150)]}
    words_file = tmp_path / "strip.words.json"
    words_file.write_text(json.dumps(words))
    record = {k: v for k, v in entry.items() if k != "entries"}
    record.update({"page_file": str(FIXTURES / entry["page_file"]), "words_file": str(words_file),
                   "words_sha256": sources._sha(words_file), "id": "slide-p3", "ticker": "SLD"})
    index_path = tmp_path / "idx.json"
    index_path.write_text(json.dumps({"index_version": 1, "companies": {"SLD": {"pages": [record]}}}))
    index = sources.load_index(index_path)
    text = "Phase 2b initiation in psoriasis: Q1 '27"
    boxes = {"text block": [(80, 200, 1100, 500)], "title": [(80, 100, 600, 160)]}

    def transcribe(prompt, n):
        if "Transcribe all the text" in prompt:
            return json.dumps({"text": text})
        return json.dumps({"events": []})

    class First:
        model = MODEL

        def ready(self):
            return None

        def call(self, prompt, image=None):
            ev = {"ticker": "PTGX", "company": "Protagonist Therapeutics, Inc.", "program": None,
                  "event_type": "milestone", "title": "Phase 2b initiation", "date_text": "January 1, 2020",
                  "date": "2020-01-01", "date_end": None, "quote": text, "word_ids": [], "box": None}
            return Reply(json.dumps({"events": [ev]}), self.model, prompt_tokens=100, completion_tokens=10)

    runs = tmp_path / "runs"
    runs.mkdir()
    choices = jobs.validate_choices({"companies": ["SLD"], "event_types": list(read_events.EVENT_TYPES),
                                     "time_window_months": 12, "spending_limit_usd": None, "note_for_agent": None},
                                    index)
    jobs.create_run(runs, choices)
    jobs.run_job(runs, "run-1", First(), FakeSecond(answer=transcribe), None, None, FakeSam(boxes), index)
    doc = json.loads((runs / "run-1" / "run.json").read_text())
    page = doc["sources"][0]["pages"][0]
    row = doc["rows"][0]
    assert page["kind"] == "picture" and page["note"] == picture.SOURCE_NOTE
    assert row["group"] == "removed"
    assert picture.SOURCE_NOTE in row["reason"]
