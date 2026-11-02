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


def run(replies, search=None, fetch=None, gate=None, **kw):
    gate = gate or FakeGate()
    model = FakeModel(replies)
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
    assert rec["settings"] == {"max_turns": None, "max_searches": None, "max_fetches": None}
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
                       ChatReply("muse-wonder-1.0", content=content, prompt_tokens=50, completion_tokens=20)])
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
