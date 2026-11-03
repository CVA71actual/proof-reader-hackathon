"""Fakes for the research loop tests (RESEARCHER-FULL-001 S1): a scripted Responses API, a fetcher that serves fixed
bytes, and an offline HTML renderer. No model, no network, no Chrome, no key."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pymupdf

from capture import research_sources as rs
from capture import responses_reader as rr

PUBLIC_IP = "93.184.216.34"


def public(host):
    return ["127.0.0.1"] if host in ("loopback.test",) else [PUBLIC_IP]


def usage(i=1000, o=200, reasoning=50):
    return {"input_tokens": i, "output_tokens": o, "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": i + o}


def call(name, args, call_id):
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name,
            "arguments": json.dumps(args), "status": "completed"}


def search(query, urls, sid="ws_1"):
    return {"type": "web_search_call", "id": sid, "status": "completed", "action": {"type": "search", "query": query},
            "results": [{"type": "text_result", "title": u, "url": u, "snippet": "lead"} for u in urls]}


def reasoning(n=1):
    return {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"ENC{n}"}


def message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}


def body(output, u=None, status="completed", rid="resp", model="muse-spark-1.3", incomplete=None):
    b = {"id": rid, "object": "response", "status": status, "model": model, "output": output,
         "usage": usage() if u is None else u}
    if incomplete:
        b["incomplete_details"] = {"reason": incomplete}
    return b


class FakeResponses:
    """A scripted Responses API. `script`: a list of response bodies (dicts), ResponsesReply objects, or callables
    (request body -> one of those). Every request is recorded; running out of script is a test failure."""

    model = rr.MODEL
    name = "Muse Spark"

    def __init__(self, script, key=True):
        self.script = list(script)
        self.requests = []
        self.key = key

    def ready(self):
        return None if self.key else rr.NO_KEY

    def respond(self, req):
        self.requests.append(copy.deepcopy(req))
        if not self.script:
            raise AssertionError("The research loop made more calls than the test scripted.")
        item = self.script.pop(0)
        if callable(item):
            item = item(req)
        if isinstance(item, rr.ResponsesReply):
            return item
        return rr.parse_response(item, req["model"], rr.request_size(req))


class PerCompany:
    """Route each request to a per-ticker script by the company card in its input."""

    model = rr.MODEL
    name = "Muse Spark"

    def __init__(self, scripts: dict):
        self.readers = {t: FakeResponses(s) for t, s in scripts.items()}
        self.requests = []

    def ready(self):
        return None

    def respond(self, req):
        self.requests.append(req)
        text = json.dumps(req["input"])
        for t, r in self.readers.items():
            if f'\\"ticker\\": \\"{t}\\"' in text:
                return r.respond(req)
        raise AssertionError("no script for this company")


class FakeFetcher:
    """Serves fixed answers by URL; records every URL asked. resolve() says every host is public (no DNS)."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.asked = []

    def resolve(self, host):
        return public(host)

    def get(self, url):
        self.asked.append(url)
        rs.check_url(url, self.resolve)
        got = self.pages.get(url)
        if got is None:
            return rs.Fetched(url, url, 404, "text/html", b"", "2026-11-02T18:00:00+00:00")
        if isinstance(got, Exception):
            raise got
        status, ctype, data = got
        return rs.Fetched(url, url, status, ctype, data, "2026-11-02T18:00:00+00:00")


def offline_render(html_path: Path, pdf_path: Path) -> str:
    """The research renderer's contract without Chrome: sanitize, then print the visible text as a code-written PDF."""
    snapshot, counts = rs.sanitize_html(html_path.read_text())
    text = re.sub(r"<[^>]+>", "\n", snapshot)
    text = "\n".join(" ".join(l.split()) for l in text.splitlines() if l.strip())
    pdf_path.write_bytes(rs.render_text_pdf(text, "Rendered page"))
    (pdf_path.parent / (pdf_path.name + ".limitations.json")).write_text(
        json.dumps({"removed": counts, "limitations": rs.render_limitations(counts)}))
    return "offline test renderer (sanitized snapshot)"


def html(*paras, title="Example release") -> bytes:
    return ("<html><head><title>" + title + "</title></head><body>"
            + "".join(f"<p>{p}</p>" for p in paras) + "</body></html>").encode()


def pdf_with(lines, *, tiny=(), off_page=()) -> bytes:
    """A PDF page with normal 11pt lines, optional 1pt (hidden) lines and text placed past the right edge."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 72
    for line in lines:
        page.insert_text((54, y), line, fontsize=11)
        y += 16
    for line in tiny:
        page.insert_text((54, y), line, fontsize=1)
        y += 4
    for line in off_page:
        page.insert_text((640, 400), line, fontsize=11)
    out = doc.tobytes()
    doc.close()
    return out


def tools(pages, resolve=("PTGX", "1377121", "Protagonist Therapeutics, Inc.")):
    from runs.research import ResearchTools
    ident = {resolve[0]: (resolve[1], resolve[2])}

    def res(t):
        if t not in ident:
            raise KeyError(t)
        return ident[t]
    return ResearchTools(fetcher=FakeFetcher(pages), render_html=offline_render, resolve=res,
                         brave=FakeBrave(key=False), reviewer=FakeChat([], key=False, model=REVIEWER_MODEL),
                         adjudicator=FakeChat([], key=False, model=ADJUDICATOR_MODEL))


def event(title, quote, *, source="src-r1-ptgx", page=1, date_text=None, date=None, date_end=None, program=None,
          conditions=None, spans=None, rid=None, rev=None, ticker="PTGX", type_hint="clinical_readout"):
    evidence = spans or [{"source_id": source, "page_number": page, "quote": quote, "word_ids": None,
                          "roles": ["action", "timing"]}]
    ev = {"ticker": ticker, "subject_company": None, "program": program,
            "indication": None, "title": title, "type_hint": type_hint, "action": "topline data", "modality": None,
            "date_text": date_text, "date": date, "date_end": date_end, "conditions": conditions, "relation": None,
            "relevance": None, "relevance_kind": "none", "placement_proposal": "upcoming", "primary_span_index": 0,
            "evidence": evidence, "prior_row_ids": []}
    return ev if rid is None else {"id": rid, "expected_revision": rev, "event": ev}


class FakeBrave:
    """A scripted Brave Search: answers by query; records every query. key=False: unavailable (no BRAVE_API_KEY)."""
    name = "Brave Search"

    def __init__(self, answers=None, key=True, rate_limited=False):
        self.answers, self.key, self.rate_limited, self.queries = dict(answers or {}), key, rate_limited, []

    def ready(self):
        from capture.brave_search import NO_KEY
        return None if self.key else NO_KEY

    def search(self, query, count=10):
        from capture.brave_search import BraveResult
        self.queries.append(query)
        if self.rate_limited:
            return BraveResult(query, error="Brave Search rate limit reached (HTTP 429).", http_status=429,
                               rate_limited=True)
        return BraveResult(query, results=[{"title": f"TITLE-{u}", "url": u, "snippet": f"SNIPPET-{u}"}
                                           for u in self.answers.get(query, [])], http_status=200)


REVIEWER_MODEL = "meta-models/muse-spark-open-1026"
ADJUDICATOR_MODEL = "muse-wonder-1.0"


class FakeChat:
    """A scripted chat model for the S2 reviewer (Muse Spark Open) or adjudicator (Muse Wonder): the interface of
    capture.research_reviewer.OneShotChat. `script`: ChatReply objects or callables (messages -> ChatReply). key=False:
    no key, so the stage does not run. Running out of script is a test failure."""

    def __init__(self, script, key=True, model=REVIEWER_MODEL):
        self.script, self.key, self.model, self.sent = list(script), key, model, []

    def ready(self):
        return None if self.key else f"No key was found for {self.model}, so this stage did not run."

    def chat(self, messages, tools=None, json_mode=False):
        self.sent.append(copy.deepcopy(messages))
        if not self.script:
            raise AssertionError("The reviewer made more calls than the test scripted.")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


def chat_tools(*calls, p=500, c=100, finish="tool_calls", model=REVIEWER_MODEL):
    from capture.research_reviewer import ChatReply
    return ChatReply(model, tool_calls=[{"id": f"t{i}-{n}", "name": n, "arguments": json.dumps(a)}
                                        for i, (n, a) in enumerate(calls)], finish_reason=finish,
                     prompt_tokens=p, completion_tokens=c)


def verdict(obj, p=300, c=80, finish="stop"):
    from capture.research_reviewer import ChatReply
    return ChatReply(ADJUDICATOR_MODEL, content=json.dumps(obj), finish_reason=finish, prompt_tokens=p,
                     completion_tokens=c)


def assess_then(*calls, verdict="no_issue", before=(), p=500, c=100):
    """A reviewer reply (a callable on the request) that gives every draft row a verdict, row first (A-TO-Z-001),
    then makes `calls`. `before` calls run ahead of the assessment (a report_finding the verdict refers to)."""
    def reply(messages):
        draft = json.loads(messages[1]["content"])["draft_proposals"]
        before_ = [b(draft) if callable(b) else b for b in before]
        assess = ("assess_rows", {"assessments": [{"row_id": r["row_id"], "revision_id": r["revision_id"],
                                                   "verdict": verdict} for r in draft]})
        return chat_tools(*before_, assess, *calls, p=p, c=c)
    return reply
