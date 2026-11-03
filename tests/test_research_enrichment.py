"""Codex finish decision 5c: an existing proposal gains newly captured evidence through the revision path, as an
EVIDENCE UPDATE, separately from the one error-repair allowance. Real path only: POST /api/runs/new with a scripted
Responses API and fake fetches; state and exports through the shared code. No paid call, no network.

Register rows: A-TO-Z-001 (attaching a source is its disposition), IDENTITY-001 (the material basis change expires the
Keep through state and exports), CODE-OWNS-001 (code owns the revision counter), CLAIM-001 (an extra passing citation
never clears an unrelated failure)."""

from __future__ import annotations

import copy
import json

import pytest

from report.data import build_view
from runs.state import current_state
from tests import research_fakes as F
from tests.test_research_s1 import PAGE1, PR1, PR2, QUOTE, RUN_SCHEMA, run_json, start

B_TEXT = "The ICARUS trial is a Phase 3 study."
PAGES = {PR1: (200, "text/html", PAGE1), PR2: (200, "text/html", F.html(B_TEXT))}


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _event(date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30"):
    return F.event("ICARUS Phase 3 topline", QUOTE, date_text=date_text, date=date, date_end=date_end,
                   program="ICARUS", conditions=[])


def _with_b(ev, quote=B_TEXT):
    more = copy.deepcopy(ev)
    more["evidence"].append({"source_id": "src-r2-ptgx", "page_number": 1, "quote": quote, "word_ids": None,
                             "roles": ["context"]})
    return more


def _finish(cid="fin"):
    return F.body([F.call("finish_draft", {"dispositions": [
        {"row_id": "e1", "disposition": "proposal", "related_row_id": None, "reason": "x"}], "limitations": []}, cid)])


def _run(runs_dir, revise_calls, ev=None, tail=None):
    ev = ev or _event()
    script = [F.body([F.call("fetch_url", {"url": PR1, "purpose": "initial"}, "c1")]),
              F.body([F.call("propose_events", {"events": [ev]}, "c2")]),
              F.body([F.call("fetch_url", {"url": PR2, "purpose": "support"}, "c3")])]
    script += [F.body([c]) for c in revise_calls]
    script += tail or [_finish()]
    reader = F.FakeResponses(script)
    _, rid, _ = start(runs_dir, reader, pages=PAGES)
    return reader, rid, run_json(runs_dir, rid)


def _answer(reader, call_id):
    outs = [json.loads(i["output"]) for q in reader.requests for i in q["input"]
            if i.get("type") == "function_call_output" and i.get("call_id") == call_id]
    return outs[-1]


def test_a_then_one_proposal_then_b_gives_the_same_row_at_revision_2_with_both_sources(runs_dir):
    reader, rid, run = _run(runs_dir, [F.call("revise_event", {"id": "e1", "expected_revision": 1,
                                                               "event": _with_b(_event())}, "c4")])
    RUN_SCHEMA.validate(run)
    rows = [r for r in run["rows"] if r.get("research")]
    assert len(rows) == 1
    r = rows[0]["research"]
    assert rows[0]["id"] == "e1" and r["revision"] == 2 and r["update"] == "evidence_update"
    assert [s["source_id"] for s in r["evidence"]] == ["src-r1-ptgx", "src-r2-ptgx"]
    assert r["revisions"][0]["revision"] == 1 and r["revisions"][0]["replaced_by"] == "evidence_update"
    assert r["feedback"]["repair_open"] is False and "repaired_at" not in r["feedback"]      # the repair is untouched
    assert _answer(reader, "c4")["data"]["events"][0]["status"] == "checked"
    assert any("evidence update, not a repair" in t["text"] for t in rows[0]["trail"] if t["step"] == "Revised")
    # unchanged controls remain clean; the new span is checked too
    assert r["issues"] == [] and all(s["quote_match"] == "pass" for s in r["evidence"])
    # A-TO-Z-001: attaching B is B's disposition, so the company finishes without a separate nothing_material
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "finished" and c["finish"]["unsettled_sources"] == []


def test_an_identical_replay_is_a_no_op_and_a_stale_revision_is_refused(runs_dir):
    more = _with_b(_event())
    reader, rid, run = _run(runs_dir, [
        F.call("revise_event", {"id": "e1", "expected_revision": 1, "event": more}, "c4"),
        F.call("revise_event", {"id": "e1", "expected_revision": 2, "event": more}, "c5"),      # identical replay
        F.call("revise_event", {"id": "e1", "expected_revision": 1, "event": more}, "c6")])     # stale
    r = next(x for x in run["rows"] if x.get("research"))["research"]
    assert r["revision"] == 2 and len(r["revisions"]) == 1
    assert _answer(reader, "c5")["data"]["events"][0]["status"] == "unchanged"
    stale = _answer(reader, "c6")["data"]["events"][0]
    assert stale["status"] == "refused" and stale["issues"][0]["code"] == "stale_revision"


def test_newly_failed_support_from_b_reaches_review_and_never_clears_an_unrelated_failure(runs_dir):
    # revision 1 carries a timing failure (the quote says Q2 2027, the event says Q4 2027)
    wrong = _event(date_text="Q4 2027", date="2027-10-01", date_end="2027-12-31")
    reader, rid, run = _run(runs_dir, [F.call("revise_event", {
        "id": "e1", "expected_revision": 1, "event": _with_b(wrong, quote="The ICARUS trial is a Phase 9 study.")},
        "c4")], ev=wrong, tail=[F.body([F.call("nothing_material", {"source_id": "src-r2-ptgx", "reason": "x"},
                                               "nm")]), _finish()])
    row = next(x for x in run["rows"] if x.get("research"))
    r = row["research"]
    before = {i["code"] for i in r["revisions"][0]["issues"]}
    after = {i["code"] for i in r["issues"]}
    assert r["revision"] == 2 and r["update"] == "evidence_update"
    assert before and before <= after                        # the unrelated failure is still there
    assert r["evidence"][1]["quote_match"] != "pass"          # B's failed support is recorded on the row
    assert len(after) > len(before) or any("src-r2" in json.dumps(i) for i in r["issues"])
    assert r["feedback"]["repair_open"] is True               # the one repair allowance was not spent by it
    state = current_state(run, [])["targets"]["e1"]
    assert state["needs_attention"] is True                   # it reaches review


def test_changing_the_event_is_not_an_evidence_update_once_the_repair_is_spent(runs_dir):
    moved = _with_b(_event(date_text="Q3 2027", date="2027-07-01", date_end="2027-09-30"))
    reader, rid, run = _run(runs_dir, [F.call("revise_event", {"id": "e1", "expected_revision": 1, "event": moved},
                                              "c4")],
                            tail=[F.body([F.call("nothing_material", {"source_id": "src-r2-ptgx", "reason": "x"},
                                                 "nm")]), _finish()])
    refused = _answer(reader, "c4")["data"]["events"][0]
    assert refused["status"] == "refused" and refused["issues"][0]["code"] == "repair_not_offered"
    assert "event fields differ" in refused["issues"][0]["message"]
    assert next(x for x in run["rows"] if x.get("research"))["research"]["revision"] == 1


def test_a_genuinely_different_event_from_b_stays_a_second_row(runs_dir):
    other = F.event("ICARUS Phase 3 design", B_TEXT, source="src-r2-ptgx", program="ICARUS", conditions=[],
                    type_hint="other")
    reader, rid, run = _run(runs_dir, [F.call("propose_events", {"events": [other]}, "c4")],
                            tail=[F.body([F.call("finish_draft", {"dispositions": [
                                {"row_id": r, "disposition": "proposal", "related_row_id": None, "reason": "x"}
                                for r in ("e1", "e2")], "limitations": []}, "fin")])])
    rows = [x for x in run["rows"] if x.get("research")]
    assert [x["id"] for x in rows] == ["e1", "e2"] and all(x["research"]["revision"] == 1 for x in rows)


def test_the_evidence_update_expires_an_earlier_keep_through_state_and_exports(runs_dir):
    from fastapi.testclient import TestClient
    from server.app import create_app
    reader, rid, run = _run(runs_dir, [F.call("revise_event", {"id": "e1", "expected_revision": 1,
                                                               "event": _with_b(_event())}, "c4")])
    path = runs_dir / rid / "run.json"
    rev2 = json.loads(path.read_text())
    row2 = next(x for x in rev2["rows"] if x["id"] == "e1")
    hist = row2["research"]["revisions"][0]
    row1 = {**hist["row"], "trail": row2["trail"],
            "research": {**row2["research"], "revision": 1, "evidence": hist["evidence"], "issues": hist["issues"],
                         "revisions": []}}
    rev1 = copy.deepcopy(rev2)
    rev1["rows"] = [row1 if x["id"] == "e1" else x for x in rev1["rows"]]
    path.write_text(json.dumps(rev1, indent=1, ensure_ascii=False) + "\n")
    client = TestClient(create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None,
                                   wonder_reader=None, sam_reader=None))
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": "e1", "action": "keep", "note": None,
                                                           "fix": None}).status_code == 200
    assert client.get(f"/api/runs/{rid}").json()["state"]["targets"]["e1"]["accepted"] is True
    path.write_text(json.dumps(rev2, indent=1, ensure_ascii=False) + "\n")      # the evidence update lands
    assert client.get(f"/api/runs/{rid}").json()["state"]["targets"]["e1"]["accepted"] is False
    decisions = [json.loads(l) for l in (runs_dir / rid / "decisions.jsonl").read_text().splitlines() if l.strip()]
    view = build_view(json.loads(path.read_text()), decisions, generated_at="2026-11-02T20:00:00+00:00")
    assert view["counts"]["accepted"] == 0
