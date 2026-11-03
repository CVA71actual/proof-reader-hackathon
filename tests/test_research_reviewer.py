"""RESEARCHER-FULL-001 S2: the independent reviewer and the Wonder adjudicator, with fakes only (no model or paid
call). Covers each finding kind, complete vs incomplete, unreadable output -> unresolved, a cited source that was not
fetched is rejected, binding to the exact revision, and no retry."""

from __future__ import annotations

import json

import pytest

from capture import research_reviewer as rv
from capture.research_reviewer import ChatReply

SOURCES = [
    {"source_id": "S1", "url": "https://example.com/pr", "title": "Q3 release",
     "text": "The company expects to submit the NDA in   H2 2026, contingent on a Type B meeting with FDA."},
    {"source_id": "S2", "url": "https://example.com/deck", "title": "November deck",
     "text": "NDA submission now planned for potential H1 2027."},
]
PROPOSALS = [
    {"row_id": "r1", "revision_id": 2, "title": "NDA submission", "date_text": "H2 2026", "conditions": [],
     "source_ids": ["S1"], "reasoning": "SECRET RESEARCHER REASONING"},
    {"row_id": "r2", "revision_id": 1, "title": "Phase 3 readout", "date_text": "Q1 2027", "conditions": None,
     "source_ids": ["S1"]},
]
FETCHED = {"https://example.com/newer": {"source_id": "S3", "url": "https://example.com/newer",
                                         "text": "Topline data from the EXPAND trial is expected October 12."}}


class FakeModel:
    model = "meta-models/muse-spark-open-1026"

    def __init__(self, replies, ready=None):
        self.replies = list(replies)
        self.sent: list = []
        self._ready = ready

    def ready(self):
        return self._ready

    def chat(self, messages, tools=None, json_mode=False):
        self.sent.append(json.loads(json.dumps(messages)))
        if not self.replies:
            raise AssertionError("an unexpected extra model request was made")
        return self.replies.pop(0)


class FakeGate:
    def __init__(self, refuse_after: int | None = None, refuse_kind: str | None = None):
        self.events: list[dict] = []
        self.refuse_after, self.refuse_kind = refuse_after, refuse_kind
        self.reserved = 0

    def __call__(self, ev):
        self.events.append(ev)
        if ev["phase"] == "reserve":
            if self.refuse_kind in (None, ev["kind"]) and self.refuse_after is not None \
                    and self.reserved >= self.refuse_after:
                raise RuntimeError("Spending cap reached: $30.00 of $30.00.")
            self.reserved += 1
            return {"ticket": self.reserved}
        return None

    def settles(self):
        return [e for e in self.events if e["phase"] == "settle"]


def call(name, args, i=[0]):
    i[0] += 1
    return {"id": f"c{i[0]}", "name": name, "arguments": json.dumps(args) if not isinstance(args, str) else args}


def tools(*calls, p=100, c=50):
    return ChatReply(rv.reviewer_model().model, tool_calls=list(calls), finish_reason="tool_calls",
                     prompt_tokens=p, completion_tokens=c)


def finish(summary="done"):
    return call("finish_review", {"summary": summary})


def assess_all(verdict="no_issue"):
    """The row-first turn (A-TO-Z-001): every draft row gets a verdict before any search or fetch."""
    return tools(call("assess_rows", {"assessments": [{"row_id": p["row_id"], "revision_id": p["revision_id"],
                                                       "verdict": verdict} for p in PROPOSALS]}))


def with_assessment(replies):
    """Row first: the assessment rides in the first reply when that reply does more than finish (so call counts are
    unchanged); a finish-only first reply gets its own assessment turn before it."""
    replies = list(replies)
    if not replies:
        return replies
    first = replies[0]
    if isinstance(first, ChatReply) and any(c["name"] != "finish_review" for c in first.tool_calls):
        first.tool_calls = assess_all().tool_calls + list(first.tool_calls)
        return replies
    return [assess_all()] + replies


def run(replies, search=None, fetch=None, gate=None, assess_first=True, **kw):
    gate = gate or FakeGate()
    model = FakeModel(with_assessment(replies) if assess_first else list(replies))
    rec = rv.review_draft(company="MIRM", assignment="Research catalysts.", proposals=PROPOSALS, sources=SOURCES,
                          search=search or (lambda q: [{"url": "https://example.com/newer", "snippet": "EXPAND Oct 12"}]),
                          fetch_capture=fetch or (lambda url: FETCHED.get(url)), spend_gate=gate, model=model, **kw)
    return rec, model, gate


# ------------------------------------------------------------------ each finding kind

@pytest.mark.parametrize("kind,rows,evidence", [
    ("stale_guidance", [{"row_id": "r1", "revision_id": 2}],
     [{"source_id": "S2", "quote": "NDA submission now planned for potential H1 2027."}]),
    ("conflicting_guidance", [{"row_id": "r1", "revision_id": 2}],
     [{"source_id": "S1", "quote": "submit the NDA in H2 2026"},
      {"source_id": "S2", "quote": "potential H1 2027"}]),
    ("unsupported_claim", [{"row_id": "r2", "revision_id": 1}], []),
    ("lost_condition", [{"row_id": "r1", "revision_id": 2}],
     [{"source_id": "S1", "quote": "contingent on a Type B meeting with FDA"}]),
])
def test_each_row_finding_kind_is_kept_and_bound(kind, rows, evidence):
    rec, _, _ = run([tools(call("report_finding", {"kind": kind, "row_revision_ids": rows, "evidence": evidence,
                                                   "text": f"A specific {kind} problem."})),
                     tools(finish())])
    assert rec["status"] == "complete" and rec["clean"] is False
    assert [f["kind"] for f in rec["findings"]] == [kind]
    f = rec["findings"][0]
    assert f["row_revision_ids"] == rows
    assert [e["source_id"] for e in f["evidence"]] == [e["source_id"] for e in evidence]
    for e in f["evidence"]:   # offsets point into the whitespace-normalised captured text
        text = " ".join(next(s for s in SOURCES if s["source_id"] == e["source_id"])["text"].split())
        assert text[e["start"]:e["end"]] == e["quote"]
    assert rec["rejected"] == []


def test_omission_needs_no_row_but_needs_a_fetched_source():
    rec, _, _ = run([tools(call("search", {"query": "MIRM EXPAND topline"})),
                     tools(call("fetch", {"url": "https://example.com/newer"})),
                     tools(call("report_finding", {
                         "kind": "omission", "row_revision_ids": [],
                         "evidence": [{"source_id": "S3", "quote": "expected October 12"}],
                         "text": "EXPAND topline (Oct 12) is missing from the draft."})),
                     tools(finish())])
    assert rec["status"] == "complete"
    assert rec["findings"][0]["kind"] == "omission" and rec["findings"][0]["row_revision_ids"] == []
    assert rec["reviewed"]["sources"] == {"given": ["S1", "S2"], "fetched": ["S3"]}
    assert rec["searches"] == [{"query": "MIRM EXPAND topline", "status": "ok", "results": 1}]
    assert rec["fetches"][0]["status"] == "captured"


def test_unknown_kind_and_rowless_attack_are_rejected():
    rec, _, _ = run([tools(call("report_finding", {"kind": "vibes", "row_revision_ids": [], "evidence": [],
                                                   "text": "x"}),
                           call("report_finding", {"kind": "stale_guidance", "row_revision_ids": [],
                                                   "evidence": [{"source_id": "S2", "quote": "potential H1 2027"}],
                                                   "text": "stale"})),
                     tools(finish())])
    assert rec["findings"] == [] and len(rec["rejected"]) == 2
    # complete, but the rejected attempts stay visible and the review is not called clean
    assert rec["status"] == "complete" and rec["clean"] is False


# ------------------------------------------------------------------ evidence must be fetched, snippets are leads

def test_cited_source_seen_only_in_search_is_rejected_as_evidence():
    rec, model, _ = run([tools(call("search", {"query": "EXPAND"})),
                         tools(call("report_finding", {
                             "kind": "omission", "row_revision_ids": [],
                             "evidence": [{"source_id": "https://example.com/newer", "quote": "EXPAND Oct 12"}],
                             "text": "EXPAND missing."})),
                         tools(finish())])
    assert rec["findings"] == []
    assert "not a captured source" in rec["rejected"][0]["problems"][0]
    fed_back = json.loads(model.sent[2][-1]["content"])      # the rejection went back to the model as a tool result
    assert fed_back["status"] == "rejected"


def test_quote_not_in_captured_text_is_rejected():
    rec, _, _ = run([tools(call("report_finding", {
        "kind": "lost_condition", "row_revision_ids": [{"row_id": "r1", "revision_id": 2}],
        "evidence": [{"source_id": "S1", "quote": "subject to approval by the board"}], "text": "lost"})),
        tools(finish())])
    assert rec["findings"] == [] and "not in the captured text" in rec["rejected"][0]["problems"][0]


def test_failed_capture_cannot_be_cited():
    def broken(url):
        raise OSError("blocked")
    rec, _, _ = run([tools(call("fetch", {"url": "https://example.com/newer"})),
                     tools(call("report_finding", {"kind": "omission", "row_revision_ids": [],
                                                   "evidence": [{"source_id": "S3", "quote": "October 12"}],
                                                   "text": "missing"})),
                     tools(finish())], fetch=broken)
    assert rec["fetches"] == [{"url": "https://example.com/newer", "status": "error", "error": "OSError"}]
    assert rec["findings"] == [] and rec["rejected"]


# ------------------------------------------------------------------ binding to the revision

def test_finding_on_another_revision_is_rejected():
    rec, _, _ = run([tools(call("report_finding", {
        "kind": "stale_guidance", "row_revision_ids": [{"row_id": "r1", "revision_id": 1}],
        "evidence": [{"source_id": "S2", "quote": "potential H1 2027"}], "text": "stale"}),
        call("report_finding", {
            "kind": "stale_guidance", "row_revision_ids": [{"row_id": "r9", "revision_id": 1}],
            "evidence": [{"source_id": "S2", "quote": "potential H1 2027"}], "text": "stale"})),
        tools(finish())])
    assert rec["findings"] == []
    assert "the reviewed revision is 2" in rec["rejected"][0]["problems"][0]
    assert "not in this draft" in rec["rejected"][1]["problems"][0]


def test_revision_ids_as_strings_bind_to_the_given_revision_and_record_what_was_reviewed():
    rec, _, _ = run([tools(call("report_finding", {
        "kind": "stale_guidance", "row_revision_ids": [{"row_id": "r1", "revision_id": "2"}],
        "evidence": [{"source_id": "S2", "quote": "potential H1 2027"}], "text": "stale"})),
        tools(finish())])
    assert rec["findings"][0]["row_revision_ids"] == [{"row_id": "r1", "revision_id": 2}]
    assert rec["reviewed"]["rows"] == [{"row_id": "r1", "revision_id": 2}, {"row_id": "r2", "revision_id": 1}]


def test_reviewer_never_sees_researcher_reasoning():
    _, model, _ = run([tools(finish())])
    assert "SECRET RESEARCHER REASONING" not in json.dumps(model.sent[0])


# ------------------------------------------------------------------ complete versus incomplete

def test_complete_with_no_findings_is_clean():
    rec, _, _ = run([tools(finish("Nothing material missing."))])
    assert rec["status"] == "complete" and rec["clean"] is True and rec["findings"] == []
    assert rec["summary"] == "Nothing material missing."
    assert rec["model"] == "meta-models/muse-spark-open-1026" and rec["provider"] == "baseten"


@pytest.mark.parametrize("reply,why", [
    (ChatReply("m", content="Looks fine to me.", finish_reason="stop"), "without calling finish_review"),
    (ChatReply("m", content=None, finish_reason="length"), "cut off"),
    (ChatReply("m", error="The model call did not succeed (BadRequestError, status 400).", http_status=400,
               may_have_been_billed=True), "status 400"),
    (ChatReply("m", error="The Muse Spark Open service did not answer in time.", timed_out=True,
               may_have_been_billed=True), "did not answer"),
])
def test_empty_or_failed_review_is_incomplete_never_clean(reply, why):
    rec, model, _ = run([reply])
    assert rec["status"] == "incomplete" and rec["clean"] is False and why in rec["reason"]
    assert model.replies == []           # exactly one request: no retry


def test_findings_before_a_failure_are_kept_but_review_is_incomplete():
    rec, _, _ = run([tools(call("report_finding", {
        "kind": "lost_condition", "row_revision_ids": [{"row_id": "r1", "revision_id": 2}],
        "evidence": [{"source_id": "S1", "quote": "contingent on a Type B meeting"}], "text": "lost"})),
        ChatReply("m", content="", finish_reason="stop")])
    assert rec["status"] == "incomplete" and len(rec["findings"]) == 1 and rec["clean"] is False


def test_unreadable_tool_arguments_are_fed_back_and_recorded():
    rec, _, _ = run([tools(call("report_finding", "{not json")), tools(finish())])
    assert rec["rejected"][0]["problems"] == ["The tool arguments were not a readable JSON object."]


def test_no_key_means_incomplete_and_no_call():
    gate = FakeGate()
    rec = rv.review_draft(company="X", assignment="a", proposals=PROPOSALS, sources=SOURCES, search=lambda q: [],
                          fetch_capture=lambda u: None, spend_gate=gate, model=FakeModel([], ready="No key."))
    assert rec["status"] == "incomplete" and rec["reason"] == "No key." and gate.events == []


def test_malformed_success_envelope_is_a_possibly_billed_error():
    r = rv.parse_chat_response({"model": "m", "choices": [], "usage": {"prompt_tokens": 7}}, "req")
    assert r.error and r.may_have_been_billed and r.prompt_tokens == 7 and r.completion_tokens is None
    r = rv.parse_chat_response([], "req")
    assert r.error and r.may_have_been_billed and r.prompt_tokens is None


# ------------------------------------------------------------------ accounting and limits

def test_every_model_call_and_search_goes_through_the_gate_with_usage():
    rec, _, gate = run([tools(call("search", {"query": "q"}), p=10, c=5),
                        tools(finish(), p=None, c=8)],
                       search=lambda q: {"results": [{"url": "u"}], "searches": None})
    kinds = [(e["phase"], e["kind"]) for e in gate.events]
    assert kinds == [("reserve", "model"), ("settle", "model"), ("reserve", "search"), ("settle", "search"),
                     ("reserve", "model"), ("settle", "model")]
    assert all(e["max_output_tokens"] for e in gate.events if e["phase"] == "reserve" and e["kind"] == "model")
    settles = gate.settles()
    assert settles[0]["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "searches": 0}
    assert settles[1]["usage"]["searches"] is None                 # unknown search usage stays unknown
    assert settles[2]["usage"]["prompt_tokens"] is None            # unknown token count stays unknown, never 0
    u = rec["usage"]
    assert u["prompt_tokens"] is None and u["prompt_tokens_unknown_requests"] == 1
    assert u["completion_tokens"] == 13 and u["searches"] is None and u["model_requests"] == 2


def test_gate_refusal_stops_incomplete_with_its_reason():
    rec, model, _ = run([tools(call("search", {"query": "q"})), tools(finish())], gate=FakeGate(refuse_after=1))
    assert rec["status"] == "incomplete" and "Spending cap reached" in rec["reason"]
    assert len(model.sent) == 1


def test_gate_falsy_ticket_refuses():
    rec, model, _ = run([tools(finish())], gate=lambda ev: None)
    assert rec["status"] == "incomplete" and "refused" in rec["reason"] and model.sent == []


def test_no_limits_by_default_and_optional_turn_limit():
    replies = [tools(call("search", {"query": f"q{i}"})) for i in range(30)] + [tools(finish())]
    rec, _, _ = run(replies)
    assert rec["status"] == "complete" and rec["turns"] == 31
    assert {k: rec["settings"][k] for k in ("max_turns", "max_searches", "max_fetches")} == \
        {"max_turns": None, "max_searches": None, "max_fetches": None}
    assert rec["settings"]["pages_per_read"] == rv.PAGES_PER_READ and rec["settings"]["replay_turns"] == rv.REPLAY_TURNS
    rec, _, _ = run([tools(call("search", {"query": "q"}))] * 3 + [tools(finish())], max_turns=2)
    assert rec["status"] == "incomplete" and "turn limit" in rec["reason"]


def test_one_shot_chat_does_not_retry_a_400(monkeypatch):
    class Err(Exception):
        status_code = 400

    class Client:
        n = 0

        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    Client.n += 1
                    raise Err()

    monkeypatch.setattr(rv.http, "openai_client", lambda base, key: Client)
    m = rv.OneShotChat("Muse Spark Open", "https://x", "m", "NO_SUCH_KEY_ENV")
    r = m.chat([{"role": "user", "content": "hi"}])
    assert Client.n == 1 and r.http_status == 400 and r.may_have_been_billed and "status 400" in r.error


def test_one_shot_chat_does_not_retry_a_timeout(monkeypatch):
    import httpx

    class Client:
        n = 0

        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    Client.n += 1
                    raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(rv.http, "openai_client", lambda base, key: Client)
    r = rv.OneShotChat("Muse Wonder", "https://x", "m", "NO_SUCH_KEY_ENV").chat([])
    assert Client.n == 1 and r.timed_out and r.may_have_been_billed


# ------------------------------------------------------------------ adjudication

DISPUTE = {"dispute_id": "D1", "row_revision_ids": [{"row_id": "r1", "revision_id": 2}], "component": "timing",
           "readings": [{"by": "researcher", "text": "H2 2026"}, {"by": "reviewer", "text": "potential H1 2027"}],
           "evidence": [{"source_id": "S1", "quote": "H2 2026"}, {"source_id": "S2", "quote": "H1 2027"}]}


def adj(content, disputes=None, gate=None, **kw):
    model = FakeModel([content if isinstance(content, ChatReply) else
                       ChatReply("muse-wonder-1.0", content=content, finish_reason="stop", prompt_tokens=50,
                                 completion_tokens=20)])
    rec = rv.adjudicate(disputes=disputes or [DISPUTE], sources=SOURCES, spend_gate=gate or FakeGate(),
                        model=model, **kw)
    return rec, model


def test_adjudication_resolution_is_a_proposal_never_human_acceptance():
    rec, model = adj(json.dumps({"verdict": "resolved", "reading": 1, "resolution": "potential H1 2027",
                                 "evidence": [{"source_id": "S2", "quote": "now planned for potential H1 2027"}],
                                 "reason": "newer deck"}))
    o = rec["outcomes"][0]
    assert o["outcome"] == "proposed_resolution" and o["human_accepted"] is False
    assert o["evidence"][0]["source_id"] == "S2"
    sent = json.loads(model.sent[0][1]["content"])
    assert {s["source_id"] for s in sent["captured_sources"]} == {"S1", "S2"}


@pytest.mark.parametrize("content,why", [
    ("not json at all", "could not be read"),
    ("[1, 2]", "could not be read"),
    (json.dumps({"verdict": "probably"}), "neither resolved nor unresolved"),
    (json.dumps({"verdict": "resolved", "reading": 1, "resolution": "x", "evidence": []}), "no captured evidence"),
    (json.dumps({"verdict": "resolved", "reading": 5, "resolution": "x",
                 "evidence": [{"source_id": "S2", "quote": "H1 2027"}]}), "does not exist"),
    (json.dumps({"verdict": "resolved", "reading": 1, "resolution": "x",
                 "evidence": [{"source_id": "S9", "quote": "H1 2027"}]}), "not among the captured"),
    (json.dumps({"verdict": "resolved", "reading": 1, "resolution": "x",
                 "evidence": [{"source_id": "S2", "quote": "Q4 2028"}]}), "not in the captured text"),
    (json.dumps({"verdict": "unresolved", "reason": "sources disagree"}), "sources disagree"),
    (ChatReply("muse-wonder-1.0", error="The model call did not succeed (UnprocessableEntityError, status 422).",
               http_status=422, may_have_been_billed=True), "status 422"),
])
def test_adjudication_fails_closed(content, why):
    rec, model = adj(content)
    o = rec["outcomes"][0]
    assert o["outcome"] == "unresolved" and why in o["reason"] and o["human_accepted"] is False
    assert model.replies == []          # one request per dispute, no retry


def test_only_real_disputes_are_sent():
    rec, model = adj(json.dumps({"verdict": "unresolved", "reason": "r"}),
                     disputes=[{"dispute_id": "D0", "readings": [{"by": "a", "text": "x"}]}, DISPUTE])
    assert [s["dispute_id"] for s in rec["skipped"]] == ["D0"] and len(model.sent) == 1


def test_adjudication_gate_refusal_leaves_disputes_unresolved():
    rec, model = adj("{}", gate=FakeGate(refuse_after=0))
    assert rec["status"] == "incomplete" and rec["outcomes"][0]["outcome"] == "unresolved"
    assert model.sent == []


def test_adjudication_usage_and_optional_call_limit():
    rec, _ = adj(json.dumps({"verdict": "unresolved", "reason": "r"}), disputes=[DISPUTE, {**DISPUTE, "dispute_id": "D2"}],
                 max_calls=1)
    assert [o["outcome"] for o in rec["outcomes"]] == ["unresolved", "unresolved"]
    assert "call limit" in rec["outcomes"][1]["reason"] and rec["status"] == "incomplete"
    assert rec["usage"]["prompt_tokens"] == 50 and rec["usage"]["model_requests"] == 1


# ------------------------------------------------------------------ Manus 15:12 PT: truncation, retrieval, envelope

def test_truncated_reply_with_parseable_finish_is_incomplete_and_earlier_findings_kept():
    lost = call("report_finding", {"kind": "lost_condition", "row_revision_ids": [{"row_id": "r1", "revision_id": 2}],
                                   "evidence": [{"source_id": "S1", "quote": "contingent on a Type B meeting"}],
                                   "text": "lost"})
    cut = tools(call("report_finding", {"kind": "unsupported_claim",
                                        "row_revision_ids": [{"row_id": "r2", "revision_id": 1}],
                                        "evidence": [], "text": "in the cut-off reply"}), finish())
    cut.finish_reason = "length"
    rec, model, gate = run([tools(lost), cut])
    assert rec["status"] == "incomplete" and rec["clean"] is False and "cut off" in rec["reason"]
    assert [f["text"] for f in rec["findings"]] == ["lost"]          # earlier finding kept, cut-off one not used
    assert rec["summary"] is None and model.replies == []           # no retry
    assert [e["phase"] for e in gate.events] == ["reserve", "settle"] * 2


def test_truncated_but_readable_verdict_stays_unresolved():
    reply = ChatReply("muse-wonder-1.0", finish_reason="length", prompt_tokens=50, completion_tokens=12288,
                      content=json.dumps({"verdict": "resolved", "reading": 1, "resolution": "potential H1 2027",
                                          "evidence": [{"source_id": "S2", "quote": "potential H1 2027"}]}))
    rec, model = adj(reply)
    o = rec["outcomes"][0]
    assert o["outcome"] == "unresolved" and "cut off" in o["reason"] and o["human_accepted"] is False
    assert rec["status"] == "incomplete" and model.replies == []


def test_failed_search_then_finish_is_not_clean():
    def broken(q):
        raise TimeoutError("slow")
    rec, _, _ = run([tools(call("search", {"query": "MIRM omission search"})), tools(finish("No findings."))],
                    search=broken)
    assert rec["status"] == "complete" and rec["clean"] is False
    assert rec["limitations"] == [{"kind": "search", "query": "MIRM omission search",
                                   "reason": "The search did not succeed (TimeoutError)."}]
    assert rec["reviewed"]["not_reviewed"] == rec["not_reviewed"] and "not reviewed" in rec["reason"]


def test_uncaptured_fetch_then_finish_is_not_clean():
    rec, _, _ = run([tools(call("fetch", {"url": "https://public.example/missing"})), tools(finish("No findings."))])
    assert rec["clean"] is False
    assert rec["limitations"] == [{"kind": "fetch", "url": "https://public.example/missing",
                                   "reason": "The source could not be captured."}]


def test_retrieval_failure_covered_later_by_the_same_capture_is_clean():
    attempts = []

    def flaky(url):
        attempts.append(url)
        return None if len(attempts) == 1 else FETCHED["https://example.com/newer"]
    rec, _, _ = run([tools(call("fetch", {"url": "https://example.com/newer"})),
                     tools(call("fetch", {"url": "https://example.com/newer"})),
                     tools(call("nothing_material", {"source_id": "S3", "reason": "no new event"})),
                     tools(finish())], fetch=flaky)
    assert rec["limitations"] == [] and rec["clean"] is True
    assert len(attempts) == 2           # the model asked twice; the module itself never retries


def test_wrong_shaped_tool_calls_is_a_billed_failure_settled_once():
    env = {"model": "fake-open", "choices": [{"message": {"content": None, "tool_calls": 1},
                                              "finish_reason": "tool_calls"}],
           "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    r = rv.parse_chat_response(env, "req")
    assert r.error and r.may_have_been_billed and (r.prompt_tokens, r.completion_tokens) == (100, 50)
    bad_item = {"model": "m", "choices": [{"message": {"tool_calls": [{"id": "c1", "function": 3}]}}]}
    assert rv.parse_chat_response(bad_item, "req").error

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return env

    class Model(rv.OneShotChat):
        def ready(self):
            return None
    import capture.research_reviewer as mod
    orig = mod.http.openai_client
    mod.http.openai_client = lambda base, key: Client
    try:
        gate = FakeGate()
        rec = rv.review_draft(company="X", assignment="a", proposals=PROPOSALS, sources=SOURCES,
                              search=lambda q: [], fetch_capture=lambda u: None, spend_gate=gate,
                              model=Model("Muse Spark Open", "https://x", "m", "NO_SUCH_KEY_ENV"))
    finally:
        mod.http.openai_client = orig
    assert [e["phase"] for e in gate.events] == ["reserve", "settle"]
    s = gate.settles()[0]
    assert s["usage"]["prompt_tokens"] == 100 and s["usage"]["completion_tokens"] == 50
    assert s["may_have_been_billed"] is True and s["error"]
    assert rec["status"] == "incomplete" and rec["clean"] is False and rec["turns"] == 1


def test_injected_model_that_raises_is_still_settled_once():
    class Raises(FakeModel):
        def chat(self, *a, **k):
            raise TypeError("bad envelope")
    gate = FakeGate()
    rec = rv.review_draft(company="X", assignment="a", proposals=PROPOSALS, sources=SOURCES, search=lambda q: [],
                          fetch_capture=lambda u: None, spend_gate=gate, model=Raises([]))
    assert [e["phase"] for e in gate.events] == ["reserve", "settle"]
    assert gate.settles()[0]["may_have_been_billed"] is True and rec["status"] == "incomplete"


# ------------------------------------------------------------------ Manus section 4: an empty draft is still reviewed

def _review(proposals, replies, gate=None):
    gate = gate or FakeGate()
    model = FakeModel(replies)
    rec = rv.review_draft(company="MIRM", assignment="Research catalysts.", proposals=proposals, sources=SOURCES,
                          search=lambda q: [{"url": "https://example.com/newer"}],
                          fetch_capture=lambda url: FETCHED.get(url), spend_gate=gate, model=model)
    return rec, model, gate


def test_empty_draft_gets_the_rowless_omission_review():
    rec, model, _ = _review([], [
        tools(call("search", {"query": "MIRM upcoming"})),
        tools(call("fetch", {"url": "https://example.com/newer"})),
        tools(call("report_finding", {"kind": "omission", "row_revision_ids": [],
                                      "evidence": [{"source_id": "S3", "quote": "expected October 12"}],
                                      "text": "EXPAND topline Oct 12 is missing."}),
              call("report_finding", {"kind": "stale_guidance", "row_revision_ids": [{"row_id": "r1", "revision_id": 2}],
                                      "evidence": [{"source_id": "S2", "quote": "potential H1 2027"}],
                                      "text": "names a row that does not exist"})),
        tools(finish())])
    assert rec["status"] == "complete" and [f["kind"] for f in rec["findings"]] == ["omission"]
    assert rec["reviewed"]["rows"] == [] and len(rec["rejected"]) == 1      # no placeholder row was created
    sent = json.loads(model.sent[0][1]["content"])
    assert sent["draft_proposals"] == [] and {s["source_id"] for s in sent["captured_sources"]} == {"S1", "S2"}


def test_empty_draft_complete_with_no_findings_is_clean():
    rec, _, _ = _review([], [tools(finish())])
    assert rec["status"] == "complete" and rec["clean"] is True


@pytest.mark.parametrize("bad", [[{"title": "no ids"}], [PROPOSALS[0], {"row_id": "r3"}], "not a list", None])
def test_malformed_draft_is_incomplete_before_any_call(bad):
    rec, model, gate = _review(bad, [])
    assert rec["status"] == "incomplete" and rec["clean"] is False and gate.events == [] and model.sent == []


# ------------------------------------------------------------------ Red Team PR174_7f943a1: finish must come alone

def test_finish_beside_a_search_is_ignored_until_issued_alone():
    rec, model, _ = run([tools(call("search", {"query": "MIRM pipeline"}), finish("No findings.")),
                         tools(finish("Read the search results; nothing material."))])
    assert len(model.sent) == 2                                   # the search result went back before finishing
    fed = [json.loads(m["content"]) for m in model.sent[1] if m["role"] == "tool"]
    assert fed[-2]["status"] == "ok" and fed[-1]["status"] == "ignored"       # (fed[0] is the row assessment)
    assert rec["ignored_finishes"] == [{"turn": 1, "reason": rv.FINISH_NOT_ALONE}]
    assert rec["status"] == "complete" and rec["summary"] == "Read the search results; nothing material."


def test_finish_beside_other_calls_then_stop_is_incomplete():
    rec, _, _ = run([tools(call("search", {"query": "q"}), finish()),
                     ChatReply("m", content="done", finish_reason="stop")])
    assert rec["status"] == "incomplete" and rec["clean"] is False and rec["ignored_finishes"]


# ------------------------------------------------------------------ Manus 17:07 PT s.2: classify before completion

@pytest.mark.parametrize("bad", [None, {}, {"results": None}, {"results": "x"}, 7,
                                 {"results": [], "status": "in_progress"}])
def test_malformed_search_after_a_timeout_stays_a_limitation_end_to_end(bad):
    answers = [TimeoutError("slow"), bad]

    def search(q):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    rec, model, gate = run([tools(call("search", {"query": "MIRM omission"})),
                            tools(call("search", {"query": "MIRM omission"})),
                            tools(finish("No findings."))], search=search)
    assert rec["status"] == "complete" and rec["clean"] is False
    assert rec["limitations"][0]["kind"] == "search" and rec["limitations"][0]["query"] == "MIRM omission"
    assert [s["status"] for s in rec["searches"]] == ["error", "error"]
    fed = json.loads(model.sent[2][-1]["content"])           # the model was told the second search failed
    assert fed["status"] == "error"
    search_settles = [e for e in gate.settles() if e["kind"] == "search"]
    assert len(search_settles) == 2 and all(e["error"] and e["may_have_been_billed"] for e in search_settles)


@pytest.mark.parametrize("good", [[], {"results": []}, {"results": [{"url": "u"}], "status": "completed", "searches": 1}])
def test_valid_search_after_a_timeout_clears_the_limitation_end_to_end(good):
    answers = [TimeoutError("slow"), good]

    def search(q):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    rec, _, _ = run([tools(call("search", {"query": "q"})), tools(call("search", {"query": "q"})),
                     tools(finish())], search=search)
    assert rec["limitations"] == [] and rec["clean"] is True and rec["searches"][-1]["status"] == "ok"


@pytest.mark.parametrize("reason", [None, "content_filter", "function_call", "weird"])
def test_unsupported_finish_reason_is_incomplete_and_its_finish_unused(reason):
    r = tools(finish("No findings."))
    r.finish_reason = reason
    rec, model, gate = run([r], assess_first=False)
    assert rec["status"] == "incomplete" and rec["clean"] is False and rec["summary"] is None
    assert repr(reason) in rec["reason"] and model.replies == []
    assert [e["phase"] for e in gate.events] == ["reserve", "settle"]


@pytest.mark.parametrize("reason", [None, "content_filter", "tool_calls"])
def test_unsupported_verdict_finish_reason_stays_unresolved(reason):
    rec, _ = adj(ChatReply("muse-wonder-1.0", finish_reason=reason, prompt_tokens=5, completion_tokens=5,
                           content=json.dumps({"verdict": "resolved", "reading": 1, "resolution": "potential H1 2027",
                                               "evidence": [{"source_id": "S2", "quote": "potential H1 2027"}]})))
    assert rec["outcomes"][0]["outcome"] == "unresolved" and rec["status"] == "incomplete"


def test_parse_keeps_finish_reason_as_given_and_drops_non_strings():
    env = {"model": "m", "choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}]}
    assert rv.parse_chat_response(env, "r").finish_reason == "content_filter"
    env["choices"][0]["finish_reason"] = 5
    assert rv.parse_chat_response(env, "r").finish_reason is None


# ------------------------------------------------------------------ Red Team PR174_8d822c0: unfinished count is unknown

@pytest.mark.parametrize("count", [0, 3, None])
@pytest.mark.parametrize("status", ["in_progress", "searching", "failed"])
def test_unfinished_search_settles_unknown_usage_whatever_count_it_reports(status, count):
    rec, _, gate = run([tools(call("search", {"query": "q"})), tools(finish())],
                       search=lambda q: {"results": [], "status": status, "searches": count})
    s = [e for e in gate.settles() if e["kind"] == "search"]
    assert len(s) == 1 and s[0]["usage"]["searches"] is None
    assert s[0]["error"] and s[0]["may_have_been_billed"] is True
    assert rec["usage"]["searches"] is None and rec["usage"]["searches_unknown_requests"] == 1
    assert rec["clean"] is False and rec["limitations"][0]["query"] == "q"


def test_completed_search_keeps_its_reported_count():
    _, _, gate = run([tools(call("search", {"query": "q"})), tools(finish())],
                     search=lambda q: {"results": [], "status": "completed", "searches": 3})
    s = [e for e in gate.settles() if e["kind"] == "search"][0]
    assert s["usage"]["searches"] == 3 and s["error"] is None
