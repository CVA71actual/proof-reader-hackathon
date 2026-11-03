"""STOP-RULE-001 (CVA 2026-11-03, after live run 9): the research stopping rule and the person's Finish now.

A company's research ENDS AS FINISHED (not a limitation) when it has gone stall_turns_per_company model turns with no
new proposal (default 40), reaches turn_ceiling_per_company model turns (default 250) or minutes_ceiling_per_company
minutes (default 45), or a person chooses Finish now (POST /api/runs/{id}/research-finish). The run then goes on to
the independent review, adjudication and the support check and publishes like a model finish. research-stop
(discard: nothing reviewed, nothing published) is unchanged and wins over Finish now.

Fakes only (tests/research_fakes.py): a scripted researcher, reviewer and adjudicator, a fake Brave and fetcher."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from runs import research
from server.app import create_app
from tests import research_fakes as F
from tests.test_lettuce_option import FakeDetector
from tests.test_research_review_wire import PAGE2
from tests.test_research_s1 import PAGE1, PR1, PR2, QUOTE, RUN_SCHEMA, run_json

QUERY = "Protagonist catalysts 2027"


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def reviewer():
    return F.FakeChat([F.assess_then(("search", {"query": "Protagonist EXPAND topline"})),
                       F.chat_tools(("finish_review", {"summary": "Checked the draft; nothing wrong."}))])


def start(runs_dir, script, *, limits=None, review=None, client_box=None, **choices):
    reader = F.FakeResponses(script)
    tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1), PR2: (200, "text/html; charset=utf-8", PAGE2)})
    tools.reviewer = review or reviewer()
    tools.adjudicator = F.FakeChat([], model=F.ADJUDICATOR_MODEL)
    tools.brave = F.FakeBrave(answers={QUERY: [PR1], "Protagonist EXPAND topline": [PR2]})
    tools.support_detector = FakeDetector()
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    client = TestClient(app)
    if client_box is not None:
        client_box.append(client)
    body = {"mode": "research", "companies": ["PTGX"], "as_of": "2026-11-02", "support_check": "lettucedetect",
            **choices}
    if limits is not None:
        body["research_limits"] = limits
    r = client.post("/api/runs/new", json=body)
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    return client, rid, reader, tools


def event(title="ICARUS Phase 3 topline data"):
    return F.event(title, QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30", program="ICARUS",
                   conditions=[])


def fetch_turn():
    return F.body([F.call("fetch_url", {"url": PR1, "purpose": "company release"}, "c1")], rid="r1")


def propose_turn(n, title="ICARUS Phase 3 topline data"):
    return F.body([F.call("propose_events", {"events": [event(title)]}, f"p{n}")], rid=f"rp{n}")


def search_turn(n):
    """No progress: the same Brave query, the same answer (a repeat after the first)."""
    return F.body([F.call("brave_search", {"query": QUERY, "count": 5}, f"b{n}")], rid=f"rb{n}")


def web_only_turn(n):
    """No progress: a reply with only a web search (the loop nudges and goes on)."""
    return F.body([F.search(QUERY, [PR1], sid=f"ws_{n}")], rid=f"rw{n}")


def the_run(runs_dir, rid):
    run = run_json(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    return run, run["run"], run["run"]["research"]


def assert_reviewed_and_published(meta, r, code):
    c = r["companies"]["PTGX"]
    assert c["status"] == "finished" and c["limitation"] is None, c["status_reason"]
    assert c["ended_by"]["code"] == code
    assert meta["status"] == "done", meta["status_reason"]
    assert "Partial research" not in meta["status_reason"]
    assert "PTGX: research ended — " in meta["status_reason"]
    rec = r["review"]["PTGX"]
    assert rec["status"] == "complete" and rec["clean"] is True, rec.get("reason")
    assert rec["research_ended"]["kind"] == "finished"
    assert r["support_check"]["companies"]["PTGX"]["no_finding"] >= 1      # the support check ran too
    kill = [k for k in r["kill_conditions"] if k["code"] == code]
    assert len(kill) == 1 and kill[0]["outcome"] == "finished" and kill[0]["ticker"] == "PTGX"
    assert kill[0]["detail"].startswith("PTGX: research ended — ") and kill[0]["detail"].endswith("moving to review.")
    assert any(s["kind"] == "limit" and s["text"] == kill[0]["detail"] for s in r["steps"])
    return c, kill[0]


# ------------------------------------------------------------------ defaults and settings

def test_stop_rules_are_on_by_default_settable_and_zero_is_off():
    lim = research.validate_limits(None)
    assert {k: lim[k] for k in research.STOP_RULE_DEFAULTS} == {
        "stall_turns_per_company": 40, "turn_ceiling_per_company": 250, "minutes_ceiling_per_company": 45}
    lim = research.validate_limits({"stall_turns_per_company": 10, "turn_ceiling_per_company": 0,
                                    "minutes_ceiling_per_company": 2.5})
    assert (lim["stall_turns_per_company"], lim["turn_ceiling_per_company"], lim["minutes_ceiling_per_company"]) \
        == (10, 0, 2.5)
    assert research.validate_limits(lim) == lim             # a re-validated stored setting keeps "off"
    for bad in ({"stall_turns_per_company": -1}, {"turn_ceiling_per_company": 2.5},
                {"stall_turns_per_company": True}, {"minutes_ceiling_per_company": "45"}):
        with pytest.raises(ValueError):
            research.validate_limits(bad)
    # the existing keys keep their behaviour: off (null) unless a person turns them on
    assert all(lim[k] is None for k in research.LIMIT_KEYS
               if k not in ("concurrency", "stop_on_empty_rate", *research.STOP_RULE_DEFAULTS))


# ------------------------------------------------------------------ the stall rule

def test_stall_after_default_40_turns_without_a_new_proposal_reviews_and_publishes(runs_dir):
    script = [fetch_turn(), propose_turn(2)] + [web_only_turn(n) for n in range(3, 3 + 40)]
    _, rid, reader, tools = start(runs_dir, script)
    run, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 42 and reader.script == []        # ended before turn 43: 40 turns after turn 2
    c, kill = assert_reviewed_and_published(meta, r, "stall")
    assert (kill["setting"], kill["turn"], kill["last_proposal_turn"]) == (40, 42, 2)
    assert c["status_reason"].startswith("Research ended — no new proposal in 40 turns (stall rule); moving to review.")
    assert r["limits"]["stall_turns_per_company"] == 40
    assert [row["id"] for row in run["rows"] if row.get("research")] and tools.reviewer.script == []


def test_a_new_proposal_resets_the_stall_counter(runs_dir):
    # stall 3: turns 3-4 idle, turn 5 a new proposal (resets), turns 6-8 idle -> ends before turn 9
    script = ([fetch_turn(), propose_turn(2)] + [web_only_turn(n) for n in (3, 4)]
              + [propose_turn(5, title="ICARUS Phase 3 topline data (update)")] + [web_only_turn(n) for n in (6, 7, 8)])
    _, rid, reader, _ = start(runs_dir, script, limits={"stall_turns_per_company": 3})
    _, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 8
    _, kill = assert_reviewed_and_published(meta, r, "stall")
    assert (kill["turn"], kill["last_proposal_turn"]) == (8, 5)


def test_a_refused_or_duplicate_proposal_is_not_progress(runs_dir):
    # turn 3 repeats turn 2's proposal exactly (a duplicate: the same row) -> no progress
    script = [fetch_turn(), propose_turn(2), propose_turn(3), web_only_turn(4), web_only_turn(5)]
    _, rid, reader, _ = start(runs_dir, script, limits={"stall_turns_per_company": 3})
    _, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 5
    _, kill = assert_reviewed_and_published(meta, r, "stall")
    assert kill["last_proposal_turn"] == 2


def test_repeated_identical_searches_count_as_no_progress(runs_dir):
    script = [fetch_turn(), propose_turn(2)] + [search_turn(n) for n in range(3, 3 + 4)]
    _, rid, reader, tools = start(runs_dir, script, limits={"stall_turns_per_company": 4})
    _, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 6
    assert len(r["repeats"]) >= 2                                   # recorded as repeats, and they did not reset
    _, kill = assert_reviewed_and_published(meta, r, "stall")
    assert (kill["turn"], kill["last_proposal_turn"]) == (6, 2)


# ------------------------------------------------------------------ the ceilings

def test_turn_ceiling_ends_research_as_finished_then_review(runs_dir):
    script = [fetch_turn(), propose_turn(2)] + [web_only_turn(n) for n in range(3, 6)]
    _, rid, reader, _ = start(runs_dir, script, limits={"turn_ceiling_per_company": 5, "stall_turns_per_company": 0})
    _, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 5
    c, kill = assert_reviewed_and_published(meta, r, "turn_ceiling")
    assert (kill["setting"], kill["turn"]) == (5, 5)
    assert "the ceiling of 5 model turns was reached" in c["status_reason"]


def test_minutes_ceiling_ends_research_as_finished_then_review(runs_dir, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(research.time, "monotonic", lambda: clock["t"])

    def slow(n):
        def reply(req):
            clock["t"] += 60          # each turn takes a minute
            return web_only_turn(n)
        return reply
    script = [fetch_turn(), propose_turn(2)] + [slow(n) for n in (3, 4)]
    _, rid, reader, _ = start(runs_dir, script, limits={"minutes_ceiling_per_company": 2})
    _, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 4
    c, _ = assert_reviewed_and_published(meta, r, "minutes_ceiling")
    assert "the ceiling of 2 minutes was reached" in c["status_reason"]


# ------------------------------------------------------------------ Finish now and the discard stop

def _only_run(runs_dir):
    (d,) = [p for p in runs_dir.iterdir() if (p / "run.json").exists()]
    return d.name


def test_finish_now_ends_research_then_review_and_publish(runs_dir):
    box = []

    def finish_now(req):
        r = box[0].post(f"/api/runs/{_only_run(runs_dir)}/research-finish", json={"note": "enough"})
        assert r.status_code == 202, r.text
        assert r.json()["by"] == "person" and r.json()["note"] == "enough"
        return web_only_turn(3)
    script = [fetch_turn(), propose_turn(2), finish_now]
    client, rid, reader, _ = start(runs_dir, script, client_box=box)
    _, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 3                              # ended before its next research call
    c, kill = assert_reviewed_and_published(meta, r, "finish_now")
    assert "a person chose Finish now" in c["status_reason"] and kill["setting"] is None
    assert (runs_dir / rid / "research" / research.FINISH_FILE).exists()
    # the run is done now: neither button applies any more
    assert client.post(f"/api/runs/{rid}/research-finish").status_code == 409
    assert client.post(f"/api/runs/{rid}/research-stop").status_code == 409


def test_research_stop_is_unchanged_nothing_reviewed_or_published(runs_dir):
    def stop(req):
        research.request_stop(runs_dir, _only_run(runs_dir))
        return web_only_turn(3)
    script = [fetch_turn(), propose_turn(2), stop]
    _, rid, reader, tools = start(runs_dir, script, review=F.FakeChat([]))
    _, meta, r = the_run(runs_dir, rid)
    assert len(reader.requests) == 3
    c = r["companies"]["PTGX"]
    assert c["status"] == "stopped" and c["limitation"]["kind"] == "person" and "ended_by" not in c
    assert meta["status"] == "needs_input" and meta["status_reason"].startswith("Partial research, not published")
    assert r["review"]["PTGX"]["status"] == "not_run" and tools.reviewer.sent == []
    assert not [k for k in r["kill_conditions"] if k.get("outcome") == "finished"]


def test_research_stop_wins_over_finish_now(runs_dir):
    def both(req):
        rid = _only_run(runs_dir)
        research.request_finish(runs_dir, rid)
        research.request_stop(runs_dir, rid)
        return web_only_turn(3)
    _, rid, reader, tools = start(runs_dir, [fetch_turn(), propose_turn(2), both], review=F.FakeChat([]))
    _, meta, r = the_run(runs_dir, rid)
    assert r["companies"]["PTGX"]["limitation"]["kind"] == "person"
    assert meta["status"] == "needs_input" and tools.reviewer.sent == []


def test_finish_endpoint_refuses_a_page_run_and_an_unknown_body(runs_dir):
    client, rid, _, _ = start(runs_dir, [fetch_turn(), propose_turn(2)] + [web_only_turn(n) for n in range(3, 43)])
    assert client.post(f"/api/runs/{rid}/research-finish", json={"note": 3}).status_code in (409, 422)
    run = run_json(runs_dir, rid)
    run["run"]["choices"]["mode"] = "pages"
    (runs_dir / rid / "run.json").write_text(json.dumps(run))
    assert client.post(f"/api/runs/{rid}/research-finish").status_code == 409
