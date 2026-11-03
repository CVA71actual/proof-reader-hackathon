"""The Claude reviewer (CVA decision 2026-11-03; MULTI-FAMILY-001) through the real path, with a FAKE anthropic client:
POST /api/runs/new -> research -> the access check -> review_draft's tool loop through capture/research_claude.py ->
run.json -> labels. No SDK, network or paid call: the client is injected (the SDK is imported lazily in the adapter)."""

from __future__ import annotations

import copy
import json
import sys

import pytest

from capture import research_claude as rc_claude
from tests import research_fakes as F
from tests.test_research_review_wire import start
from tests.test_research_s1 import RUN_SCHEMA, happy_script, run_json


def _usage(i=600, o=120, **extra):
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            **extra}


def thinking(n=1):
    return {"type": "thinking", "thinking": "", "signature": f"SIG-{n}"}


def tool_use(name, args, tid):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def message(content, stop="tool_use", model=rc_claude.MODEL, usage=None, stop_details=None):
    m = {"id": "msg", "type": "message", "role": "assistant", "model": model, "content": content,
         "stop_reason": stop, "usage": usage or _usage()}
    if stop_details is not None:
        m["stop_details"] = stop_details
    return m


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeAnthropic:
    """The documented surface the adapter uses: client.beta.messages.stream(**kw) -> context manager ->
    get_final_message(). `script`: messages (dicts), exceptions, or callables (request kwargs -> one of those). The
    one-token access check is answered separately (`probe`: a message, an exception, or the default ok)."""

    def __init__(self, script, probe=None):
        self.script, self.probe_answer, self.requests, self.probes = list(script), probe, [], 0
        self.beta = self
        self.messages = self

    def stream(self, **kw):
        if kw.get("max_tokens") == rc_claude.PROBE_MAX_TOKENS and "tools" not in kw:
            self.probes += 1
            item = self.probe_answer or message([{"type": "text", "text": "ok"}], stop="end_turn",
                                                usage=_usage(10, 2))
        else:
            self.requests.append(copy.deepcopy(kw))
            if not self.script:
                raise AssertionError("more Claude requests than scripted")
            item = self.script.pop(0)
            if callable(item):
                item = item(kw)
        if isinstance(item, Exception):
            raise item
        return _Stream(item)


class PermissionDeniedError(Exception):
    status_code = 403

    def __init__(self, msg):
        super().__init__(msg)
        self.message = msg


def _assess(kw, extra=(), verdict="no_issue", before=()):
    draft = json.loads(kw["messages"][0]["content"])["draft_proposals"]
    calls = [b(draft) if callable(b) else b for b in before]
    calls.append(tool_use("assess_rows", {"assessments": [{"row_id": r["row_id"], "revision_id": r["revision_id"],
                                                           "verdict": verdict} for r in draft]}, "t-assess"))
    calls += list(extra)
    return message([thinking(1)] + calls)


def _run(runs_dir, script, probe=None, **kw):
    fake = FakeAnthropic(script, probe=probe)
    reviewer = rc_claude.ClaudeChat(client=fake)
    _, rid, tools = start(runs_dir, F.FakeResponses(happy_script()), reviewer, **kw)
    return fake, rid, run_json(runs_dir, rid)


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def test_a_normal_claude_review_sends_the_documented_request_and_passes_thinking_back_unchanged(runs_dir):
    fake, rid, run = _run(runs_dir, [_assess, message([thinking(2), tool_use("finish_review", {"summary": "ok"},
                                                                               "t-fin")])])
    RUN_SCHEMA.validate(run)
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["status"] == "complete" and e["clean"] is True and e["reviewer"] == "Claude Opus 5.5"
    assert fake.probes == 1 and run["run"]["research"]["reviewer_access"]["refused"] is False
    r1, r2 = fake.requests
    assert r1["model"] == "claude-opus-5-5" and r1["max_tokens"] == 16384
    assert r1["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert r1["output_config"] == {"effort": "high"} and r1["tool_choice"] == {"type": "auto"}
    assert r1["betas"] == ["thinking-binding-controls-2026-08-01"] and "fallbacks" not in r1   # Codex ruling
    assert "independent reviewer" in r1["system"] and all(m["role"] != "system" for m in r1["messages"])
    assert {t["name"] for t in r1["tools"]} >= {"search", "fetch", "read_source_pages", "assess_rows",
                                                "nothing_material", "report_finding", "finish_review"}
    assert all("input_schema" in t for t in r1["tools"])
    # the assistant turn goes back exactly as it came, thinking block and signature included
    assistant = next(m for m in r2["messages"] if m["role"] == "assistant")
    assert assistant["content"][0] == thinking(1) and assistant["content"][1]["name"] == "assess_rows"
    results = r2["messages"][-1]
    assert results["role"] == "user" and [b["type"] for b in results["content"]] == ["tool_result"]
    assert results["content"][0]["tool_use_id"] == "t-assess" and "is_error" not in results["content"][0]


def test_the_tool_loop_returns_every_result_in_one_user_message_with_is_error_on_failures(runs_dir):
    def first(kw):
        return _assess(kw, extra=[tool_use("search", {"query": "q"}, "t-s"),
                                  tool_use("read_source_pages", {"source_id": "src-r1-ptgx", "pages": [1]}, "t-r")])
    fake, rid, run = _run(runs_dir, [first, message([tool_use("finish_review", {"summary": "x"}, "t-f")])],
                          brave=F.FakeBrave(rate_limited=True))
    r2 = fake.requests[1]
    results = r2["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in results] == ["t-assess", "t-s", "t-r"]
    assert results[1].get("is_error") is True and "is_error" not in results[2]
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["status"] == "complete" and "1 search did not complete" in e["label"]


def test_a_max_tokens_reply_is_a_runaway_and_nothing_in_it_runs(runs_dir):
    cut = message([thinking(1), tool_use("finish_review", {"summary": "x"}, "t")], stop="max_tokens")
    fake, rid, run = _run(runs_dir, [cut])
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["status"] == "incomplete" and "cut off" in e["reason"] and len(fake.requests) == 1


def test_a_refusal_is_a_recorded_stop_with_its_category(runs_dir):
    declined = message([], stop="refusal", stop_details={"category": "cyber", "explanation": "declined"})
    fake, rid, run = _run(runs_dir, [declined])
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["status"] == "incomplete" and "refusal, category cyber" in e["reason"]
    assert e["review"]["refusal"] == {"category": "cyber", "explanation": "declined", "model": "claude-opus-5-5"}


def test_an_unexpected_other_model_answering_is_recorded_and_unpriced_never_priced_low(runs_dir):
    """No server-side fallback is requested; if another model nonetheless answers, it is recorded and its call is
    unpriced (no price is known for it), so the gate stops paid work instead of under-counting."""
    def first(kw):
        m = _assess(kw)
        m.update(model="claude-opus-4-8")
        return m
    fake, rid, run = _run(runs_dir, [first])
    rec = run["run"]["research"]["review"]["PTGX"]["review"]
    assert rec["served_by"][0]["model"] == "claude-opus-4-8"
    assert run["run"]["research"]["companies"]["PTGX"]["review_usage"]["unpriced_calls"] == 1


def test_a_403_falls_back_to_muse_spark_open_and_the_label_says_same_family(runs_dir):
    fake = FakeAnthropic([], probe=PermissionDeniedError("Your API key does not have permission to use this model."))
    fallback = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"}))], **F.FALLBACK)
    reviewer = rc_claude.ClaudeChat(client=fake)

    def go():
        from tests.test_research_review_wire import PAGE1, PR1
        from fastapi.testclient import TestClient
        from server.app import create_app
        tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1)})
        tools.reviewer, tools.reviewer_fallback, tools.brave = reviewer, fallback, F.FakeBrave()
        tools.adjudicator = F.FakeChat([], model=F.ADJUDICATOR_MODEL)
        reader = F.FakeResponses(happy_script())
        app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None,
                         wonder_reader=None, sam_reader=None, research_tools=tools, research_reader=reader)
        return TestClient(app).post("/api/runs/new", json={"mode": "research", "companies": ["PTGX"],
                                                           "as_of": "2026-11-02"}).json()["id"]
    rid = go()
    r = run_json(runs_dir, rid)["run"]["research"]
    assert fake.probes == 1 and fake.requests == []                       # refused once, never retried
    assert r["reviewer_access"]["refused"] is True and r["reviewer_access"]["http_status"] == 403
    assert "does not have permission" in r["reviewer_access"]["message"]
    e = r["review"]["PTGX"]
    assert e["reviewer"] == "Muse Spark Open" and e["multi_family"]["met"] is False
    assert "same model family as the researcher" in e["label"] and "independently" not in e["label"]


def test_multi_family_001_passes_when_claude_ran(runs_dir):
    fake, rid, run = _run(runs_dir, [_assess, message([tool_use("finish_review", {"summary": "ok"}, "t-fin")])])
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["families"]["reviewer"]["family"] == "Claude" and e["families"]["researcher"]["family"] == \
        "Muse Spark (Meta)"
    assert e["families"]["same_family"] is False and e["multi_family"]["met"] is True
    assert e["label"] == ("Agent research — independently reviewed by Claude Opus 5.5 (different model family from "
                          "the researcher) (no findings)")


def test_without_the_sdk_the_claude_reviewer_says_so_and_the_fallback_runs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setitem(sys.modules, "anthropic", None)                     # import anthropic -> ImportError
    from capture import read_table
    monkeypatch.setattr(read_table, "load_env_file", lambda *a, **k: None)
    assert rc_claude.ClaudeChat().ready() == rc_claude.NO_SDK


def test_the_reviewer_reserves_its_own_reply_allowance_on_the_gate(runs_dir):
    fake, rid, run = _run(runs_dir, [_assess, message([tool_use("finish_review", {"summary": "ok"}, "t-fin")])])
    assert rc_claude.ClaudeChat.max_tokens == 16384 == fake.requests[0]["max_tokens"]


# ------------------------------------------------------------------ CAP-001 (live review 0b1b2d2) and Manus S2-02/03

def test_one_request_is_one_attempt_and_settles_within_its_reservation(runs_dir):
    """CAP-001 (Codex ruling): no server-side fallback, so one request bills one attempt, reserved exactly; a reply
    that used its whole reply allowance still settles within the reservation."""
    def full(kw):
        m = _assess(kw)
        m["usage"] = _usage(1000, 16384)
        return m
    fake, rid, run = _run(runs_dir, [full, message([tool_use("finish_review", {"summary": "x"}, "t")])])
    r = run["run"]["research"]
    assert all("fallbacks" not in q for q in fake.requests)
    assert r["review"]["PTGX"]["status"] == "complete" and not r.get("reservation_exceeded")


@pytest.mark.parametrize("bad", [{"id": None}, {"id": ""}, {"name": None}, {"input": None}, {"input": []},
                                 {"input": False}])
def test_a_malformed_tool_use_is_never_a_clean_completion(runs_dir, bad):
    block = {**tool_use("finish_review", {"summary": "done"}, "t-fin"), **bad}
    fake, rid, run = _run(runs_dir, [message([block])])
    e = run["run"]["research"]["review"]["PTGX"]
    assert e["status"] == "incomplete" and "malformed tool call" in e["reason"] and e["clean"] is False


@pytest.mark.parametrize("bad", [{"cache_creation_input_tokens": -5}, {"input_tokens": True},
                                 {"output_tokens": "100"}, {"cache_read_input_tokens": 1.5}])
def test_an_invalid_usage_component_is_unknown_accounting_never_a_tiny_price(runs_dir, bad):
    fake, rid, run = _run(runs_dir, [_assess, message([tool_use("finish_review", {"summary": "ok"}, "t-fin")],
                                                      usage={**_usage(), **bad})])
    u = run["run"]["research"]["companies"]["PTGX"]["review_usage"]
    assert u["unpriced_calls"] == 1 and run["run"]["unpriced_calls"] >= 1


def test_absent_optional_cache_fields_are_fine():
    reply = rc_claude.parse_message(message([tool_use("finish_review", {"summary": "ok"}, "t")],
                                            usage={"input_tokens": 10, "output_tokens": 3}), rc_claude.MODEL)
    assert reply.prompt_tokens == 10 and reply.completion_tokens == 3 and reply.error is None


# ------------------------------------------------------------------ billing refusal (live 06:33 PT): no credit left

class BadRequestError(Exception):
    """The shape of anthropic.BadRequestError: status_code 400 and the API's error body."""
    status_code = 400

    def __init__(self, message, etype="invalid_request_error"):
        super().__init__(message)
        self.message = message
        self.body = {"type": "error", "error": {"type": etype, "message": message}}


CREDIT = ("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or "
          "purchase credits.")


def _fallback_run(runs_dir, fake, fallback):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from tests.test_research_review_wire import PAGE1, PAGE2, PR1, PR2
    tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1), PR2: (200, "text/html; charset=utf-8", PAGE2)})
    tools.reviewer, tools.reviewer_fallback, tools.brave = rc_claude.ClaudeChat(client=fake), fallback, F.FakeBrave()
    tools.adjudicator = F.FakeChat([F.verdict({"verdict": "unresolved", "reason": "x"})], model=F.ADJUDICATOR_MODEL)
    reader = F.FakeResponses(happy_script())
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    rid = TestClient(app).post("/api/runs/new", json={"mode": "research", "companies": ["PTGX"],
                                                      "as_of": "2026-11-02"}).json()["id"]
    return run_json(runs_dir, rid)


def test_a_credit_balance_refusal_at_the_access_check_falls_back_like_a_403(runs_dir):
    fake = FakeAnthropic([], probe=BadRequestError(CREDIT))
    fallback = F.FakeChat([F.assess_then(), F.chat_tools(("finish_review", {"summary": "x"}))], **F.FALLBACK)
    run = _fallback_run(runs_dir, fake, fallback)
    r = run["run"]["research"]
    acc = r["reviewer_access"]
    assert fake.probes == 1 and fake.requests == [] and acc["refused"] is True
    assert acc["reason"] == "Claude reviewer unavailable: API credit balance too low"
    assert "credit balance is too low" in acc["message"]
    e = r["review"]["PTGX"]
    assert e["status"] == "complete" and e["reviewer"] == "Muse Spark Open" and e["multi_family"]["met"] is False
    assert e["reviewer_choice"]["primary_unavailable"].startswith("Claude reviewer unavailable: API credit balance")
    assert "same model family as the researcher" in e["label"] and "independently" not in e["label"]
    from tools import research_smoke
    assert research_smoke.reviewer_line(r) == ("Reviewer: Muse Spark Open (fallback: Claude reviewer unavailable: "
                                               "API credit balance too low)")


def test_a_credit_balance_refusal_mid_review_keeps_the_findings(runs_dir):
    from tests.test_research_review_wire import CONDITION

    def first(kw):
        def lost(draft):
            return tool_use("report_finding", {"kind": "lost_condition", "evidence": [
                {"source_id": "src-r1-ptgx", "quote": CONDITION}], "text": "dropped", "row_revision_ids": [
                {"row_id": draft[0]["row_id"], "revision_id": 1}]}, "t-find")
        return _assess(kw, verdict="finding", before=(lost,))
    fake = FakeAnthropic([first, BadRequestError(CREDIT)])
    fallback = F.FakeChat([], **F.FALLBACK)
    run = _fallback_run(runs_dir, fake, fallback)
    e = run["run"]["research"]["review"]["PTGX"]
    row = next(x for x in run["rows"] if x.get("research"))
    assert fallback.sent == [] and e["status"] == "incomplete" and e["reviewer"] == "Claude Opus 5.5"
    assert "mid-review (Claude reviewer unavailable: API credit balance too low)" in e["reason"]
    assert [f["kind"] for f in row["research"]["review_findings"] if f["current"]] == ["lost_condition"]
    assert run["run"]["research"]["reviewer_access"]["reason"].endswith("credit balance too low")


@pytest.mark.parametrize("err,expected", [
    (BadRequestError(CREDIT), True),
    (BadRequestError("max_tokens: Field required"), False),                 # an ordinary bad request
    (BadRequestError(CREDIT, etype="overloaded_error"), False),             # the message alone is not enough
    (PermissionDeniedError(CREDIT), False),                                 # 403 has its own rule
])
def test_only_anthropics_credit_balance_400_is_a_billing_refusal(err, expected):
    assert rc_claude.billing_refusal(err) is expected
