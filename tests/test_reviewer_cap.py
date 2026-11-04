"""REVIEW-CAP-001 and RATE-LIMIT-001 (live run 10: DeepSeek V4 Pro made 92 reviewer requests, largely repeated Brave
searches, then Baseten returned HTTP 429 and the review ended incomplete). Fakes only: no model, search or paid call.

- the reviewer turn cap (30 by default) asks the reviewer once to submit its findings: complete with what it found;
  a final turn that does not finish is incomplete with the plain reason "review turn limit (N) reached";
- an identical repeated search is answered from the earlier result, never a new (billed) search;
- HTTP 429 is retried after waits (Retry-After honoured); still refused, the frozen draft is handed to the next
  reviewer in the chain (a billing-refused Claude skipped), and attribution names every reviewer that made calls."""

from __future__ import annotations

import json

import pytest

from capture import research_reviewer as rv
from capture.research_reviewer import ChatReply
from runs import research
from tests import research_fakes as F
from tests.test_research_reviewer import PROPOSALS, SOURCES, FakeGate, FakeModel, call, finish, tools

RATE = "The model call did not succeed (RateLimitError, status 429)."
QUERY = "Protagonist Q3 2026 earnings date"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waited: list = []
    monkeypatch.setattr(rv, "_sleep", waited.append)
    return waited


def limited(retry_after=None, model="deepseek-ai/DeepSeek-V4-Pro-0813"):
    return ChatReply(model, error=RATE, http_status=429, may_have_been_billed=False, retry_after=retry_after)


class Search:
    def __init__(self):
        self.queries: list = []

    def __call__(self, q):
        self.queries.append(q)
        return [{"url": "https://example.com/ir", "title": "Investor relations", "snippet": "Q3 results on Nov 6"}]


class ToolsModel(FakeModel):
    """FakeModel that also keeps the tool names offered on each request."""

    def __init__(self, replies):
        super().__init__(replies)
        self.offered: list = []

    def chat(self, messages, tools=None, json_mode=False):
        self.offered.append(sorted((t.get("function") or t)["name"] for t in tools or []))
        return super().chat(messages, tools, json_mode)


def assess_all(verdicts=("finding", "no_issue")):
    return call("assess_rows", {"assessments": [{"row_id": p["row_id"], "revision_id": p["revision_id"],
                                                 "verdict": v} for p, v in zip(PROPOSALS, verdicts)]})


def unsupported():
    return call("report_finding", {"kind": "unsupported_claim", "row_revision_ids": [{"row_id": "r1",
                                                                                      "revision_id": 2}],
                                   "evidence": [], "text": "S1 does not say the Type B meeting has happened."})


def review(model, gate=None, search=None, **kw):
    return rv.review_draft(company={"ticker": "PTGX"}, assignment="brief", proposals=PROPOSALS, sources=SOURCES,
                           search=search or Search(), fetch_capture=lambda url: None, spend_gate=gate or FakeGate(),
                           model=model, **kw)


# ------------------------------------------------------------------ the turn cap

def test_the_cap_is_on_by_default_recorded_and_in_the_request_preview():
    assert rv.REVIEW_TURN_CEILING == 30
    lim = research.validate_limits(None)
    assert lim["review_turn_ceiling_per_company"] == 30
    assert research.validate_limits({"review_turn_ceiling_per_company": 0})["review_turn_ceiling_per_company"] == 0
    with pytest.raises(ValueError):
        research.validate_limits({"review_turn_ceiling_per_company": -1})
    assert "review_turn_ceiling_per_company" in research.STOP_RULE_DEFAULTS     # -> run_context.stop_rules


def test_cap_reached_then_the_final_submit_turn_ends_complete_with_the_findings():
    search = Search()
    model = ToolsModel([tools(unsupported(), assess_all()),
                        tools(call("search", {"query": QUERY})),
                        tools(call("search", {"query": QUERY})),
                        tools(finish("submitted"))])
    rec = review(model, search=search, turn_ceiling=3)
    assert rec["status"] == "complete" and rec["ended_by"] == "review_turn_ceiling"
    assert "review turn limit (3) was reached" in rec["reason"] and "1 finding" in rec["reason"]
    assert [f["kind"] for f in rec["findings"]] == ["unsupported_claim"] and rec["turns"] == 4
    assert rec["settings"]["review_turn_ceiling"] == 3
    final = model.sent[-1][-1]
    assert final["role"] == "user" and "Submit your findings now" in final["content"]
    assert model.offered[-1] == sorted(rv.FINAL_TURN_TOOL_NAMES) and "search" in model.offered[0]
    assert search.queries == [QUERY]      # the repeat was not a new search


def test_cap_reached_and_the_final_turn_does_not_finish_is_incomplete_with_the_cap_reason():
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue"))),
                        tools(call("search", {"query": QUERY})),
                        tools(call("search", {"query": "another"}))])
    rec = review(model, turn_ceiling=2)
    assert rec["status"] == "incomplete" and rec["ended_by"] is None
    assert "review turn limit (2) reached" in rec["reason"]
    assert len(model.sent) == 3        # exactly one final turn, nothing after it


def test_the_final_turn_refuses_a_finish_with_rows_still_unassessed():
    model = ToolsModel([tools(call("read_source_pages", {"source_id": "S1", "pages": [1]})),
                        tools(finish())])
    rec = review(model, turn_ceiling=1)
    assert rec["status"] == "incomplete" and "review turn limit (1) reached" in rec["reason"]
    assert "row(s) have no verdict" in rec["reason"]


def test_cap_zero_is_off():
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue")))] +
                       [tools(call("search", {"query": f"q{i}"})) for i in range(4)] + [tools(finish())])
    rec = review(model, turn_ceiling=0)
    assert rec["status"] == "complete" and rec["turns"] == 6 and rec["settings"]["review_turn_ceiling"] is None


def test_a_repeated_identical_search_is_answered_from_the_earlier_result_and_not_billed():
    search, gate = Search(), FakeGate()
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue"))),
                        tools(call("search", {"query": QUERY})),
                        tools(call("search", {"query": "  " + QUERY.lower() + " "})),
                        tools(finish())])
    rec = review(model, gate=gate, search=search)
    assert search.queries == [QUERY]
    assert [s["status"] for s in rec["searches"]] == ["ok", "repeated"]
    assert sum(1 for e in gate.settles() if e["kind"] == "search") == 1
    answer = json.loads(model.sent[3][-1]["content"])
    assert answer["repeated"] is True and answer["urls"] == ["https://example.com/ir"]


# ------------------------------------------------------------------ HTTP 429

def test_a_429_then_success_is_retried_after_a_wait_and_completes_with_one_reviewer(no_sleep):
    gate = FakeGate()
    model = ToolsModel([limited(), tools(assess_all(("no_issue", "no_issue"))), limited(retry_after=2),
                        tools(finish())])
    rec = review(model, gate=gate)
    assert rec["status"] == "complete" and rec["rate_limited"] is None
    assert no_sleep == [5, 2]          # the default first wait, then the provider's Retry-After
    assert [w["wait_s"] for w in rec["rate_limit_waits"]] == [5, 2]
    assert rec["usage"]["model_requests"] == 4 and rec["turns"] == 2     # each physical request on the gate


def test_a_persistent_429_stops_with_rate_limited_after_the_backoff(no_sleep):
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue")))] + [limited()] * 4)
    rec = review(model)
    assert rec["status"] == "incomplete" and no_sleep == list(rv.RATE_LIMIT_BACKOFF)
    assert rec["rate_limited"]["requests"] == 4 and rec["rate_limited"]["after_turns"] == 1
    assert "rate limit (HTTP 429) 4 times" in rec["reason"]


def test_carried_work_is_rechecked_and_kept_with_its_own_attribution():
    first = {"from": "DeepSeek V4 Pro",
             "findings": [{"finding_id": "F1", "kind": "unsupported_claim", "by": "DeepSeek V4 Pro", "evidence": [],
                           "row_revision_ids": [{"row_id": "r1", "revision_id": 2}], "text": "not said"},
                          {"finding_id": "F2", "kind": "unsupported_claim", "by": "DeepSeek V4 Pro", "evidence": [],
                           "row_revision_ids": [{"row_id": "r1", "revision_id": 1}], "text": "old revision"}],
             "assessments": [{"row_id": "r1", "revision_id": 2, "verdict": "finding"},
                             {"row_id": "r2", "revision_id": 1, "verdict": "no_issue"}]}
    model = ToolsModel([tools(finish())])
    rec = review(model, carried=first)
    assert rec["status"] == "complete"
    assert [(f["by"], f["carried_from"]) for f in rec["findings"]] == [("DeepSeek V4 Pro", "DeepSeek V4 Pro")]
    assert rec["carried"] == {"from": "DeepSeek V4 Pro", "findings": 1, "findings_rejected": 1, "assessments": 2}
    assert "continues the review DeepSeek V4 Pro started" in model.sent[0][2]["content"]


# ------------------------------------------------------------------ the handover in a run

def _billing(fake):
    from capture.research_claude import BILLING_REFUSED
    reply = ChatReply(fake.model, error="Claude Opus 5.5 did not answer: BadRequestError (HTTP 400): credit balance "
                                        "is too low", http_status=400, may_have_been_billed=False)
    reply.access_reason = BILLING_REFUSED
    return reply


def test_a_persistent_429_hands_over_to_muse_spark_open_and_attribution_names_both(runs_dir):
    from report.data import attempted_reviewer_names, models_used
    from tests.test_research_review_wire import DEEPSEEK, _chain_run
    from tests.test_research_s1 import RUN_SCHEMA
    deepseek = F.FakeChat([F.assess_then()] + [limited()] * 4, probe=True, **DEEPSEEK)
    claude = F.FakeChat([], refuse="Your credit balance is too low to access the Anthropic API.")
    claude._refusal = lambda: _billing(claude)
    muse = F.FakeChat([F.chat_tools(("finish_review", {"summary": "continued"}),
                                    model=F.FALLBACK["model"])], **F.FALLBACK)
    run = _chain_run(runs_dir, [deepseek, claude], muse)
    RUN_SCHEMA.validate(run)
    r = run["run"]["research"]
    e = r["review"]["PTGX"]
    assert len(deepseek.sent) == 5 and claude.sent == [] and len(muse.sent) == 1
    assert e["status"] == "complete" and e["reviewer"] == "Muse Spark Open"
    assert e["review"]["carried"]["from"] == "DeepSeek V4 Pro"
    first = e["attempts"][0]
    assert first["reviewer"] == "DeepSeek V4 Pro" and first["status"] == "incomplete"
    assert "Handed over" in first["reason"] and "rate limited" in first["reason"]
    assert attempted_reviewer_names([e]) == ["DeepSeek V4 Pro", "Muse Spark Open"]
    assert models_used(run)["reviewers"] == ["DeepSeek V4 Pro", "Muse Spark Open"]
    acc = r["reviewer_access"]
    assert acc["handovers"][0]["from"] == "DeepSeek V4 Pro" and acc["handovers"][0]["http_status"] == 429
    assert any(c.get("rate_limited") and c["name"] == "DeepSeek V4 Pro" for c in acc["candidates"])
    assert [x["name"] for x in e["reviewer_choice"]["skipped"]] == ["DeepSeek V4 Pro", "Claude Opus 5.5"]


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d
