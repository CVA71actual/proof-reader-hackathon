"""Request provenance (CVA 2026-11-03, after run 9): the visible brief is the whole research policy and the leading
instruction block; code text is tool mechanics and output format only; the complete effective request is inspectable
before a run and frozen, versioned, at admission; a legacy run says unavailable. No network, no paid call."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from capture import research_instructions as ri
from capture import research_sources as rs
from runs import research, research_contract as rc, research_request as rq
from server import jobs
from server.app import create_app
from tests import research_fakes as F
from tests.test_research_s1 import PAGE1, PR1, happy_script, run_json, start

BODY = {"mode": "research", "companies": ["PTGX"], "as_of": "2026-11-02"}
FINISH = [F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "f")])]

# the substantive rules run 9's controller carried; each now lives only in the brief
MOVED = ["publication dateline", "exact printed day", "Quote whole sentences", "date_text", "Prefer primary sources",
         "brave_search first", "Work like an analyst", "follow leads", "upcoming", "primary origin",
         "no year", "derives the bounds", "'by year-end'"]


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _app(runs_dir, reader, tools=None):
    tools = tools or F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1)})
    return TestClient(create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None,
                                 wonder_reader=None, sam_reader=None, research_tools=tools, research_reader=reader))


def _turns(runs_dir, rid):
    return [json.loads(p.read_text()) for p in sorted((runs_dir / rid / "research" / "turns").glob("*.json"))]


def _research_stage(stages):
    return next(s for s in stages if s["stage"] == "research")


# ---------------------------------------------------------------- one source of research policy


def test_no_research_policy_outside_the_brief():
    for phrase in MOVED:
        assert phrase not in rc.CONTROLLER, phrase
    brief = ri.RESEARCH_DEFAULT
    # brief v7 (CVA 2026-11-04, run 16): CVA's structured brief plus one event per conference and replaced guidance
    for phrase in ("A publication date is not the event date", "Exact date only for a printed day",
                   "Prefer primary sources", "Use brave_search first", "at its origin", "An approval is not a launch",
                   "An event that already happened is not an event to report", "monitoring item",
                   "One event in several sources: one event citing all", "Each conference or meeting is its own event",
                   "cite the earlier as replaced", "record_coverage", "finish_draft.limitations"):
        assert phrase in brief, phrase
    assert "company card" not in brief and "PTGX" not in brief
    assert len(brief) <= ri.MAX_LENGTH
    card = rq.company_card("PTGX", __import__("datetime").date(2026, 11, 3), "P", "1", True)
    assert set(card) == {"ticker", "company", "cik", "as_of", "calendar_window", "search_tools"}     # facts only
    assert card["calendar_window"] == ["2026-11-03", "2027-05-03"]


def test_the_brief_is_the_first_instruction_block_the_researcher_receives(runs_dir):
    reader = F.FakeResponses(happy_script())
    start(runs_dir, reader)
    assert len(reader.requests) >= 2
    for body in reader.requests:
        assert body["instructions"].startswith(ri.RESEARCH_DEFAULT)
        assert body["instructions"] == ri.RESEARCH_DEFAULT + "\n\n" + rc.CONTROLLER
        assert body["input"][0]["content"].startswith("Company card:\n")
        assert all(ri.RESEARCH_DEFAULT not in (i.get("content") or "") for i in body["input"]
                   if isinstance(i.get("content"), str))      # never a second copy


# ---------------------------------------------------------------- preview == admission == what was sent


def test_preview_matches_what_admission_saves(runs_dir):
    note = "Focus on rusfertide and its partner trials."
    reader = F.FakeResponses(FINISH)
    client = _app(runs_dir, reader)
    pre = client.post("/api/research/effective-request", json={**BODY, "note_for_agent": note})
    assert pre.status_code == 200, pre.text
    got = client.get("/api/research/effective-request", params={"tickers": "PTGX", "note": note,
                                                                 "as_of": "2026-11-02"}).json()
    assert got["stages"] == pre.json()["stages"]
    rid = client.post("/api/runs/new", json={**BODY, "note_for_agent": note}).json()["id"]
    rec = client.get(f"/api/runs/{rid}/request-record").json()
    assert rec["status"] == "recorded" and rec["current_version"] == 1
    v1 = rec["versions"][0]
    assert v1["reason"] == "admission" and v1["intact"] is True and v1["changed"] == []
    assert v1["stages"] == pre.json()["stages"]


def test_preview_labels_every_stage(runs_dir):
    out = _app(runs_dir, F.FakeResponses([])).get("/api/research/effective-request",
                                                  params={"tickers": "PTGX", "as_of": "2026-11-03"}).json()
    names = [s["stage"] for s in out["stages"]]
    assert names == ["research", "review", "adjudication", "support_check"]
    r = out["stages"][0]
    assert r["model"] == "Muse Spark" and r["editable"]["brief"] == ri.RESEARCH_DEFAULT
    assert r["editable"]["brief_is_default"] is True
    assert r["enforced_by_code"]["contract_text"] == rc.CONTROLLER
    assert r["enforced_by_code"]["instructions_text"].startswith(r["editable"]["brief"])
    tools = {t["name"]: t for t in r["enforced_by_code"]["tools"]}
    assert set(rc.ARGUMENTS) | {"web_search"} == set(tools)
    assert tools["propose_events"]["parameters"] == rc.ARGUMENTS["propose_events"]
    assert r["run_context"]["reference_date"] == "2026-11-03"
    assert r["run_context"]["window"] == {"start": "2026-11-03", "end": "2027-05-03", "months": 6}
    assert r["run_context"]["company_cards"][0]["ticker"] == "PTGX"
    review = out["stages"][1]
    assert [m["name"] for m in review["models"]][:2] == ["Muse Spark Open", "DeepSeek V4 Pro"]
    assert review["editable"]["brief"] == ri.RESEARCH_DEFAULT and review["enforced_by_code"]["instructions_text"]
    assert out["stages"][2]["model"] == "Muse Wonder"
    sup = out["stages"][3]
    assert sup["engine"] == "LettuceDetect" and sup["enabled"] is False and sup["status_if_off"]
    bad = _app(runs_dir, F.FakeResponses([])).get("/api/research/effective-request", params={"tickers": ""})
    assert bad.status_code == 422


def test_saved_outbound_request_holds_the_brief_verbatim_and_the_contract(runs_dir):
    reader = F.FakeResponses(happy_script())
    client, rid, _ = start(runs_dir, reader)
    rec = client.get(f"/api/runs/{rid}/request-record").json()
    v1 = _research_stage(rec["versions"][0]["stages"])
    turns = _turns(runs_dir, rid)
    assert turns and all(t["request_version"] == 1 for t in turns)
    for t in turns:
        assert t["request"]["instructions"] == v1["enforced_by_code"]["instructions_text"]
        assert t["request"]["instructions"].startswith(v1["editable"]["brief"])
        assert t["request"]["instructions"].endswith(v1["enforced_by_code"]["contract_text"])
    start_rec = rec["company_starts"][0]
    assert turns[0]["request"]["input"][0]["content"] == "Company card:\n" + json.dumps(start_rec["company_card"])
    assert start_rec["company_card"]["company"] == "Protagonist Therapeutics, Inc."
    assert start_rec["company_card"]["calendar_window"] == ["2026-11-02", "2027-05-02"]
    research_uses = [u for u in rec["uses"] if u["stage"] == "research"]
    assert [u["turn"] for u in research_uses] == [t["n"] for t in turns]
    assert rec["versions"][0]["used_by"] == rec["uses"]
    assert any(u["stage"] == "review" for u in rec["uses"])
    row = run_json(runs_dir, rid)["rows"][0]
    assert row["research"]["request_version"] == 1


def test_an_edited_brief_is_what_is_saved_and_sent(runs_dir):
    note = "Only PN-881. Exact days only; drop anything undated."
    reader = F.FakeResponses(FINISH)
    client, rid, _ = start(runs_dir, reader, note_for_agent=note)
    v1 = _research_stage(client.get(f"/api/runs/{rid}/request-record").json()["versions"][0]["stages"])
    assert v1["editable"] == {"brief": note, "brief_is_default": False}
    assert reader.requests[0]["instructions"] == note + "\n\n" + rc.CONTROLLER
    assert _turns(runs_dir, rid)[0]["request"]["instructions"] == v1["enforced_by_code"]["instructions_text"]


def test_changing_defaults_later_leaves_an_old_run_showing_its_original(runs_dir, monkeypatch):
    reader = F.FakeResponses(FINISH)
    client, rid, _ = start(runs_dir, reader)
    before = client.get(f"/api/runs/{rid}/request-record").json()
    monkeypatch.setattr(ri, "RESEARCH_DEFAULT", "A different default brief.")
    monkeypatch.setattr(rc, "CONTROLLER", rc.CONTROLLER + "\n- a later contract line")
    after = client.get(f"/api/runs/{rid}/request-record").json()
    assert after == before
    v1 = _research_stage(after["versions"][0]["stages"])
    assert "A different default brief." not in json.dumps(after) and "later contract line" not in json.dumps(after)
    assert v1["editable"]["brief"] == _turns(runs_dir, rid)[0]["request"]["instructions"].split("\n\nTechnical")[0]


def test_a_legacy_run_says_unavailable_and_labels_saved_turn_files(runs_dir):
    reader = F.FakeResponses(FINISH)
    client, rid, _ = start(runs_dir, reader)
    (runs_dir / rid / rq.RECORD_FILE).unlink()          # as a run admitted before request records existed
    rec = client.get(f"/api/runs/{rid}/request-record").json()
    assert rec["status"] == "unavailable" and "before request records existed" in rec["reason"]
    assert "versions" not in rec
    saved = rec["from_saved_request_files"]
    assert saved["label"].startswith("From saved request files")
    assert saved["companies"][0]["instructions_text"] == _turns(runs_dir, rid)[0]["request"]["instructions"]
    page = client.get("/api/runs/sample/request-record")
    assert page.status_code in (200, 404)


def test_run_9_style_folder_without_turns_is_unavailable_without_reconstruction(tmp_path):
    d = tmp_path / "run-x"
    d.mkdir()
    run = {"run": {"id": "run-x", "choices": {"mode": "research", "companies": ["PTGX"], "note_for_agent": "x"}}}
    v = rq.request_record_view(d, run)
    assert v["status"] == "unavailable" and v["from_saved_request_files"] is None


def test_a_continuation_with_a_changed_request_is_version_2_tied_to_its_calls(runs_dir, monkeypatch):
    choices = jobs.validate_choices({"mode": "research", "companies": ["PTGX"]}, None)
    rid = jobs.create_run(runs_dir, choices, as_of="2026-11-02")["run"]["id"]
    rec = rq.load(runs_dir / rid)
    assert [v["version"] for v in rec["versions"]] == [1]
    # the code's contract changed between admission and the research start (a redeploy while queued)
    monkeypatch.setattr(rc, "CONTROLLER", rc.CONTROLLER + "\n- a changed contract line")
    reader = F.FakeResponses(FINISH)
    research.run_research(runs_dir, rid, reader, F.tools({PR1: (200, "text/html", PAGE1)}))
    rec = rq.request_record_view(runs_dir / rid, run_json(runs_dir, rid))
    assert rec["current_version"] == 2
    v1, v2 = rec["versions"]
    assert v1["used_by"] == [] and v1["reason"] == "admission"
    assert "research.enforced_by_code.contract_text" in v2["changed"] and "the job's start" in v2["reason"]
    assert v2["used_by"] and all(u["version"] == 2 for u in v2["used_by"])
    assert all(t["request_version"] == 2 for t in _turns(runs_dir, rid))
    assert reader.requests[0]["instructions"].endswith("a changed contract line")
    assert rec["company_starts"][0]["version"] == 2
    # versions are append-only: freezing again is refused
    with pytest.raises(ValueError):
        rq.freeze(runs_dir / rid, run_json(runs_dir, rid))


def test_a_tampered_version_is_shown_as_not_intact(runs_dir):
    reader = F.FakeResponses(FINISH)
    client, rid, _ = start(runs_dir, reader)
    p = runs_dir / rid / rq.RECORD_FILE
    rec = json.loads(p.read_text())
    rec["versions"][0]["stages"][0]["editable"]["brief"] = "edited after the fact"
    p.write_text(json.dumps(rec))
    assert client.get(f"/api/runs/{rid}/request-record").json()["versions"][0]["intact"] is False


def test_secrets_are_redacted_and_instructions_are_not(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "sk-test-0123456789")
    out = rq.redact({"a": "key sk-test-0123456789 here", "b": ["Work like an analyst"]})
    assert out == {"a": "key [redacted] here", "b": ["Work like an analyst"]}


# ---------------------------------------------------------------- run 9's ClinicalTrials.gov cursor


def test_an_empty_object_cursor_is_no_cursor_and_an_unissued_cursor_is_refused(runs_dir):
    first_url = rs.ctgov_search_url("rusfertide", None)
    page2_url = rs.ctgov_search_url("rusfertide", "tok-2")
    pages = {PR1: (200, "text/html", PAGE1),
             first_url: (200, "application/json", json.dumps({"studies": [], "nextPageToken": "tok-2"}).encode()),
             page2_url: (200, "application/json", json.dumps({"studies": []}).encode())}
    script = [F.body([F.call("fetch_ctgov", {"nct_id": "null", "query": "rusfertide", "cursor": "{}"}, "q1")]),
              F.body([F.call("fetch_ctgov", {"nct_id": None, "query": "rusfertide", "cursor": "made-up"}, "q2")]),
              F.body([F.call("fetch_ctgov", {"nct_id": None, "query": "rusfertide", "cursor": "tok-2"}, "q3")]),
              FINISH[0]]
    reader = F.FakeResponses(script)
    client, rid, tools = start(runs_dir, reader, pages)
    asked = [u for u in tools.fetcher.asked if "clinicaltrials" in u]
    assert asked == [first_url, page2_url] and "pageToken=%7B%7D" not in " ".join(asked)
    out1 = json.loads(reader.requests[1]["input"][-1]["output"])
    assert out1["status"] == "ok" and out1["data"]["next_cursor"] == "tok-2"
    out2 = json.loads(reader.requests[2]["input"][-1]["output"])
    assert out2["status"] == "error" and "next_cursor" in out2["issues"][0]["message"]


# ---------------------------------------------------------------- New Run's turn limit (turn_ceiling_per_company)


def test_turn_limit_default_is_200_and_served(runs_dir):
    assert research.STOP_RULE_DEFAULTS["turn_ceiling_per_company"] == 200
    client = _app(runs_dir, F.FakeResponses([]))
    assert client.get("/api/settings").json()["turn_limit_per_company_default"] == 200
    pre = client.post("/api/research/effective-request", json=BODY).json()
    assert pre["stages"][0]["run_context"]["turn_limit_per_company"] == 200
    choices = jobs.validate_choices({"mode": "research", "companies": ["PTGX"]}, None)
    assert choices["research_limits"]["turn_ceiling_per_company"] == 200


def test_turn_limit_is_recorded_and_enforced(runs_dir):
    # a model that never finishes: one search lead per turn, forever
    script = [F.body([F.call("fetch_ctgov", {"nct_id": None, "query": f"q{n}", "cursor": None}, f"q{n}")])
              for n in range(10)]
    reader = F.FakeResponses(script)
    client, rid, _ = start(runs_dir, reader, turn_limit_per_company=3, limits={"stall_turns_per_company": 0})
    meta = run_json(runs_dir, rid)["run"]
    assert meta["choices"]["research_limits"]["turn_ceiling_per_company"] == 3
    assert "turn_limit_per_company" not in meta["choices"]
    assert len(reader.requests) == 3
    c = meta["research"]["companies"]["PTGX"]
    assert c["status"] == "finished" and c["ended_by"]["code"] == "turn_ceiling"
    rec = client.get(f"/api/runs/{rid}/request-record").json()
    assert _research_stage(rec["versions"][0]["stages"])["run_context"]["turn_limit_per_company"] == 3


def test_turn_limit_120_is_recorded(runs_dir):
    client = _app(runs_dir, F.FakeResponses([]))
    pre = client.post("/api/research/effective-request", json={**BODY, "turn_limit_per_company": 120}).json()
    assert pre["choices"]["research_limits"]["turn_ceiling_per_company"] == 120
    assert pre["stages"][0]["run_context"]["turn_limit_per_company"] == 120


@pytest.mark.parametrize("bad", [0, -5, 2.5, "120", True])
def test_an_invalid_turn_limit_is_refused(runs_dir, bad):
    client = _app(runs_dir, F.FakeResponses([]))
    r = client.post("/api/runs/new", json={**BODY, "turn_limit_per_company": bad})
    assert r.status_code == 422 and "turn_limit_per_company" in r.json()["detail"]
    page = client.post("/api/runs/new", json={"companies": ["PTGX"], "turn_limit_per_company": 50})
    assert page.status_code == 422
