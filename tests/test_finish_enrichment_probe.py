"""Desired enrichment behavior: exposes the existing clean-row revision refusal."""
import copy
import json
import socket

from tests import research_fakes as F
from tests.test_research_s1 import start, run_json, PR1, PR2, PAGE1, QUOTE


def test_clean_proposal_can_gain_a_captured_second_source_without_new_row(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This probe must not use the network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    ev = F.event("ICARUS Phase 3 topline", QUOTE, date_text="Q2 2027", date="2027-04-01",
                 date_end="2027-06-30", program="ICARUS", conditions=[])
    more = copy.deepcopy(ev)
    more["evidence"].append({"source_id": "src-r2-ptgx", "page_number": 1,
                             "quote": "The ICARUS trial is a Phase 3 study.",
                             "word_ids": None, "roles": ["context"]})
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": PR1, "purpose": "initial"}, "c1")]),
        F.body([F.call("propose_events", {"events": [ev]}, "c2")]),
        F.body([F.call("fetch_url", {"url": PR2, "purpose": "support"}, "c3")]),
        F.body([F.call("revise_event", {"id": "e1", "expected_revision": 1, "event": more}, "c4")]),
        F.body([F.call("nothing_material", {"source_id": "src-r2-ptgx", "reason": "No separate event"}, "c5")]),
        F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": "e1", "disposition": "proposal", "related_row_id": None, "reason": "same event"}],
            "limitations": []}, "c6")]),
    ])
    _, rid, _ = start(runs_dir, reader, pages={PR1: (200, "text/html", PAGE1),
        PR2: (200, "text/html", F.html("The ICARUS trial is a Phase 3 study."))})
    outputs = [json.loads(item["output"]) for req in reader.requests for item in req["input"]
               if item.get("type") == "function_call_output" and item.get("call_id") == "c4"]
    assert outputs, "revision result was not delivered"
    print("REVISION RESULT:", json.dumps(outputs[-1]))
    rows = run_json(runs_dir, rid)["rows"]
    assert len(rows) == 1
    assert rows[0]["research"]["revision"] == 2, outputs[-1]
    assert len(rows[0]["research"]["evidence"]) == 2
