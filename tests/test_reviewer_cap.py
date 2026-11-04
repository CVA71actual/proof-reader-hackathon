"""REVIEW-CAP-001 and RATE-LIMIT-001 (live run 10: DeepSeek V4 Pro made 92 reviewer requests, largely repeated Brave
searches, then Baseten returned HTTP 429 and the review ended incomplete). Fakes only: no model, search or paid call.

- the reviewer turn cap (30 by default) asks the reviewer once to submit its findings: complete with what it found;
- REVIEW-CAP-002 (live run 11: the final turn did not call finish_review; 0 findings, rows unassessed): a reminder at
  cap-10, a final turn that requires a tool call (tool_choice, when the client supports it), and when the final turn
  still does not finish, CODE closes the review: findings and verdicts kept; complete when every row has a verdict and
  every fetched source is settled, otherwise incomplete "review turn limit (N) reached; K of M rows assessed";
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


class ChoiceModel(ToolsModel):
    """A client that supports tool_choice (as OneShotChat does); keeps the tool_choice of each request."""
    supports_tool_choice = True

    def __init__(self, replies):
        super().__init__(replies)
        self.choices: list = []

    def chat(self, messages, tools=None, json_mode=False, tool_choice=None):
        self.choices.append(tool_choice)
        return super().chat(messages, tools, json_mode)


def test_all_rows_assessed_and_the_final_turn_does_not_finish_code_closes_complete():
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue"))),
                        tools(call("search", {"query": QUERY})),
                        tools(call("search", {"query": "another"}))])
    rec = review(model, turn_ceiling=2)
    assert rec["status"] == "complete" and rec["closed_by"] == "code" and rec["ended_by"] == "review_turn_ceiling"
    assert "review turn limit (2) was reached" in rec["reason"] and "code closed the review: all 2 rows" in rec["reason"]
    assert len(model.sent) == 3        # exactly one final turn, nothing after it
    assert rec["rows_not_assessed"] == [] and len(rec["assessments"]) == 2


def test_the_model_ignores_the_final_turn_code_closes_with_findings_kept_and_rows_counted():
    only_r1 = call("assess_rows", {"assessments": [{"row_id": "r1", "revision_id": 2, "verdict": "finding"}]})
    model = ToolsModel([tools(unsupported(), only_r1),
                        tools(call("search", {"query": QUERY})),
                        tools(call("search", {"query": "ignored the final turn"}))])
    rec = review(model, turn_ceiling=2)
    assert rec["status"] == "incomplete" and rec["closed_by"] == "code"
    assert rec["reason"].startswith("review turn limit (2) reached; 1 of 2 rows assessed")
    assert [f["kind"] for f in rec["findings"]] == ["unsupported_claim"]          # never dropped
    assert [(a["row_id"], a["verdict"]) for a in rec["assessments"]] == [("r1", "finding")]
    assert [r["row_id"] for r in rec["rows_not_assessed"]] == ["r2"]
    assert rec["clean"] is False and len(model.sent) == 3


def test_the_final_turn_refuses_a_finish_with_rows_still_unassessed():
    model = ToolsModel([tools(call("read_source_pages", {"source_id": "S1", "pages": [1]})),
                        tools(finish())])
    rec = review(model, turn_ceiling=1)
    assert rec["status"] == "incomplete" and rec["closed_by"] == "code"
    assert rec["reason"].startswith("review turn limit (1) reached; 0 of 2 rows assessed")
    assert "row(s) have no verdict" in rec["reason"]


def test_a_failed_final_turn_is_closed_by_code_with_the_verdicts_kept():
    error = ChatReply("deepseek-ai/DeepSeek-V4-Pro-0813", error="The model call did not succeed (HTTP 400).",
                      http_status=400, may_have_been_billed=False)
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue"))), error])
    rec = review(model, turn_ceiling=1)
    assert rec["status"] == "complete" and rec["closed_by"] == "code" and "HTTP 400" in rec["reason"]


def test_the_final_turn_requires_a_tool_call_when_the_client_supports_tool_choice():
    model = ChoiceModel([tools(assess_all(("no_issue", "no_issue"))), tools(finish("submitted"))])
    rec = review(model, turn_ceiling=1)
    assert rec["status"] == "complete" and rec["closed_by"] is None and rec["summary"] == "submitted"
    # REVIEW-EARLY-001: every turn requires a tool call on such a client
    assert model.choices == ["required", "required"] and rv.FINAL_TURN_TOOL_CHOICE == rv.TOOL_CHOICE == "required"
    assert model.offered[-1] == sorted(rv.FINAL_TURN_TOOL_NAMES)       # verdict and finding tools stay offered
    assert "assess_rows" in model.offered[-1] and "report_finding" in model.offered[-1]
    assert rec["settings"]["final_turn_tool_choice"] == "required"
    # a client without tool_choice support (Claude: a forced choice 400s) is sent none
    plain = ToolsModel([tools(assess_all(("no_issue", "no_issue"))), tools(finish())])
    assert review(plain, turn_ceiling=1)["settings"]["final_turn_tool_choice"] is None


# ------------------------------------------------------------------ REVIEW-EARLY-001: an early prose reply

PROSE = ChatReply("deepseek-ai/DeepSeek-V4-Pro-0813", content="The draft looks reasonable overall.",
                  finish_reason="stop", prompt_tokens=100, completion_tokens=50)


def test_a_prose_reply_is_nudged_once_then_the_review_goes_on_and_finishes():
    model = ToolsModel([PROSE, tools(assess_all(("no_issue", "no_issue"))), tools(finish("done"))])
    rec = review(model)
    assert rec["status"] == "complete" and rec["closed_by"] is None and rec["summary"] == "done"
    nudge = model.sent[1][-2:]
    assert nudge[0] == {"role": "assistant", "content": PROSE.content}
    assert nudge[1] == {"role": "user", "content": rv.NUDGE_MESSAGE}
    assert rv.NUDGE_MESSAGE == ("Use the tools: give each row a verdict with the verdict tool, report findings, then "
                                "call finish_review.")
    assert rec["prose_replies"][0]["content"] == PROSE.content and rec["turns"] == 3


def test_two_prose_replies_are_closed_by_code_with_findings_and_verdicts_kept():
    only_r1 = call("assess_rows", {"assessments": [{"row_id": "r1", "revision_id": 2, "verdict": "finding"}]})
    model = ToolsModel([tools(unsupported(), only_r1), PROSE, PROSE])
    rec = review(model)
    assert rec["status"] == "incomplete" and rec["closed_by"] == "code"
    assert rec["ended_by"] == "reviewer_stopped_early"
    assert rec["reason"].startswith("reviewer stopped early; 1 of 2 rows assessed")
    assert [f["kind"] for f in rec["findings"]] == ["unsupported_claim"] and len(rec["assessments"]) == 1
    assert len(model.sent) == 3 and len(rec["prose_replies"]) == 2


def test_two_prose_replies_after_every_row_has_a_verdict_close_complete():
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue"))), PROSE, PROSE])
    rec = review(model)
    assert rec["status"] == "complete" and rec["closed_by"] == "code"
    assert rec["reason"].startswith("The reviewer stopped early") and "all 2 rows assessed" in rec["reason"]


def test_an_early_technical_end_is_closed_by_code_and_incomplete_even_with_every_row_assessed():
    error = ChatReply("m", error="The model call did not succeed (HTTP 500).", http_status=500,
                      may_have_been_billed=True)
    rec = review(ToolsModel([tools(assess_all(("no_issue", "no_issue"))), error]))
    assert rec["status"] == "incomplete" and rec["closed_by"] == "code" and "HTTP 500" in rec["reason"]
    assert rec["reason"].startswith("reviewer stopped early; 2 of 2 rows assessed") and len(rec["assessments"]) == 2


def test_a_rejected_tool_choice_is_dropped_and_the_turn_resent_without_it():
    bad = ChatReply("m", error="The model call did not succeed (BadRequestError, status 400).", http_status=400,
                    may_have_been_billed=False)
    gate = FakeGate()
    model = ChoiceModel([bad, tools(assess_all(("no_issue", "no_issue"))), tools(finish())])
    rec = review(model, gate=gate)
    assert rec["status"] == "complete" and rec["turns"] == 2
    assert model.choices == ["required", None, None]
    assert rec["tool_choice_off"]["http_status"] == 400 and rec["usage"]["model_requests"] == 3


def test_the_reminder_is_injected_once_at_cap_minus_ten():
    reminder = "10 turns left: every row needs a verdict; then call finish_review."
    model = ToolsModel([tools(assess_all(("no_issue", "no_issue"))),
                        tools(call("search", {"query": QUERY})),
                        tools(call("search", {"query": "second"})),
                        tools(finish())])
    rec = review(model, turn_ceiling=12)
    def has(msgs):
        return [m for m in msgs if m.get("role") == "user" and m.get("content") == reminder]
    assert [len(has(m)) for m in model.sent] == [0, 0, 1, 1]      # added before request 3 (after 2 turns), once
    assert model.sent[2][-1]["content"] == reminder
    assert rec["reminded_at_turn"] == 2 and rec["status"] == "complete"
    assert rv.REMINDER_TURNS_LEFT == 10
    # the default cap (30): the reminder comes after turn 20
    assert "FIRST give EVERY draft row a verdict" in rv.REVIEWER_INSTRUCTIONS


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
