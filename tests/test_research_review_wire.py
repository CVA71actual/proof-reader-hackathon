"""RESEARCHER-FULL-001: the S2 independent reviewer wired into the S1 research loop, through the real path only.

Every test starts a research run through the app (POST /api/runs/new), so the research loop, the review after each
company's research, the run's one spend gate, run.json, the shared state and the export view are the code under test.
Fakes only: a scripted Responses API (researcher), a scripted chat model (Muse Spark Open reviewer, Muse Wonder
adjudicator), a fake Brave Search and a fake fetcher. No model, network or paid call.

Covers: a clean review and its label; findings bound to row revisions, a reviewer-fetched source captured through the
S1 path with its manifest line, a dispute adjudicated fail-closed; an empty draft still reviewed (omission attack); a
truncated review is incomplete; a failed Brave search is a limitation and blocks clean; a gate refusal (an unpriced
call) stops the review as incomplete; the review reserve leaves room for the review after research hits its share."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from capture import research_reviewer as rv
from capture.research_reviewer import ChatReply
from report.data import build_view
from runs import prices, research
from runs.state import current_state
from server.app import create_app
from tests import research_fakes as F
from tests.test_research_s1 import PAGE1, PR1, PR2, QUOTE, RUN_SCHEMA, happy_script, run_json

CONDITION = "subject to FDA feedback"
PAGE2 = F.html("SAN DIEGO, Nov. 1, 2026 -- Partner update.",
               "Protagonist expects EXPAND topline data on October 12, 2027.", title="Partner release")
OMISSION_QUOTE = "Protagonist expects EXPAND topline data on October 12, 2027."


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def start(runs_dir, reader, reviewer, adjudicator=None, *, brave=None, limit=None, pages=None):
    tools = F.tools(pages if pages is not None else {PR1: (200, "text/html; charset=utf-8", PAGE1),
                                                     PR2: (200, "text/html; charset=utf-8", PAGE2)})
    tools.reviewer = reviewer
    tools.adjudicator = adjudicator or F.FakeChat([], model=F.ADJUDICATOR_MODEL)
    tools.brave = brave or F.FakeBrave(answers={"Protagonist EXPAND topline": [PR2]})
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    client = TestClient(app)
    body = {"mode": "research", "companies": ["PTGX"], "as_of": "2026-11-02"}
    if limit is not None:
        body["spending_limit_usd"] = limit
    r = client.post("/api/runs/new", json=body)
    assert r.status_code == 202, r.text
    return client, r.json()["id"], tools


def draft_of(messages) -> dict:
    """The reviewer's input as the wiring sent it (the user message is the draft JSON)."""
    return json.loads(messages[1]["content"])


def decisions(runs_dir, rid):
    path = runs_dir / rid / "decisions.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def exported(runs_dir, rid):
    run = run_json(runs_dir, rid)
    ds = decisions(runs_dir, rid)
    return run, current_state(run, ds), build_view(run, ds, generated_at="2026-11-02T20:00:00+00:00")


def research_rows(run):
    return [r for r in run["rows"] if r.get("research")]


def steps(run, kind):
    return [s for s in run["run"]["research"]["steps"] if s["kind"] == kind]


# ------------------------------------------------------------------ a clean review

def test_clean_review_after_research_labels_the_row_run_and_export(runs_dir, tmp_path):
    reviewer = F.FakeChat([F.assess_then(("search", {"query": "Protagonist EXPAND topline"})),
                           F.chat_tools(("finish_review", {"summary": "Checked the draft; nothing wrong."}))])
    client, rid, tools = start(runs_dir, F.FakeResponses(happy_script()), reviewer)
    run, state, view = exported(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    meta = run["run"]
    r = meta["research"]
    rec = r["review"]["PTGX"]
    assert rec["status"] == "complete" and rec["clean"] is True, rec["reason"]
    assert rec["label"] == research.LABEL_REVIEWED_CLEAN == r["label"] == view["research_label"]
    assert rec["research_ended"]["kind"] == "finished"
    row = research_rows(run)[0]
    assert rec["reviewed_basis"] == [[row["id"], "1"]]
    assert row["research"]["review_findings"] == []
    assert row["research"]["review"] == {"status": "complete", "reviewed_revision": 1, "attempt": 1,
                                         "verdict": "no_issue", "at": rec["finished_at"]}
    assert row["research"]["label"] == research.LABEL_REVIEWED_CLEAN
    # the reviewer saw the draft bound by revision, and the captured source text, never researcher reasoning
    sent = draft_of(reviewer.sent[0])
    assert [(p["row_id"], p["revision_id"]) for p in sent["draft_proposals"]] == [(row["id"], 1)]
    assert "text" not in sent["captured_sources"][0] and sent["captured_sources"][0]["pages"] >= 1
    assert [(c["source_id"], c["page_number"]) for c in sent["cited_pages"]] == [("src-r1-ptgx", 1)]
    assert QUOTE in sent["cited_pages"][0]["text"]
    # its search is Brave (query + URLs in the manifest, never the payload), on the same run gate
    assert tools.brave.queries == ["Protagonist EXPAND topline"]
    manifest = (runs_dir / rid / "research" / "manifest.jsonl").read_text()
    leads = [json.loads(l) for l in manifest.splitlines() if '"stage": "independent_review"' in l]
    assert leads and leads[0]["urls"] == [PR2] and "SNIPPET" not in manifest and "TITLE-" not in manifest
    usage = r["companies"]["PTGX"]["review_usage"]
    expected = 2 * prices.cost_usd("Claude Opus 5.5", 500, 100) + prices.request_cost_usd("Brave Search", 1)
    assert usage["model_requests"] == 2 and usage["search_requests"] == 1
    assert usage["spend_usd"] == pytest.approx(expected)
    assert meta["spend_usd"] >= usage["spend_usd"]
    # the trail and the shared state
    assert steps(run, "review")[-1]["refs"]["label"] == research.LABEL_REVIEWED_CLEAN
    assert row["id"] in state["targets"]
    assert r["audit"]["label"] == research.LABEL_REVIEWED_CLEAN and r["audit"]["exports"]["agree"]
    # exports carry the reviewed label, never "verified"
    from report.build import build as build_report
    out = build_report(rid, out=tmp_path / "rep", run_dir=runs_dir / rid)
    pdf_text = " ".join("".join(p.get_text() for p in pymupdf.open(out["pdf"])).split())
    assert research.LABEL_REVIEWED_CLEAN in pdf_text and "verified" not in pdf_text.lower().replace("unverified", "")


# ------------------------------------------------------------------ findings, a fetched source, adjudication

def finding_script():
    def lost(draft):
        return ("report_finding", {
            "kind": "lost_condition", "row_revision_ids": [{"row_id": draft[0]["row_id"], "revision_id": 1}],
            "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}],
            "text": "The NDA plan is subject to FDA feedback; the draft drops the condition."})
    return [F.assess_then(verdict="finding", before=(lost,)),
            F.chat_tools(("search", {"query": "Protagonist EXPAND topline"})),
            F.chat_tools(("fetch", {"url": PR2})),
            F.chat_tools(("report_finding", {"kind": "omission", "row_revision_ids": [],
                                             "evidence": [{"source_id": "src-r2-ptgx", "quote": OMISSION_QUOTE}],
                                             "text": "EXPAND topline (October 12, 2027) is missing from the draft."})),
            F.chat_tools(("finish_review", {"summary": "Two findings."}))]


def test_findings_bind_to_row_revisions_and_disputes_go_to_wonder_fail_closed(runs_dir):
    reviewer = F.FakeChat(finding_script())
    wonder = F.FakeChat([F.verdict({"verdict": "resolved", "reading": 1, "resolution": "Keep the condition.",
                                    "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}],
                                    "reason": "The release states the condition."})], model=F.ADJUDICATOR_MODEL)
    _, rid, tools = start(runs_dir, F.FakeResponses(happy_script()), reviewer, wonder)
    run, state, view = exported(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    r = run["run"]["research"]
    rec = r["review"]["PTGX"]
    assert rec["status"] == "complete" and rec["clean"] is False
    assert [f["kind"] for f in rec["review"]["findings"]] == ["lost_condition", "omission"]
    row = research_rows(run)[0]
    # the row-level field the calendar reads: one finding, bound to the reviewed revision, with its adjudication
    (f,) = row["research"]["review_findings"]
    assert f["kind"] == "lost_condition" and f["row_id"] == row["id"] and f["revision_id"] == 1 and f["current"]
    assert f["evidence"][0]["source_id"] == "src-r1-ptgx" and f["evidence"][0]["quote"] == CONDITION
    assert f["review_status"] == "complete" and f["by"] == rv.REVIEWER_NAME
    assert f["adjudication"]["outcome"] == rv.RESOLVED and f["adjudication"]["human_accepted"] is False
    assert row["research"]["label"] == research.LABEL_REVIEWED_FINDINGS
    assert r["label"] == view["research_label"] == research.LABEL_REVIEWED_FINDINGS
    # Wonder got one request: the dispute (researcher reading vs reviewer reading) with the captured evidence only
    assert len(wonder.sent) == 1
    body = json.loads(wonder.sent[0][1]["content"])
    assert len(body["readings"]) == 2 and body["captured_sources"][0]["source_id"] == "src-r1-ptgx"
    assert rec["disputes"] == [{"dispute_id": "PTGX-F1", "finding_id": "F1", "row_revision_ids":
                                [{"row_id": row["id"], "revision_id": 1}], "component": "lost_condition"}]
    # the reviewer-fetched source went through the S1 capture path: run source, pages, manifest line
    src = next(s for s in run["sources"] if s["id"] == "src-r2-ptgx")
    assert src["retrieval"]["tool"] == "review_fetch" and src["retrieval"]["stage"] == "independent_review"
    assert (runs_dir / rid / src["pages"][0]["words"]).is_file()
    lines = [json.loads(l) for l in (runs_dir / rid / "research" / "manifest.jsonl").read_text().splitlines()]
    assert any(l["kind"] == "source" and l["source_id"] == "src-r2-ptgx" for l in lines)
    assert r["audit"]["every_source_captured"] and not r["audit"]["problems"]
    # the trail: a finding step per finding, the omission without a row
    fs = steps(run, "finding")
    assert [s["refs"]["finding_kind"] for s in fs] == ["lost_condition", "omission"] and fs[1]["refs"]["rows"] == []
    assert "never a human acceptance" in fs[0]["text"]
    assert row["id"] in state["targets"]


def test_unreadable_verdict_leaves_the_dispute_unresolved(runs_dir):
    reviewer = F.FakeChat(finding_script())
    wonder = F.FakeChat([ChatReply(F.ADJUDICATOR_MODEL, content="not json", finish_reason="stop", prompt_tokens=10,
                                   completion_tokens=5)], model=F.ADJUDICATOR_MODEL)
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, wonder)
    run = run_json(runs_dir, rid)
    (f,) = research_rows(run)[0]["research"]["review_findings"]
    assert f["adjudication"]["outcome"] == rv.UNRESOLVED and f["adjudication"]["human_accepted"] is False


# ------------------------------------------------------------------ an empty draft is still reviewed

def empty_research():
    return [F.body([F.call("finish_draft", {"dispositions": [], "limitations": [
        {"question": "Everything", "tool_call_ids": [], "source_ids": [], "reason": "nothing found"}]}, "c1")])]


def test_empty_draft_runs_the_omission_review(runs_dir):
    reviewer = F.FakeChat([F.chat_tools(("search", {"query": "Protagonist EXPAND topline"})),
                           F.chat_tools(("fetch", {"url": PR2})),
                           F.chat_tools(("report_finding", {"kind": "omission", "row_revision_ids": [],
                                                            "evidence": [{"source_id": "src-r1-ptgx",
                                                                          "quote": OMISSION_QUOTE}],
                                                            "text": "EXPAND topline is missing."})),
                           F.chat_tools(("finish_review", {"summary": "One omission."}))])
    _, rid, _ = start(runs_dir, F.FakeResponses(empty_research()), reviewer)
    run, _, view = exported(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    assert research_rows(run) == []
    assert draft_of(reviewer.sent[0])["draft_proposals"] == []
    rec = run["run"]["research"]["review"]["PTGX"]
    assert rec["status"] == "complete" and [f["kind"] for f in rec["review"]["findings"]] == ["omission"]
    assert rec["adjudication"]["status"] == "not_needed"
    assert rec["label"] == research.LABEL_REVIEWED_FINDINGS == view["research_label"]


# ------------------------------------------------------------------ a truncated review is incomplete

def test_truncated_review_is_incomplete_and_not_labelled_reviewed(runs_dir):
    cut = F.chat_tools(("finish_review", {"summary": "done"}), finish="length")
    reviewer = F.FakeChat([cut])
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer)
    run, _, view = exported(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    rec = run["run"]["research"]["review"]["PTGX"]
    assert rec["status"] == "incomplete" and rec["clean"] is False and "cut off" in rec["reason"]
    row = research_rows(run)[0]
    assert row["research"]["label"] == research.LABEL and row["research"]["review"]["reviewed_revision"] is None
    assert run["run"]["research"]["label"] == view["research_label"] == research.LABEL
    assert "verified" not in json.dumps(run["run"]["research"]["label"]).replace("not independently reviewed", "")


# ------------------------------------------------------------------ a failed Brave search blocks clean

def test_failed_brave_search_is_a_limitation_and_blocks_clean(runs_dir):
    reviewer = F.FakeChat([F.assess_then(("search", {"query": "Protagonist EXPAND topline"})),
                           F.chat_tools(("finish_review", {"summary": "Nothing found."}))])
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, brave=F.FakeBrave(rate_limited=True))
    run, _, view = exported(runs_dir, rid)
    rec = run["run"]["research"]["review"]["PTGX"]
    assert rec["status"] == "complete" and rec["clean"] is False
    assert rec["review"]["limitations"][0]["query"] == "Protagonist EXPAND topline"
    limited = research.LABEL_REVIEWED_LIMITED + " — 1 search did not complete"     # never silently complete
    assert rec["label"] == limited == view["research_label"]
    assert research_rows(run)[0]["research"]["label"] == limited
    usage = run["run"]["research"]["companies"]["PTGX"]["review_usage"]
    assert usage["search_requests"] == 1 and usage["worst_case_charged_usd"] == prices.request_cost_usd("Brave Search", 1)
    assert any("failed" in s["text"] and (s.get("refs") or {}).get("stage") == "independent_review"
               for s in steps(run, "search"))


def test_no_brave_key_refuses_the_reviewer_search_at_the_gate(runs_dir):
    reviewer = F.FakeChat([F.assess_then(("search", {"query": "Protagonist EXPAND topline"}))])
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, brave=F.FakeBrave(key=False))
    rec = run_json(runs_dir, rid)["run"]["research"]["review"]["PTGX"]
    assert rec["status"] == "incomplete" and "Brave Search" in rec["reason"] and rec["label"] == research.LABEL


# ------------------------------------------------------------------ a gate refusal stops the review

def test_gate_refusal_after_an_unpriced_call_makes_the_review_incomplete(runs_dir):
    script = happy_script()
    script[0] = F.body(script[0]["output"], u={}, rid="r1")      # the first research call reports no usage
    reviewer = F.FakeChat([])                                       # no reviewer request may be made
    _, rid, _ = start(runs_dir, F.FakeResponses(script), reviewer)
    run = run_json(runs_dir, rid)
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["limitation"]["kind"] == "unpriced"
    rec = run["run"]["research"]["review"]["PTGX"]
    assert rec["status"] == "incomplete" and "spend gate refused" in rec["reason"]
    assert "could not be priced" in rec["reason"] and reviewer.sent == []
    assert rec["label"] == research.LABEL
    # a refused request delivered nothing: no page counts as read
    assert rec["review"]["reviewed"]["pages_read"] == {}
    assert [x["source_id"] for x in rec["review"]["not_reviewed"]] == ["src-r1-ptgx"]


def test_a_cited_page_in_a_request_the_gate_refused_is_not_read():
    """NEXT-CALLER-001 / COMPACT-001 (Red Team PR177_8cd269a): read = delivered in a request sent AND answered."""
    srcs = [{"source_id": "S1", "pages": [{"page_number": 1, "text": "Cited text."},
                                          {"page_number": 2, "text": "Other."}]}]
    rows = [{"row_id": "e1", "revision_id": 1, "evidence": [{"source_id": "S1", "page_number": 1, "quote": "Cited"}]}]
    rec = rv.review_draft(company="X", assignment="a", proposals=rows, sources=srcs, search=lambda q: [],
                          fetch_capture=lambda u: None, spend_gate=lambda ev: {"admitted": False, "reason": "cap"},
                          model=F.FakeChat([]))
    assert rec["status"] == "incomplete" and rec["reviewed"]["pages_read"] == {}
    assert rec["cited_pages_unread"][0]["source_id"] == "S1" and rec["cited_pages_unread"][0]["page_number"] == 1
    # a request that was sent but failed reads nothing either; one answered request reads the cited page
    failed = ChatReply(F.REVIEWER_MODEL, error="boom", may_have_been_billed=True)
    rec = rv.review_draft(company="X", assignment="a", proposals=rows, sources=srcs, search=lambda q: [],
                          fetch_capture=lambda u: None, spend_gate=lambda ev: {"t": 1}, model=F.FakeChat([failed]))
    assert rec["reviewed"]["pages_read"] == {}
    ok = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"}))])
    rec = rv.review_draft(company="X", assignment="a", proposals=rows, sources=srcs, search=lambda q: [],
                          fetch_capture=lambda u: None, spend_gate=lambda ev: {"t": 1}, model=ok)
    assert rec["reviewed"]["pages_read"] == {"S1": "1"} and rec["cited_pages_unread"] == [] and rec["clean"]


def test_reviewer_reply_without_usage_is_unpriced_never_zero(runs_dir):
    no_usage = ChatReply(F.REVIEWER_MODEL, tool_calls=[{"id": "t1", "name": "search",
                                                        "arguments": json.dumps({"query": "x"})}],
                         finish_reason="tool_calls", prompt_tokens=None, completion_tokens=None)
    reviewer = F.FakeChat([no_usage])
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer)
    run = run_json(runs_dir, rid)
    rec = run["run"]["research"]["review"]["PTGX"]
    usage = run["run"]["research"]["companies"]["PTGX"]["review_usage"]
    assert usage["unpriced_calls"] == 1 and run["run"]["unpriced_calls"] >= 1
    assert rec["status"] == "incomplete" and "could not be priced" in rec["reason"]


# ------------------------------------------------------------------ the review reserve

def test_research_stops_at_its_share_and_the_reserve_lets_the_review_run(runs_dir):
    # $0.12 cap: a research call's worst case (its 16,384-token reply cap alone is ~$0.07, plus input and searches)
    # exceeds research's 85% share ($0.102), so research stops at once; the reviewer's worst case (~$0.08) fits the
    # whole cap, so the review of the (empty) draft runs.
    # a reviewer priced like Muse Spark Open, so its worst case (~$0.08) fits a cap research's share cannot
    reviewer = F.FakeChat([F.chat_tools(("finish_review", {"summary": "Nothing to review."}))], name="Muse Spark Open",
                          family="Muse Spark (Meta)", provider="baseten")
    _, rid, _ = start(runs_dir, F.FakeResponses([]), reviewer, limit=0.12)
    run = run_json(runs_dir, rid)
    r = run["run"]["research"]
    reserve = r["review_reserve"]
    assert reserve["held"] is True and reserve["fraction"] == research.REVIEW_RESERVE_FRACTION
    assert reserve["usd"] == pytest.approx(0.018) and reserve["research_limit_usd"] == pytest.approx(0.102)
    assert "not a hidden limit" in reserve["reason"]
    assert any("Review reserve" in s["text"] for s in steps(run, "review"))
    c = r["companies"]["PTGX"]
    assert c["limitation"]["kind"] == "spend_cap" and "held for the independent review" in c["limitation"]["reason"]
    rec = r["review"]["PTGX"]
    assert rec["research_ended"]["kind"] == "spend_cap"
    assert rec["status"] == "complete" and len(reviewer.sent) == 1
    assert run["run"]["spend_usd"] <= 0.12


def test_no_reserve_is_held_when_the_reviewer_cannot_run(runs_dir):
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), F.FakeChat([], key=False))
    r = run_json(runs_dir, rid)["run"]["research"]
    assert r["review_reserve"]["held"] is False and r["review_reserve"]["research_limit_usd"] == 30.0
    assert r["review"]["PTGX"]["status"] == "incomplete" and r["label"] == research.LABEL


# ------------------------------------------------------------------ the review-only smoke path, offline

def test_review_only_reviews_a_copy_of_a_saved_draft_without_researching(runs_dir, tmp_path):
    from tools import research_smoke
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), F.FakeChat([], key=False))
    original = runs_dir / rid
    before_bytes = {p.relative_to(original): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    before = run_json(runs_dir, rid)
    assert before["run"]["status"] == "done"
    assert before["run"]["research"]["review"]["PTGX"]["status"] == "incomplete"
    tools = F.tools({PR1: (200, "text/html", PAGE1), PR2: (200, "text/html", PAGE2)})
    tools.reviewer = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "ok"}))])
    tools.brave = F.FakeBrave()
    dest, r = research_smoke.review_existing(original, tmp_path / "copies", 1.0, tools)
    # the original folder is byte-for-byte unchanged
    assert {p.relative_to(original): p.read_bytes() for p in original.rglob("*") if p.is_file()} == before_bytes
    after = json.loads((dest / "run.json").read_text())
    RUN_SCHEMA.validate(after)
    rec = r["review"]["PTGX"]
    assert rec["status"] == "complete" and rec["research_ended"]["kind"] == "review_only"
    assert r["label"] == research.LABEL_REVIEWED_CLEAN and r["review_only"]["note"].startswith("Review-only copy")
    assert after["run"]["choices"]["spending_limit_usd"] == pytest.approx((before["run"]["spend_usd"] or 0) + 1.0)
    assert [x["id"] for x in research_rows(after)] == [x["id"] for x in research_rows(before)]
    assert research_rows(after)[0]["research"]["label"] == research.LABEL_REVIEWED_CLEAN
    with pytest.raises(ValueError):          # never into the original, never over an existing copy
        research_smoke.review_existing(original, tmp_path / "copies", 1.0, tools)


def test_smoke_review_refuses_without_the_go(capsys):
    from tools import research_smoke
    assert research_smoke.main(["--review", "/nonexistent", "--cap-usd", "1"]) == 2
    assert "Refused" in capsys.readouterr().out


# ------------------------------------------------------------------ Brave single delivery on the reviewer path

def test_reviewer_brave_titles_and_snippets_reach_the_model_once_and_are_not_stored(runs_dir):
    reviewer = F.FakeChat([F.assess_then(("search", {"query": "Protagonist EXPAND topline"})),
                           F.chat_tools(("fetch", {"url": PR2})),
                           F.chat_tools(("finish_review", {"summary": "done"}))])
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer)
    carrying = [i for i, msgs in enumerate(reviewer.sent) if f"SNIPPET-{PR2}" in json.dumps(msgs)]
    assert carrying == [1]                                  # the request right after the search, and no other
    assert PR2 in json.dumps(reviewer.sent[2])              # later requests keep the query and URLs
    stored = "".join(p.read_text(errors="ignore") for p in (runs_dir / rid).rglob("*")
                     if p.is_file() and p.suffix in (".json", ".jsonl"))
    assert f"SNIPPET-{PR2}" not in stored and f"TITLE-{PR2}" not in stored


# ------------------------------------------------------------------ Option A: request size on a real-size draft

RUN5 = Path("/private/tmp/claude-501/-Users-cva26/2e8cb8e7-5189-4a1c-8b81-4980942b650d/scratchpad/smoke-b308d4c/"
            "runs/run-1")


def _read_script(source_id, turns=30):
    reads = [("read_source_pages", {"source_id": source_id, "pages": list(range(5 * i + 1, 5 * i + 6))})
             for i in range(turns)]
    return ([F.assess_then(reads[0])] + [F.chat_tools(r) for r in reads[1:]]
            + [F.chat_tools(("finish_review", {"summary": "read 150 pages"}))])


def _sizes(reviewer):
    return [rv._bytes([m, rv.REVIEWER_TOOLS]) for m in reviewer.sent]


def _assert_bounded(sizes):
    assert sizes[0] < 150_000, sizes[0]                       # the source LIST + cited pages only
    later = sizes[1:]
    assert len(later) == 30 and max(later) < 150_000
    assert max(later) - min(later) < 25_000, (min(later), max(later))     # flat: bounded replay, retired page text


def test_large_draft_first_request_is_small_and_per_turn_input_stays_flat():
    # 20 sources x 100 pages x ~3,000 characters (about 6 MB of captured text); 20 rows each cite one page
    page = ("Lorem ipsum dolor sit amet consectetur " * 80)[:3000]
    sources = [{"source_id": f"S{i}", "title": f"Filing {i}", "url": f"https://example.com/{i}",
                "pages": [{"page_number": n, "text": f"[S{i} p{n}] " + page} for n in range(1, 101)]}
               for i in range(20)]
    proposals = [{"row_id": f"e{i}", "revision_id": 1, "title": f"Event {i}",
                  "evidence": [{"source_id": f"S{i}", "page_number": 7, "quote": "Lorem ipsum"}]} for i in range(20)]
    reviewer = F.FakeChat(_read_script("S0"))
    gate = []
    rec = rv.review_draft(company="X", assignment="a", proposals=proposals, sources=sources, search=lambda q: [],
                          fetch_capture=lambda u: None, spend_gate=lambda ev: gate.append(ev) or {"t": len(gate)},
                          model=reviewer)
    assert rec["status"] == "complete" and rec["clean"] is True and rec["cited_pages_unread"] == []
    first = draft_of(reviewer.sent[0])
    assert len(first["cited_pages"]) == 20 and "text" not in first["captured_sources"][0]
    _assert_bounded(_sizes(reviewer))
    # every page read was delivered exactly once, then retired; unopened pages are "not reviewed" per source
    assert sum(json.dumps(m).count("[S0 p42]") for m in reviewer.sent) == 1
    nr = {x["source_id"]: x for x in rec["not_reviewed"]}
    assert rec["reviewed"]["pages_read"]["S0"] == "1-100" and "S0" not in nr
    assert nr["S1"]["pages_not_read"] == "1-6,8-100"
    # older turns become a code summary of pages read
    assert "Review so far (code summary" in json.dumps(reviewer.sent[-1])


def test_cited_page_not_available_blocks_clean():
    rec = rv.review_draft(company="X", assignment="a",
                          proposals=[{"row_id": "e1", "revision_id": 1,
                                      "evidence": [{"source_id": "GONE", "page_number": 3, "quote": "q"}]}],
                          sources=[], search=lambda q: [], fetch_capture=lambda u: None,
                          spend_gate=lambda ev: {"t": 1}, model=F.FakeChat([F.assess_then(), F.chat_tools(
                              ("finish_review", {"summary": "x"}))]))
    assert rec["status"] == "complete" and rec["clean"] is False
    assert rec["cited_pages_unread"] == [{"source_id": "GONE", "page_number": 3,
                                          "reason": "A page the draft cites was not available to the reviewer."}]


@pytest.mark.skipif(not (RUN5 / "run.json").is_file(), reason="run 5's saved draft is not on this machine")
def test_run5_real_draft_copy_first_request_and_flat_turns(tmp_path):
    from tools import research_smoke
    tools = F.tools({})
    tools.reviewer = F.FakeChat(_read_script("src-r8-ptgx"))
    tools.brave = F.FakeBrave()
    dest, r = research_smoke.review_existing(RUN5, tmp_path / "copy", 3.0, tools)
    sizes = _sizes(tools.reviewer)
    _assert_bounded(sizes)
    rec = r["review"]["PTGX"]
    assert rec["status"] == "complete" and rec["review"]["cited_pages_unread"] == []
    assert len(draft_of(tools.reviewer.sent[0])["draft_proposals"]) == 22


# ------------------------------------------------------------------ Manus 18:33

def test_incomplete_research_reply_with_a_valid_fetch_dispatches_nothing(runs_dir):
    cut = F.body([F.call("fetch_url", {"url": PR1, "purpose": "company release"}, "c1")], status="incomplete",
                 incomplete="max_output_tokens")
    reviewer = F.FakeChat([F.chat_tools(("finish_review", {"summary": "empty"}))])
    _, rid, tools = start(runs_dir, F.FakeResponses([cut, cut]), reviewer)      # LIMITS-005: two cut-offs stop it
    run = run_json(runs_dir, rid)
    assert tools.fetcher.asked == [] and run["sources"] == []
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["limitation"]["kind"] == "incomplete"
    assert run["run"]["research"]["review"]["PTGX"]["research_ended"]["kind"] == "incomplete"


def _timed_out():
    return ChatReply(F.REVIEWER_MODEL, error="Muse Spark Open did not answer in time.", timed_out=True,
                     may_have_been_billed=True)


def _rereview(runs_dir, rid, tmp_path, reviewer, mutate=None):
    from tools import research_smoke
    src = runs_dir / rid
    if mutate:
        doc = run_json(runs_dir, rid)
        mutate(doc)
        (src / "run.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    tools = F.tools({PR1: (200, "text/html", PAGE1), PR2: (200, "text/html", PAGE2)})
    tools.reviewer, tools.brave = reviewer, F.FakeBrave()
    tools.adjudicator = F.FakeChat([], model=F.ADJUDICATOR_MODEL)
    dest, r = research_smoke.review_existing(src, tmp_path / "copy", 1.0, tools)
    return json.loads((dest / "run.json").read_text()), r


def _first_review(runs_dir):
    wonder = F.FakeChat([F.verdict({"verdict": "unresolved", "reason": "The sources do not settle it."})],
                        model=F.ADJUDICATOR_MODEL)
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), F.FakeChat(finding_script()), wonder)
    run = run_json(runs_dir, rid)
    assert run["run"]["research"]["review"]["PTGX"]["status"] == "complete"
    return rid


def test_a_timed_out_rereview_keeps_earlier_findings_on_an_unchanged_basis(runs_dir, tmp_path):
    rid = _first_review(runs_dir)
    after, r = _rereview(runs_dir, rid, tmp_path, F.FakeChat([_timed_out()]))
    RUN_SCHEMA.validate(after)
    row = research_rows(after)[0]
    (f,) = row["research"]["review_findings"]
    assert f["kind"] == "lost_condition" and f["current"] is True and "stale" not in f
    assert f["review_status"] == "complete" and f["latest_attempt"]["status"] == "incomplete"
    assert f["adjudication"]["outcome"] == rv.UNRESOLVED
    assert row["research"]["review"]["status"] == "incomplete" and row["research"]["review"]["reviewed_revision"] == 1
    # CLAIM-001: the latest attempt did not complete, so the row and the audit copy say "partly reviewed"
    assert row["research"]["label"] == research.LABEL_PARTIAL_FINDINGS
    audit_row = next(x for x in r["audit"]["rows"] if x["id"] == row["id"])
    assert audit_row["scope"]["label"] == research.LABEL_PARTIAL_FINDINGS
    assert "independently" not in audit_row["scope"]["label"]
    e = r["review"]["PTGX"]
    assert e["attempt"] == 2 and e["status"] == "incomplete" and e["attempts"][0]["status"] == "complete"
    assert [x["kind"] for x in e["unbound_findings"]] == ["omission"]        # the earlier omission is kept too
    assert e["label"] == research.LABEL_PARTIAL_FINDINGS == r["label"]


def test_a_finding_whose_basis_changed_becomes_stale(runs_dir, tmp_path):
    rid = _first_review(runs_dir)

    def change_evidence(doc):
        row = next(x for x in doc["rows"] if x.get("research"))
        row["research"]["evidence"][0]["quote"] = "Topline data from the Phase 3 ICARUS trial"
    after, r = _rereview(runs_dir, rid, tmp_path, F.FakeChat([_timed_out()]), change_evidence)
    row = research_rows(after)[0]
    (f,) = row["research"]["review_findings"]
    assert f["current"] is False and f["stale"] is True and "changed" in f["stale_reason"]
    assert row["research"]["label"] == research.LABEL


def test_an_incomplete_review_with_findings_is_labelled_partly_reviewed(runs_dir):
    def first(messages):
        rid = draft_of(messages)["draft_proposals"][0]["row_id"]
        return F.chat_tools(("report_finding", {
            "kind": "lost_condition", "row_revision_ids": [{"row_id": rid, "revision_id": 1}],
            "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}], "text": "Condition dropped."}))
    reviewer = F.FakeChat([first, _timed_out()])
    wonder = F.FakeChat([F.verdict({"verdict": "unresolved", "reason": "unclear"})], model=F.ADJUDICATOR_MODEL)
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, wonder)
    run, _, view = exported(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    rec = run["run"]["research"]["review"]["PTGX"]
    assert rec["status"] == "incomplete"
    row = research_rows(run)[0]
    assert row["research"]["review_findings"][0]["review_status"] == "incomplete"
    for label in (rec["label"], row["research"]["label"], run["run"]["research"]["label"], view["research_label"]):
        assert label == research.LABEL_PARTIAL_FINDINGS and "independently reviewed" not in label


# ------------------------------------------------------------------ A-TO-Z-001 for the reviewer: rows first

PR3 = "https://news.example.net/third"
PAGE3 = F.html("Third source.", "Nothing new about Protagonist here.", title="Third")


def test_a_reviewer_that_only_searches_is_refused_and_the_refusal_is_recorded(runs_dir):
    reviewer = F.FakeChat([F.chat_tools(("search", {"query": "q1"})), F.chat_tools(("search", {"query": "q2"})),
                           F.chat_tools(("fetch", {"url": PR2})),
                           F.chat_tools(("finish_review", {"summary": "nothing"})),
                           F.chat_tools(("finish_review", {"summary": "nothing, again"}))])
    _, rid, tools = start(runs_dir, F.FakeResponses(happy_script()), reviewer)
    run = run_json(runs_dir, rid)
    rec = run["run"]["research"]["review"]["PTGX"]["review"]
    assert tools.brave.queries == [] and PR2 not in tools.fetcher.asked       # no request, no charge
    assert [(x["tool"], x["required"], x["assessed"]) for x in rec["order_refusals"]] == \
        [("search", 1, 0), ("search", 1, 0), ("fetch", 1, 0), ("finish_review", 1, 0), ("finish_review", 1, 0)]
    assert "assess_rows" in rec["order_refusals"][0]["reason"]
    fed = json.loads(reviewer.sent[1][-1]["content"])
    assert fed["status"] == "refused" and fed["code"] == "assess_rows_first"
    assert run["run"]["research"]["companies"]["PTGX"]["review_usage"]["search_requests"] == 0
    assert rec["status"] == "incomplete" and "finish twice" in rec["reason"]       # never "complete" unconcluded
    assert rec["clean"] is False and len(rec["rows_not_assessed"]) == 1
    assert run["run"]["research"]["review"]["PTGX"]["label"] == research.LABEL


def test_one_fetched_source_at_a_time_and_refetch_is_deduplicated(runs_dir):
    pages = {PR1: (200, "text/html", PAGE1), PR2: (200, "text/html", PAGE2), PR3: (200, "text/html", PAGE3)}
    reviewer = F.FakeChat([F.assess_then(("fetch", {"url": PR2})),
                           F.chat_tools(("fetch", {"url": PR3})),                 # refused: src-r2 not settled
                           F.chat_tools(("fetch", {"url": PR2})),                 # already captured: no request
                           F.chat_tools(("fetch", {"url": PR1})),                 # a given source: no request
                           F.chat_tools(("nothing_material", {"source_id": "src-r2-ptgx", "reason": "old news"}),
                                        ("fetch", {"url": PR3})),
                           F.chat_tools(("nothing_material", {"source_id": "src-r3-ptgx", "reason": "nothing"})),
                           F.chat_tools(("finish_review", {"summary": "done"}))])
    _, rid, tools = start(runs_dir, F.FakeResponses(happy_script()), reviewer, pages=pages)
    rec = run_json(runs_dir, rid)["run"]["research"]["review"]["PTGX"]["review"]
    assert tools.fetcher.asked.count(PR2) == 1 and tools.fetcher.asked.count(PR3) == 1
    assert tools.fetcher.asked.count(PR1) == 1                                  # only research fetched it
    assert [x["code"] if "code" in x else x["tool"] for x in rec["order_refusals"]] == ["fetch"]
    assert "not settled" in rec["order_refusals"][0]["reason"]
    assert [f["status"] for f in rec["fetches"]] == ["captured", "already_captured", "already_captured", "captured"]
    assert [n["source_id"] for n in rec["nothing_material"]] == ["src-r2-ptgx", "src-r3-ptgx"]
    assert rec["status"] == "complete" and rec["rows_not_assessed"] == []


@pytest.mark.skipif(not (RUN5 / "run.json").is_file(), reason="run 5's saved draft is not on this machine")
def test_run5_copy_rows_get_verdicts_before_broad_search(tmp_path):
    from tools import research_smoke

    def batch(start, end, query):
        def reply(messages):
            draft = draft_of(messages)["draft_proposals"]
            return F.chat_tools(("assess_rows", {"assessments": [
                {"row_id": r["row_id"], "revision_id": r["revision_id"], "verdict": "no_issue"}
                for r in draft[start:end]]}), ("search", {"query": query}))
        return reply
    tools = F.tools({})
    tools.reviewer = F.FakeChat([F.chat_tools(("search", {"query": "q1"})),       # turn 1: refused, nothing assessed
                                 batch(0, 10, "q2"), batch(10, 22, "q3"),
                                 F.chat_tools(("search", {"query": "q4"})),
                                 F.chat_tools(("finish_review", {"summary": "all rows assessed"}))])
    tools.brave = F.FakeBrave()
    _, r = research_smoke.review_existing(RUN5, tmp_path / "copy", 3.0, tools)
    rec = r["review"]["PTGX"]["review"]
    assert [x["turn"] for x in rec["order_refusals"]] == [1]
    assert tools.brave.queries == ["q2", "q3", "q4"]
    assert len(rec["assessments"]) == 22 and rec["rows_not_assessed"] == []
    first_search_turn = 2
    assert sum(1 for a in rec["assessments"] if a["turn"] <= first_search_turn) >= min(22, 2 * rv.ASSESS_ROWS_PER_TURN)


# ------------------------------------------------------------------ live test 21e10cd: adjudication room, duplicates

def test_reviewing_keeps_room_for_adjudicating_its_findings(runs_dir):
    def lost(draft):
        return ("report_finding", {"kind": "lost_condition",
                                   "row_revision_ids": [{"row_id": draft[0]["row_id"], "revision_id": 1}],
                                   "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}],
                                   "text": "The condition was dropped."})
    # every reviewer reply uses nearly its whole 12,288-token reply allowance: left alone it would spend the whole cap
    heavy = [F.chat_tools(("search", {"query": f"q{i}"}), c=12_000) for i in range(40)]
    reviewer = F.FakeChat([F.assess_then(verdict="finding", before=(lost,), c=12_000)] + heavy)
    wonder = F.FakeChat([F.verdict({"verdict": "resolved", "reading": 1, "resolution": "Keep the condition.",
                                    "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}],
                                    "reason": "The release states it."})], model=F.ADJUDICATOR_MODEL)
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, wonder, limit=1.0)
    run = run_json(runs_dir, rid)
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["status"] == "incomplete" and "keep" in e["reason"] and "adjudication of 1 finding" in e["reason"]
    assert len(reviewer.sent) < 41                                    # it stopped before spending everything
    assert len(wonder.sent) == 1 and e["adjudication"]["outcomes"][0]["outcome"] == rv.RESOLVED
    (f,) = research_rows(run)[0]["research"]["review_findings"]
    assert f["adjudication"]["outcome"] == rv.RESOLVED
    assert run["run"]["spend_usd"] <= 1.0


def test_adjudication_gets_only_the_pages_with_the_cited_quotes():
    pages = [{"page_number": n, "text": f"Page {n} filler. " * 400} for n in range(1, 41)]
    pages[22] = {"page_number": 23, "text": "NDA submission subject to FDA feedback, the company said."}
    srcs = rv._Sources([{"source_id": "S1", "pages": pages}])
    d = {"dispute_id": "X-F1", "component": "lost_condition", "readings": [{"by": "a", "text": "x"},
                                                                           {"by": "b", "text": "y"}],
         "evidence": [{"source_id": "S1", "quote": "subject to FDA feedback"}]}
    messages, cited = rv.adjudication_messages(d, srcs)
    body = json.loads(messages[1]["content"])
    assert cited == ["S1"] and [p["page_number"] for p in body["captured_sources"][0]["pages"]] == [23]
    assert rv._bytes(messages) < 5_000 < len(srcs.by_id["S1"]["text"])   # a page, not the 40-page document


def test_a_second_finding_on_the_same_rows_and_kind_is_merged_not_new():
    srcs = [{"source_id": "S1", "text": "PN-881 Phase 2 data are expected in H1 2027."},
            {"source_id": "S2", "text": "PN-881 data now expected in H2 2027."},
            {"source_id": "S3", "text": "Timing for PN-881 data is under review."}]
    rows = [{"row_id": "e1", "revision_id": 2, "title": "PN-881 data",
             "evidence": [{"source_id": "S1", "page_number": 1, "quote": "PN-881 Phase 2 data"}]}]

    def conflict(sid, quote):
        return ("report_finding", {"kind": "conflicting_guidance", "row_revision_ids": [{"row_id": "e1",
                                                                                        "revision_id": 2}],
                                   "evidence": [{"source_id": sid, "quote": quote}], "text": f"Conflict via {sid}."})
    model = F.FakeChat([F.chat_tools(conflict("S2", "now expected in H2 2027"),
                                     ("assess_rows", {"assessments": [{"row_id": "e1", "revision_id": 2,
                                                                       "verdict": "finding"}]})),
                        F.chat_tools(conflict("S3", "under review")),
                        F.chat_tools(conflict("S2", "now expected in H2 2027")),
                        F.chat_tools(("finish_review", {"summary": "one conflict"}))])
    rec = rv.review_draft(company="X", assignment="a", proposals=rows, sources=srcs, search=lambda q: [],
                          fetch_capture=lambda u: None, spend_gate=lambda ev: {"t": 1}, model=model)
    (f,) = rec["findings"]
    assert f["finding_id"] == "F1" and [e["source_id"] for e in f["evidence"]] == ["S2", "S3"]
    assert len(f["merged"]) == 2
    fed = json.loads(model.sent[2][-1]["content"])
    assert fed["status"] == "merged" and fed["finding_id"] == "F1" and "added to it" in fed["message"]
    assert "no new evidence" in json.loads(model.sent[3][-1]["content"])["message"]



# ------------------------------------------------------------------ Red Team PR177_21e10cd

def test_a_source_the_reviewer_could_not_open_is_named_in_every_label_and_never_clean(runs_dir, tmp_path):
    missing = "https://news.example.net/gone"
    def lost(draft):
        return ("report_finding", {"kind": "lost_condition",
                                   "row_revision_ids": [{"row_id": draft[0]["row_id"], "revision_id": 1}],
                                   "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}], "text": "dropped"})
    reviewer = F.FakeChat([F.assess_then(("fetch", {"url": missing}), verdict="finding", before=(lost,)),
                           F.chat_tools(("finish_review", {"summary": "one finding"}))])
    wonder = F.FakeChat([F.verdict({"verdict": "unresolved", "reason": "unclear"})], model=F.ADJUDICATOR_MODEL)
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, wonder)
    run, _, view = exported(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    r = run["run"]["research"]
    e = r["review"]["PTGX"]
    want = research.LABEL_REVIEWED_FINDINGS + " — 1 source could not be opened"
    assert e["status"] == "complete" and e["clean"] is False and e["limits"]["sources_not_opened"] == 1
    assert e["review"]["limitations"][0]["url"] == missing
    row = research_rows(run)[0]
    audit_row = next(x for x in r["audit"]["rows"] if x["id"] == row["id"])
    for label in (e["label"], row["research"]["label"], r["label"], view["research_label"], audit_row["scope"]["label"]):
        assert label == want
    from report.build import build as build_report
    out = build_report(rid, out=tmp_path / "rep", run_dir=runs_dir / rid)
    assert "1 source could not be opened" in " ".join("".join(p.get_text() for p in pymupdf.open(out["pdf"])).split())


def _complete_rereview(runs_dir, rid, tmp_path, verdict, extra=(), mutate=None):
    reviewer = F.FakeChat([F.assess_then(verdict=verdict, before=extra),
                           F.chat_tools(("finish_review", {"summary": "re-review"}))])
    return _rereview(runs_dir, rid, tmp_path, reviewer, mutate)


def _first_review_finding_only(runs_dir):
    def lost(draft):
        return ("report_finding", {"kind": "lost_condition",
                                   "row_revision_ids": [{"row_id": draft[0]["row_id"], "revision_id": 1}],
                                   "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}], "text": "dropped"})
    reviewer = F.FakeChat([F.assess_then(verdict="finding", before=(lost,)),
                           F.chat_tools(("finish_review", {"summary": "x"}))])
    wonder = F.FakeChat([F.verdict({"verdict": "unresolved", "reason": "unclear"})], model=F.ADJUDICATOR_MODEL)
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, wonder)
    return rid


def test_a_complete_rereview_that_does_not_resolve_a_finding_keeps_it_current(runs_dir, tmp_path):
    rid = _first_review_finding_only(runs_dir)
    def other(draft):      # the complete re-review finds a DIFFERENT problem on the row; the first is not resolved
        return ("report_finding", {"kind": "unsupported_claim", "evidence": [], "text": "overstated",
                                   "row_revision_ids": [{"row_id": draft[0]["row_id"], "revision_id": 1}]})
    after, r = _complete_rereview(runs_dir, rid, tmp_path, "finding", extra=(other,))
    fs = {f["kind"]: f for f in research_rows(after)[0]["research"]["review_findings"]}
    assert fs["lost_condition"]["current"] is True and fs["lost_condition"]["attempt"] == 1
    assert fs["unsupported_claim"]["current"] is True and fs["unsupported_claim"]["attempt"] == 2
    assert r["review"]["PTGX"]["status"] == "complete"


def test_a_complete_rereview_resolves_a_finding_only_with_no_repeat_and_a_no_issue_verdict(runs_dir, tmp_path):
    rid = _first_review_finding_only(runs_dir)
    after, r = _complete_rereview(runs_dir, rid, tmp_path, "no_issue")
    row = research_rows(after)[0]
    (f,) = row["research"]["review_findings"]
    assert f["current"] is False and f["resolved"] is True and f["resolved_by_attempt"] == 2
    assert row["research"]["label"] == research.LABEL_REVIEWED_CLEAN        # still in history, no longer current


def test_a_row_revision_change_still_expires_findings_under_a_complete_rereview(runs_dir, tmp_path):
    rid = _first_review_finding_only(runs_dir)

    def change_evidence(doc):
        row = next(x for x in doc["rows"] if x.get("research"))
        row["research"]["evidence"][0]["quote"] = "Topline data from the Phase 3 ICARUS trial"
    after, _ = _complete_rereview(runs_dir, rid, tmp_path, "no_issue", mutate=change_evidence)
    (f,) = research_rows(after)[0]["research"]["review_findings"]
    assert f["current"] is False and f["stale"] is True and "resolved" not in f


def test_a_finished_company_is_recorded_as_finished_not_researching(runs_dir):
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), F.FakeChat([], key=False))
    r = run_json(runs_dir, rid)["run"]["research"]
    c = r["companies"]["PTGX"]
    assert c["status"] == "finished" and c["status_reason"].startswith("The model finished: 1 proposal")
    finish = [s for s in r["steps"] if s["kind"] == "finish"]
    assert finish and all("Researching" not in s["text"] for s in finish)
    assert finish[0]["text"].startswith("The model finished: 1 proposal")



# ------------------------------------------------------------------ MULTI-FAMILY-001 (CVA): DeepSeek V4 Pro reviews

def test_claude_reviews_and_its_provider_and_family_are_recorded(runs_dir):
    reviewer = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"}))])
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer)
    run = run_json(runs_dir, rid)
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["reviewer_choice"]["fallback_used"] is False and e["reviewer"] == "Claude Opus 5.5"
    fam = e["families"]
    assert fam["reviewer"] == {"name": "Claude Opus 5.5", "model": F.REVIEWER_MODEL, "provider": "anthropic",
                               "family": "Claude"}
    assert fam["researcher"]["family"] == "Muse Spark (Meta)" and fam["same_family"] is False
    assert e["multi_family"]["met"] is True
    assert e["label"] == ("Agent research — independently reviewed by Claude Opus 5.5 (different model family from "
                          "the researcher) (no findings)")
    assert run["run"]["research"]["companies"]["PTGX"]["review_usage"]["spend_usd"] == pytest.approx(
        2 * prices.cost_usd("Claude Opus 5.5", 500, 100))
    assert prices.REVIEWER_PRICES_PER_1M_USD["Claude Opus 5.5"] == {"input": 4.0, "output": 20.0,
                                                                    "cached_input": 0.2}
    assert prices.REVIEWER_PRICES_PER_1M_USD["DeepSeek V4 Pro"] == {"input": 1.32, "output": 3.96,
                                                                    "cached_input": 0.132}
    assert "not yet confirmed" in prices.REVIEWER_PRICE_STATUS["DeepSeek V4 Pro"]
    assert run["run"]["prices"]["reviewer_per_1m_tokens_usd"]["DeepSeek V4 Pro"]["output"] == 3.96


def test_the_declared_fallback_runs_only_when_deepseek_is_unavailable_and_says_same_family(runs_dir):
    primary = F.FakeChat([], unavailable="deepseek-ai/DeepSeek-V4-Pro-0813 is not in the provider's model list.")
    fallback = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"}))], **F.FALLBACK)
    tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1)})
    tools.reviewer, tools.reviewer_fallback, tools.brave = primary, fallback, F.FakeBrave()
    tools.adjudicator = F.FakeChat([], model=F.ADJUDICATOR_MODEL)
    reader = F.FakeResponses(happy_script())
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    rid = TestClient(app).post("/api/runs/new", json={"mode": "research", "companies": ["PTGX"],
                                                      "as_of": "2026-11-02"}).json()["id"]
    run = run_json(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    e = run["run"]["research"]["review"]["PTGX"]
    assert primary.sent == [] and len(fallback.sent) == 2
    assert e["reviewer_choice"]["fallback_used"] is True and "not in the provider's model list" in \
        e["reviewer_choice"]["primary_unavailable"]
    assert e["families"]["same_family"] is True and e["families"]["reviewer"]["family"] == "Muse Spark (Meta)"
    want = "Agent research — reviewed by Muse Spark Open (same model family as the researcher) (no findings)"
    row = research_rows(run)[0]
    for label in (e["label"], row["research"]["label"], run["run"]["research"]["label"]):
        assert label == want and "independently" not in label
    assert any("declared fallback" in s["text"] for s in run["run"]["research"]["steps"] if s["kind"] == "review")


# ------------------------------------------------------------------ labels derived on load (e2e #180, CLAIM-001)

def test_labels_are_derived_from_the_recorded_review_on_load_not_read_back_as_stored_text(runs_dir, tmp_path):
    missing = "https://news.example.net/gone"
    def lost(draft):
        return ("report_finding", {"kind": "lost_condition",
                                   "row_revision_ids": [{"row_id": draft[0]["row_id"], "revision_id": 1}],
                                   "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}], "text": "dropped"})
    reviewer = F.FakeChat([F.assess_then(("fetch", {"url": missing}), verdict="finding", before=(lost,)),
                           _timed_out()])                                   # incomplete, one source not opened
    wonder = F.FakeChat([F.verdict({"verdict": "unresolved", "reason": "unclear"})], model=F.ADJUDICATOR_MODEL)
    client, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer, wonder)
    path = runs_dir / rid / "run.json"
    doc = json.loads(path.read_text())
    # simulate a run saved by an older build: stored labels without the limit, no derived fields
    old = "Agent research — partly reviewed (incomplete), findings so far"
    doc["run"]["research"]["label"] = old
    e = doc["run"]["research"]["review"]["PTGX"]
    e["label"] = old
    for k in ("label_kind", "limits", "reviewer_ran"):
        e.pop(k, None)
    for row in doc["rows"]:
        if row.get("research"):
            row["research"]["label"] = old
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    before = path.read_bytes()
    want = "1 source could not be opened"
    api = client.get(f"/api/runs/{rid}").json()
    labels = [api["run"]["research"]["label"]] + [r["research"]["label"] for r in api["rows"] if r.get("research")]
    from report.data import load_run_dir
    run, decisions = load_run_dir(runs_dir / rid)
    view = build_view(run, decisions, generated_at="2026-11-02T20:00:00+00:00")
    labels.append(view["research_label"])
    for label in labels:
        assert want in label and "partly reviewed by Claude Opus 5.5 (different model family from the " \
            "researcher) (incomplete)" in label, label
    from report.build import build as build_report
    out = build_report(rid, out=tmp_path / "rep", run_dir=runs_dir / rid)
    assert want in " ".join("".join(p.get_text() for p in pymupdf.open(out["pdf"])).split())
    assert path.read_bytes() == before                                       # the saved file is not rewritten


# ------------------------------------------------------------------ ten-company dry run (DRYRUN-1/2/3)

def test_dryrun_1_the_run_label_is_never_independent_when_a_company_did_not_finish_research(runs_dir):
    reviewer = F.FakeChat([F.chat_tools(("finish_review", {"summary": "Nothing to review."}))], name="Muse Spark Open",
                          family="Muse Spark (Meta)", provider="baseten")
    _, rid, _ = start(runs_dir, F.FakeResponses([]), reviewer, limit=0.12)     # research starved at its share
    run = run_json(runs_dir, rid)
    r = run["run"]["research"]
    assert r["companies"]["PTGX"]["status"] == "stopped" and r["review"]["PTGX"]["status"] == "complete"
    assert "independently reviewed by" not in r["label"]
    assert r["label"].startswith(research.LABEL) and "research did not finish for 1 of 1 company" in r["label"]
    RUN_SCHEMA.validate(run)


def test_dryrun_2_finish_beside_other_calls_twice_ends_the_review_incomplete(runs_dir):
    mixed = [F.chat_tools(("search", {"query": f"q{i}"}), ("finish_review", {"summary": "done"})) for i in range(50)]
    reviewer = F.FakeChat([F.assess_then()] + mixed)
    _, rid, tools = start(runs_dir, F.FakeResponses(happy_script()), reviewer,
                          brave=F.FakeBrave({f"q{i}": [] for i in range(50)}))
    run = run_json(runs_dir, rid)
    e = run["run"]["research"]["review"]["PTGX"]
    assert len(reviewer.sent) == 3                                  # assess, mixed (reminded), mixed (stop)
    assert tools.brave.queries == ["q0", "q1"]                      # the other calls of both replies still ran
    assert e["status"] == "incomplete" and "not sent on its own after a reminder" in e["reason"]
    reps = [s for s in run["run"]["research"]["steps"] if s["kind"] == "repeat" and
            (s.get("refs") or {}).get("stage") == "independent_review"]
    assert len(reps) == 2 and "reminded" in reps[0]["text"] and "ended incomplete" in reps[1]["text"]


def test_dryrun_3_each_company_carries_its_review_status(runs_dir):
    seen = []

    def finish(messages):
        rid = next(p.name for p in runs_dir.iterdir())
        seen.append(run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]["review_status"])
        return F.chat_tools(("finish_review", {"summary": "x"}))
    reviewer = F.FakeChat([F.assess_then(), finish])
    _, rid, _ = start(runs_dir, F.FakeResponses(happy_script()), reviewer)
    c = run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]
    assert seen == ["reviewing"] and c["status"] == "finished" and c["review_status"] == "done"


# ------------------------------------------------------------------ live test 5d028a8: the key may not use DeepSeek

REFUSED = "Error code: 403 - please check the api-key you provided"


def _two_company_run(runs_dir, primary, fallback):
    reader = F.PerCompany({"PTGX": happy_script(), "ROIV": [F.body([F.call(
        "finish_draft", {"dispositions": [], "limitations": []}, "f")])]})
    tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1)})
    ident = {"PTGX": ("1377121", "Protagonist Therapeutics, Inc."), "ROIV": ("1635088", "Roivant Sciences Ltd.")}
    tools.resolve = lambda t: ident[t]
    tools.reviewer, tools.reviewer_fallback, tools.brave = primary, fallback, F.FakeBrave()
    tools.adjudicator = F.FakeChat([], model=F.ADJUDICATOR_MODEL)
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    r = TestClient(app).post("/api/runs/new", json={"mode": "research", "companies": ["PTGX", "ROIV"],
                                                     "as_of": "2026-11-02", "research_limits": {"concurrency": 1}})
    assert r.status_code == 202, r.text
    return run_json(runs_dir, r.json()["id"])


def test_a_403_on_the_access_check_falls_back_once_per_run_and_says_same_family(runs_dir):
    primary = F.FakeChat([], refuse=REFUSED)
    fallback = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"})),
                           F.chat_tools(("finish_review", {"summary": "empty"}))], **F.FALLBACK)
    run = _two_company_run(runs_dir, primary, fallback)
    RUN_SCHEMA.validate(run)
    r = run["run"]["research"]
    assert primary.probes == 1 and primary.sent == []                # checked once per run, never retried
    acc = r["reviewer_access"]
    assert acc["refused"] is True and acc["http_status"] == 403 and "check the api-key" in acc["message"]
    assert acc["fallback"] == "Muse Spark Open"
    for t in ("PTGX", "ROIV"):
        e = r["review"][t]
        assert e["reviewer_choice"]["fallback_used"] is True and e["reviewer"] == "Muse Spark Open"
        assert "HTTP 403" in e["reviewer_choice"]["primary_unavailable"]
        assert e["families"]["same_family"] is True and e["multi_family"]["met"] is False
        assert e["label"].startswith("Agent research — reviewed by Muse Spark Open (same model family as the "
                                     "researcher)") and "independently" not in e["label"]
    assert r["companies"]["PTGX"]["review_status"] == "done"
    assert any("not retried" in s["text"] and "MULTI-FAMILY-001 not met" in s["text"] for s in r["steps"])


def test_a_403_on_the_first_review_call_switches_to_the_fallback_without_retrying(runs_dir):
    primary = F.FakeChat([], refuse=REFUSED)
    primary.probe = lambda: None                                      # no access check: the first call is refused
    fallback = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"})),
                           F.chat_tools(("finish_review", {"summary": "empty"}))], **F.FALLBACK)
    run = _two_company_run(runs_dir, primary, fallback)
    r = run["run"]["research"]
    assert len(primary.sent) == 1                                     # one refused call in the whole run
    assert r["review"]["PTGX"]["status"] == "complete" and r["review"]["ROIV"]["status"] == "complete"
    assert all(r["review"][t]["reviewer"] == "Muse Spark Open" for t in ("PTGX", "ROIV"))
    assert r["reviewer_access"]["refused"] is True and r["review"]["PTGX"]["multi_family"]["met"] is False
    assert r["review"]["PTGX"]["usage"]["spend_usd"] >= 0


def test_a_passing_access_check_keeps_claude_and_records_it(runs_dir):
    primary = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"})),
                          F.chat_tools(("finish_review", {"summary": "empty"}))], probe=True)
    run = _two_company_run(runs_dir, primary, F.FakeChat([], **F.FALLBACK))
    r = run["run"]["research"]
    assert primary.probes == 1 and r["reviewer_access"]["checked"] is True and r["reviewer_access"]["refused"] is False
    assert all(r["review"][t]["reviewer"] == "Claude Opus 5.5" and r["review"][t]["multi_family"]["met"]
               for t in ("PTGX", "ROIV"))


def test_the_smoke_tool_names_the_reviewer_that_ran_not_the_configured_one(runs_dir):
    from tools import research_smoke
    primary = F.FakeChat([], refuse=REFUSED)
    fallback = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"})),
                           F.chat_tools(("finish_review", {"summary": "empty"}))], **F.FALLBACK)
    r = _two_company_run(runs_dir, primary, fallback)["run"]["research"]
    assert research_smoke.reviewer_line(r) == "Reviewer: Muse Spark Open (fallback: Claude Opus 5.5 refused 403)"
    ok = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"})),
                     F.chat_tools(("finish_review", {"summary": "empty"}))], probe=True)
    d = runs_dir / "ok"
    d.mkdir()
    r2 = _two_company_run(d, ok, F.FakeChat([], **F.FALLBACK))["run"]["research"]
    assert research_smoke.reviewer_line(r2) == "Reviewer: Claude Opus 5.5"
