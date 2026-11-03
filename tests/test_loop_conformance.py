"""Loop conformance (RULES_AND_LESSONS_REGISTER.md, CVA 2026-11-02): every applicable register row is a behavioural
test run against EVERY agent loop through its real entry point, with fakes only (no model, network or paid call).

The loops: `research` (Muse Spark on the Responses API, driven by POST /api/runs/new) and `reviewer` (Muse Spark Open,
run by the same research run after research ends: POST /api/runs/new, then runs/research.py review_company and
capture/research_reviewer.review_draft). A future loop is added by writing its driver class and adding ONE line to
LOOPS; every row then runs against it. Each driver method sets up the row's scenario on that loop and returns plain
observations; the row's test asserts the rule on those observations, the same way for every loop.

Rows covered: A-TO-Z-001, COMPACT-001, INCOMPLETE-001, OUTCOME-001, SETTLE-001, CAP-001, EVIDENCE-001, CODE-OWNS-001,
BRAVE-001, MULTI-FAMILY-001 (the reviewer stage only). A row that a loop does not have is skipped with the reason."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from capture import responses_reader as rr
from capture import research_reviewer as rv
from capture.brave_search import BraveResult
from capture.research_reviewer import ChatReply
from runs import research
from server import jobs
from server.app import create_app
from tests import research_fakes as F
from tests.test_research_s1 import PAGE1, PR1, PR2, QUOTE, _cycle_script, happy_script

COMPACT_TURNS = 30
COMPACT_BAND_BYTES = 30_000     # the stated band: after the first turns, request sizes stay within this spread
SNIPPET = f"SNIPPET-{PR2}"


def _app(runs_dir, reader, tools, limit=None):
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    body = {"mode": "research", "companies": ["PTGX"], "as_of": "2026-11-02"}
    if limit is not None:
        body["spending_limit_usd"] = limit
    r = TestClient(app).post("/api/runs/new", json=body)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _tools(pages=None, reviewer=None, brave=None):
    tools = F.tools(pages if pages is not None else {PR1: (200, "text/html; charset=utf-8", PAGE1),
                                                     PR2: (200, "text/html; charset=utf-8", F.html("Second."))})
    if reviewer is not None:
        tools.reviewer = reviewer
    tools.adjudicator = F.FakeChat([], model=F.ADJUDICATOR_MODEL)
    tools.brave = brave or F.FakeBrave({"q": [PR2]})
    return tools


def _run(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())


def _stored(runs_dir, rid) -> str:
    return "\n".join(p.read_text(errors="replace") for p in (runs_dir / rid).rglob("*")
                     if p.is_file() and p.suffix in (".json", ".jsonl", ".txt"))


def _preset_spend_run(runs_dir, spent, cap):
    run = jobs.create_run(runs_dir, jobs.validate_choices({"mode": "research", "companies": ["PTGX"],
                                                           "spending_limit_usd": cap}), as_of="2026-11-02")
    rid = run["run"]["id"]
    path = runs_dir / rid / "run.json"
    doc = json.loads(path.read_text())
    doc["run"]["spend_usd"] = spent
    path.write_text(json.dumps(doc))
    return rid


def _multipage_pdf(n_pages: int) -> bytes:
    doc = pymupdf.open()
    for n in range(1, n_pages + 1):
        page = doc.new_page(width=612, height=792)
        lines = [QUOTE] if n == 1 else [f"Page {n} marker{n}x of the filing."] + [f"Line {i} filler text"
                                                                               for i in range(30)]
        y = 72
        for line in lines:
            page.insert_text((54, y), line, fontsize=11)
            y += 16
    out = doc.tobytes()
    doc.close()
    return out


# ================================================================================================ the research loop

class ResearchLoop:
    name = "research"

    def a_to_z(self, runs_dir):
        a, b = "https://ir.example.com/a", "https://ir.example.com/b"
        finish = F.call("finish_draft", {"dispositions": [], "limitations": []}, "f")
        reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": a, "purpose": "x"}, "c0")]),
                                  F.body([F.call("fetch_url", {"url": b, "purpose": "x"}, "c1")]),
                                  F.body([finish]), F.body([finish])])
        tools = _tools({a: (200, "text/html", F.html("Source a.")), b: (200, "text/html", F.html("Source b."))})
        rid = _app(runs_dir, reader, tools)
        r = _run(runs_dir, rid)["run"]["research"]
        return {"refused_request_made": b in tools.fetcher.asked,
                "refusal_recorded": any(c.get("a_to_z_gap") for c in r["coverage"]),
                "ended_complete": r["companies"]["PTGX"]["status"] == "finished"}

    def compact(self, runs_dir):
        script, pages = _cycle_script(COMPACT_TURNS // 2)
        reader = F.FakeResponses(script)
        _app(runs_dir, reader, _tools(pages))
        sizes = [rr.request_size(q) for q in reader.requests]
        fresh = all(f"filler{n}word7" in json.dumps(reader.requests[2 * n - 1]["input"])
                    for n in range(1, COMPACT_TURNS // 2 + 1))
        return {"sizes": sizes, "fresh_in_next_request": fresh}

    def incomplete(self, runs_dir):
        output = [F.message("Partial thoughts"), F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1"),
                  F.call("finish_draft", {"dispositions": [], "limitations": []}, "c2")]
        cut = F.body(output, status="incomplete", incomplete="max_output_tokens")
        reader = F.FakeResponses([cut, cut])       # LIMITS-005: a cut-off reply is followed by a fresh turn
        tools = _tools()
        rid = _app(runs_dir, reader, tools)
        run = _run(runs_dir, rid)
        c = run["run"]["research"]["companies"]["PTGX"]
        later = json.dumps([q["input"] for q in reader.requests[1:]])
        return {"dispatched": bool(tools.fetcher.asked), "finish_accepted": c["status"] == "finished",
                "replayed": "Partial thoughts" in later or '"c1"' in later,
                "settled_once_each": c["requests"] == len(reader.requests),
                "reservations_left": bool(jobs._RESERVED.get(rid))}

    def outcome(self, runs_dir):
        finish = F.call("finish_draft", {"dispositions": [], "limitations": []}, "f")
        bad = {"no_status": {"id": "x", "model": rr.MODEL, "output": [finish], "usage": F.usage()},
               "in_progress": F.body([finish], status="in_progress"),
               "output_not_a_list": {"id": "x", "status": "completed", "model": rr.MODEL, "output": "x",
                                     "usage": F.usage()},
               "empty_object": {}}
        out = {}
        for k, body in bad.items():
            d = runs_dir / k
            d.mkdir()
            rid = _app(d, F.FakeResponses([body, body]), _tools())
            c = _run(d, rid)["run"]["research"]["companies"]["PTGX"]
            out[k] = {"success": c["status"] == "finished", "limitation": c["limitation"] is not None}
        return out

    def settle(self, runs_dir):
        out = {}
        call = F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")
        for k, u in {"negative": F.usage(i=-5), "bool": {**F.usage(), "input_tokens": True},
                     "overflow": F.usage(i=10 ** 400), "missing": {}}.items():
            d = runs_dir / k
            d.mkdir()
            rid = _app(d, F.FakeResponses([F.body([call], u=u)]), _tools())
            meta = _run(d, rid)["run"]
            out[k] = {"unpriced": meta["unpriced_calls"] >= 1, "reservations_left": bool(jobs._RESERVED.get(rid))}
        many = [F.search(f"q{i}", [PR1], sid=f"ws{i}") for i in range(200)]
        d = runs_dir / "overrun"
        d.mkdir()
        rid = _app(d, F.FakeResponses([F.body(many + [F.call("finish_draft", {"dispositions": [],
                                                                               "limitations": []}, "f")],
                                              u=F.usage(i=10, o=10))]), _tools())
        out["overrun_recorded"] = bool(_run(d, rid)["run"]["research"].get("reservation_exceeded"))
        return out

    def cap(self, runs_dir):
        rid = _preset_spend_run(runs_dir, 29.99, 30.0)
        reader = F.FakeResponses([])
        research.run_research(runs_dir, rid, reader, _tools())
        meta = _run(runs_dir, rid)["run"]
        lim = meta["choices"]["research_limits"]
        return {"requests_made": len(reader.requests),
                "reason": meta["research"]["companies"]["PTGX"]["limitation"]["reason"],
                "default_limits_off": all(v is None for k, v in lim.items()
                                          if k not in ("concurrency", "stop_on_empty_rate"))
                and lim["stop_on_empty_rate"] is False}

    def evidence(self, runs_dir):
        ev = F.event("ICARUS", QUOTE, source="src-r9-ptgx", date_text="Q2 2027", date="2027-04-01",
                     date_end="2027-06-30")
        reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
                                  F.body([F.call("propose_events", {"events": [ev]}, "c1")]),
                                  F.body([F.call("nothing_material", {"source_id": "src-r1-ptgx", "reason": "x"},
                                                 "c2"),
                                          F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")])])
        rid = _app(runs_dir, reader, _tools())
        run = _run(runs_dir, rid)
        return {"rejected": not [r for r in run["rows"] if r.get("research")]
                and any(x["tool"] == "propose_events" for x in run["run"]["research"]["refusals"])}

    def code_owns(self, runs_dir):
        ev = {**F.event("ICARUS", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30"),
              "id": "e7", "expected_revision": 1}
        reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
                                  F.body([F.call("propose_events", {"events": [ev]}, "c1")]),
                                  F.body([F.call("nothing_material", {"source_id": "src-r1-ptgx", "reason": "x"},
                                                 "c2"),
                                          F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")])])
        _app(runs_dir, reader, _tools())
        msg = json.loads(reader.requests[2]["input"][-1]["output"])["issues"][0]["message"]
        return {"refused": True, "message": msg, "fields": ["id", "expected_revision"]}

    def brave(self, runs_dir):
        brave = F.FakeBrave({"q": [PR2]})
        reader = F.FakeResponses([F.body([F.call("brave_search", {"query": "q", "count": 3}, "b1")]),
                                  F.body([F.message("Thinking.")]),
                                  F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "f")])])
        rid = _app(runs_dir, reader, _tools(brave=brave))
        return {"deliveries": sum(1 for q in reader.requests if SNIPPET in json.dumps(q["input"])),
                "stored": SNIPPET in _stored(runs_dir, rid) or f"TITLE-{PR2}" in _stored(runs_dir, rid),
                "urls_kept": PR2 in _stored(runs_dir, rid)}

    def multi_family(self, runs_dir):
        pytest.skip("MULTI-FAMILY-001 applies to the independent reviewer stage, not to the producer loop")


# ================================================================================================ the reviewer loop

class ReviewerLoop:
    name = "reviewer"

    def _review(self, runs_dir, reviewer, *, brave=None, pages=None, research_script=None):
        tools = _tools(pages, reviewer=reviewer, brave=brave)
        rid = _app(runs_dir, F.FakeResponses(research_script or happy_script()), tools)
        run = _run(runs_dir, rid)
        return rid, tools, run, run["run"]["research"]["review"]["PTGX"]

    def a_to_z(self, runs_dir):
        reviewer = F.FakeChat([F.chat_tools(("search", {"query": "q"})), F.chat_tools(("fetch", {"url": PR2})),
                               F.chat_tools(("finish_review", {"summary": "x"})),
                               F.chat_tools(("finish_review", {"summary": "x"}))])
        _, tools, _, e = self._review(runs_dir, reviewer)
        rec = e["review"]
        return {"refused_request_made": bool(tools.brave.queries) or PR2 in tools.fetcher.asked,
                "refusal_recorded": len(rec["order_refusals"]) >= 2,
                "ended_complete": rec["status"] == "complete"}

    def compact(self, runs_dir):
        pdf_url = "https://ir.example.com/filing.pdf"
        ev = F.event("ICARUS Phase 3 topline data", QUOTE, date_text="Q2 2027", date="2027-04-01",
                     date_end="2027-06-30", program="ICARUS", conditions=[])
        script = [F.body([F.call("fetch_url", {"url": pdf_url, "purpose": "filing"}, "c1")]),
                  F.body([F.call("propose_events", {"events": [ev]}, "c2")]),
                  lambda req: F.body([F.call("finish_draft", {"dispositions": [
                      {"row_id": "e1", "disposition": "proposal", "related_row_id": None, "reason": "x"}],
                      "limitations": []}, "c3")])]
        reads = [("read_source_pages", {"source_id": "src-r1-ptgx", "pages": [n]}) for n in range(2, COMPACT_TURNS + 2)]
        reviewer = F.FakeChat([F.assess_then(reads[0])] + [F.chat_tools(r) for r in reads[1:]]
                              + [F.chat_tools(("finish_review", {"summary": "x"}))])
        self._review(runs_dir, reviewer, pages={pdf_url: (200, "application/pdf", _multipage_pdf(COMPACT_TURNS + 2))},
                     research_script=script)
        sizes = [rv._bytes([m, rv.REVIEWER_TOOLS]) for m in reviewer.sent]
        fresh = all(f"marker{n}x" in json.dumps(reviewer.sent[n - 1]) for n in range(2, COMPACT_TURNS + 2))
        return {"sizes": sizes, "fresh_in_next_request": fresh}

    def incomplete(self, runs_dir):
        cut = F.chat_tools(("fetch", {"url": PR2}), ("finish_review", {"summary": "done"}), finish="length")
        reviewer = F.FakeChat([cut])
        rid, tools, run, e = self._review(runs_dir, reviewer)
        later = json.dumps(reviewer.sent[1:])
        return {"dispatched": PR2 in tools.fetcher.asked, "finish_accepted": e["status"] == "complete",
                "replayed": PR2 in later,
                "settled_once_each": run["run"]["research"]["companies"]["PTGX"]["review_usage"]["model_requests"]
                == len(reviewer.sent),
                "reservations_left": bool(jobs._RESERVED.get(rid))}

    def outcome(self, runs_dir):
        out = {}
        for k, results in {"none": None, "empty_string": "", "object": {"x": 1}, "string": "results"}.items():
            d = runs_dir / k
            d.mkdir()
            brave = F.FakeBrave()
            brave.search = lambda q, count=10, results=results: BraveResult(q, results=results, http_status=200)
            reviewer = F.FakeChat([F.assess_then(("search", {"query": "q"})),
                                   F.chat_tools(("finish_review", {"summary": "x"}))])
            _, _, _, e = self._review(d, reviewer, brave=brave)
            rec = e["review"]
            out[k] = {"success": rec["clean"] or rec["searches"][0]["status"] == "ok",
                      "limitation": any(x.get("query") == "q" for x in rec["limitations"])}
        return out

    def settle(self, runs_dir):
        out = {}
        for k, (p, c) in {"negative": (-5, 10), "bool": (True, 10), "overflow": (10 ** 400, 10),
                          "missing": (None, None)}.items():
            d = runs_dir / k
            d.mkdir()
            reply = ChatReply(F.REVIEWER_MODEL, tool_calls=[{"id": "t", "name": "finish_review",
                                                              "arguments": json.dumps({"summary": "x"})}],
                              finish_reason="tool_calls", prompt_tokens=p, completion_tokens=c)
            rid, _, run, _ = self._review(d, F.FakeChat([reply]))
            u = run["run"]["research"]["companies"]["PTGX"]["review_usage"]
            out[k] = {"unpriced": u["unpriced_calls"] >= 1, "reservations_left": bool(jobs._RESERVED.get(rid))}
        out["overrun_recorded"] = None      # one Brave request is one priced search: no search-count overrun exists
        return out

    def cap(self, runs_dir):
        rid = _preset_spend_run(runs_dir, 29.99, 30.0)
        reviewer = F.FakeChat([])
        research.run_research(runs_dir, rid, F.FakeResponses([]), _tools(reviewer=reviewer))
        e = _run(runs_dir, rid)["run"]["research"]["review"]["PTGX"]
        return {"requests_made": len(reviewer.sent), "reason": e["reason"],
                "default_limits_off": all(e["review"]["settings"][k] is None
                                          for k in ("max_turns", "max_searches", "max_fetches"))}

    def evidence(self, runs_dir):
        reviewer = F.FakeChat([F.assess_then(("search", {"query": "q"})),
                               F.chat_tools(("report_finding", {"kind": "omission", "row_revision_ids": [],
                                                                "evidence": [{"source_id": PR2, "quote": "lead"}],
                                                                "text": "missing"})),
                               F.chat_tools(("finish_review", {"summary": "x"}))])
        _, _, _, e = self._review(runs_dir, reviewer)
        rec = e["review"]
        return {"rejected": rec["findings"] == [] and "not a captured source" in rec["rejected"][0]["problems"][0]}

    def code_owns(self, runs_dir):
        def finding(draft):
            return ("report_finding", {"finding_id": "F9", "kind": "unsupported_claim", "text": "x", "evidence": [],
                                       "row_revision_ids": [{"row_id": draft[0]["row_id"], "revision_id": 1}]})
        reviewer = F.FakeChat([F.assess_then(before=(finding,)), F.chat_tools(("finish_review", {"summary": "x"}))])
        _, _, _, e = self._review(runs_dir, reviewer)
        rec = e["review"]
        return {"refused": rec["findings"] == [], "message": rec["rejected"][0]["problems"][0],
                "fields": ["finding_id"]}

    def brave(self, runs_dir):
        reviewer = F.FakeChat([F.assess_then(("search", {"query": "q"})),
                               F.chat_tools(("read_source_pages", {"source_id": "src-r1-ptgx", "pages": [1]})),
                               F.chat_tools(("finish_review", {"summary": "x"}))])
        rid, _, _, _ = self._review(runs_dir, reviewer)
        return {"deliveries": sum(1 for m in reviewer.sent if SNIPPET in json.dumps(m)),
                "stored": SNIPPET in _stored(runs_dir, rid) or f"TITLE-{PR2}" in _stored(runs_dir, rid),
                "urls_kept": PR2 in _stored(runs_dir, rid)}

    def multi_family(self, runs_dir):
        reviewer = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"}))])
        _, _, _, e = self._review(runs_dir, reviewer)
        return e["families"]


# One line per loop: a new loop's driver is added here and every row runs against it.
LOOPS = [ResearchLoop(), ReviewerLoop()]


@pytest.fixture(params=LOOPS, ids=lambda loop: loop.name)
def loop(request):
    return request.param


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


# ================================================================================================ the rows

def test_A_TO_Z_001_gathering_without_concluding_is_refused_and_recorded(loop, runs_dir):
    o = loop.a_to_z(runs_dir)
    assert o["refused_request_made"] is False          # the refused search/fetch never reached the outside
    assert o["refusal_recorded"] is True
    assert o["ended_complete"] is False                 # it cannot end complete with unconcluded units


def test_COMPACT_001_per_turn_input_stays_in_a_band_and_fresh_text_is_delivered(loop, runs_dir):
    o = loop.compact(runs_dir)
    sizes = o["sizes"]
    assert len(sizes) >= COMPACT_TURNS
    later = sizes[4:]
    assert max(later) - min(later) < COMPACT_BAND_BYTES, (min(later), max(later))
    assert o["fresh_in_next_request"] is True


def test_INCOMPLETE_001_an_incomplete_reply_dispatches_nothing_and_settles_once(loop, runs_dir):
    o = loop.incomplete(runs_dir)
    assert o["dispatched"] is False and o["finish_accepted"] is False and o["replayed"] is False
    assert o["settled_once_each"] is True and o["reservations_left"] is False


def test_OUTCOME_001_malformed_attempts_are_never_success(loop, runs_dir):
    for case, o in loop.outcome(runs_dir).items():
        assert o["success"] is False, case
        assert o["limitation"] is True, case


def test_SETTLE_001_invalid_usage_is_never_priced_and_reservations_are_released(loop, runs_dir):
    o = loop.settle(runs_dir)
    for case in ("negative", "bool", "overflow", "missing"):
        assert o[case]["unpriced"] is True, case
        assert o[case]["reservations_left"] is False, case
    if o["overrun_recorded"] is not None:
        assert o["overrun_recorded"] is True


def test_CAP_001_strict_admission_refuses_a_bound_over_the_cap_and_no_other_limit_is_on(loop, runs_dir):
    o = loop.cap(runs_dir)               # $29.99 spent of $30: the next call's bound (well over $0.01) is refused
    assert o["requests_made"] == 0
    assert "cap" in o["reason"].lower()
    assert o["default_limits_off"] is True


def test_EVIDENCE_001_an_uncaptured_or_snippet_citation_is_rejected(loop, runs_dir):
    assert loop.evidence(runs_dir)["rejected"] is True


def test_CODE_OWNS_001_a_model_supplied_id_is_refused_naming_the_field(loop, runs_dir):
    o = loop.code_owns(runs_dir)
    assert o["refused"] is True
    for field_name in o["fields"]:
        assert field_name in o["message"]


def test_BRAVE_001_brave_payloads_reach_the_model_once_and_are_never_stored(loop, runs_dir):
    o = loop.brave(runs_dir)
    assert o["deliveries"] == 1 and o["stored"] is False and o["urls_kept"] is True


@pytest.mark.xfail(strict=True, reason="MULTI-FAMILY-001 is NOT met: the reviewer (Muse Spark Open) is the open-weights "
                                       "member of the researcher's own Muse Spark family; only the provider differs. "
                                       "Choosing a reviewer of another family is a CVA/design decision, escalated.")
def test_MULTI_FAMILY_001_the_reviewer_family_differs_from_the_producer(loop, runs_dir):
    fam = loop.multi_family(runs_dir)
    assert fam["researcher"]["provider"] and fam["reviewer"]["provider"]      # both recorded
    assert fam["researcher"]["family"] and fam["reviewer"]["family"]
    assert fam["reviewer"]["provider"] != fam["researcher"]["provider"]
    assert fam["reviewer"]["family"] != fam["researcher"]["family"]
