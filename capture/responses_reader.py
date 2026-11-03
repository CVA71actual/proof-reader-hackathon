"""The Responses API call path for Muse Spark 1.3 (RESEARCHER-FULL-001 S1): built-in web_search plus our function tools.

One request is one call to POST {BASE_URL}/responses. Nothing here decides cost or whether a call may be made: the
research job (runs/research.py) reserves the call's bounded worst case on the run's spend gate before it calls, and
prices the reply afterwards with runs/prices.py from the usage the reply carries.

What a request carries (build_request):
    model                 muse-spark-1.3
    instructions          the compact research controller (runs/research_contract.CONTROLLER), never the 3,996-character
                          page brief as a system message (run 15 lost 10 of 14 calls to reasoning tokens)
    input                 the person's research assignment, exactly, as its own user item; the company card; then the
                          replayed output items of every earlier response and our function_call_output items
    tools                 [{"type": "web_search"}] while the company has searches left, plus the flat function tools
    tool_choice           "auto" (the only value the API accepts)
    parallel_tool_calls   false: one function call per turn, so every step is gated and logged on its own
    max_output_tokens     a code-set budget (the worst case reserved on the gate)
    max_tool_calls        the built-in search allowance left for this request (omitted with web_search when it is 0)
    include               ["web_search_call.results", "reasoning.encrypted_content"]
    store                 false with replay (the default), true with previous_response_id

Continuation: CONTINUE_REPLAY sends the whole history again (every output item of every response, unchanged, before the
function_call_output items that answer its calls); CONTINUE_PREVIOUS sends previous_response_id and only the new items.

A 400 or 422 is never retried, and no other status is retried either: one request, one auditable attempt. A request
that got no answer (a timeout or a dropped connection after it was sent) may have been billed: its reply says so and the
job charges the reserved worst case. The key is read from MODEL_API_KEY (or ~/.config/proof-reader/env) and is never
printed, logged, saved or put in a reply.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from capture import http, read_table

MODEL = read_table.MODEL
BASE_URL = read_table.BASE_URL
NAME = "Muse Spark"

CONTINUE_REPLAY = "replay"
CONTINUE_PREVIOUS = "previous_response_id"
INCLUDE = ["web_search_call.results", "reasoning.encrypted_content"]
# A research turn's own output budget (LIMITS-002, CVA 2026-11-02: the model's documented maximum). Muse Spark reasons
# before it answers; reasoning tokens count against this. No maximum output is documented for muse-spark-1.3: checked
# https://dev.meta.ai/docs/models (context window 1,048,576 tokens, no output maximum stated),
# https://dev.meta.ai/docs/api-reference/responses/create-response and https://dev.meta.ai/docs/pricing-rate-limits
# (rate limits only) on 2026-11-02. So 32,768, as CVA directed when none is documented. The strict reservation scales
# with it; the run's spending cap still governs.
MAX_OUTPUT_TOKENS = 32768
REASONING = {"effort": "medium"}

# LIMITS-003 (CVA 2026-11-02): a research call waits up to 10 minutes for its reply (live run 4 lost a turn to the
# shared 120 s read timeout). Scoped to the Responses research path only; capture/http.py's shared timeouts (and every
# page-mode call) are unchanged. Connect stays 20 s.
RESEARCH_READ_TIMEOUT_S = 600


def research_timeout():
    import httpx
    return httpx.Timeout(RESEARCH_READ_TIMEOUT_S, connect=http.CONNECT_TIMEOUT_S)


NO_KEY = ("No model key was found, so nothing was researched. Add MODEL_API_KEY to ~/.config/proof-reader/env "
          "and start again.")


@dataclass
class ResponsesReply:
    """One Responses call as the job needs it. `output` holds the response's output items exactly as returned (they are
    replayed unchanged). Token counts are None when the reply did not report them; `searches` is None when it cannot be
    counted. `error` is a plain sentence (never the provider's body); `may_have_been_billed` says whether a failed
    request may still cost money."""
    model: str
    response_id: str | None = None
    status: str | None = None
    incomplete_reason: str | None = None
    output: list = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    searches: int | None = 0
    error: str | None = None
    http_status: int | None = None
    may_have_been_billed: bool = False
    timed_out: bool = False
    request_bytes: int = 0

    # ---------------------------------------------------------------- the parts of the output the loop uses

    def function_calls(self) -> list[dict]:
        """The function_call items: {call_id, name, arguments (the raw JSON string)}."""
        return [{"call_id": i.get("call_id"), "name": i.get("name"), "arguments": i.get("arguments")}
                for i in self.output if isinstance(i, dict) and i.get("type") == "function_call"]

    def web_searches(self) -> list[dict]:
        """The web_search_call items: {id, status, queries, results [{title, url, snippet}]}."""
        out = []
        for i in self.output:
            if not isinstance(i, dict) or i.get("type") != "web_search_call":
                continue
            action = i.get("action") if isinstance(i.get("action"), dict) else {}
            queries = action.get("queries") if isinstance(action.get("queries"), list) else (
                [action["query"]] if isinstance(action.get("query"), str) else [])
            results = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("snippet")}
                       for r in (i.get("results") or []) if isinstance(r, dict)]
            out.append({"id": i.get("id"), "status": i.get("status"), "queries": queries, "results": results})
        return out

    def text(self) -> str:
        """The assistant's prose (output_text blocks), joined."""
        parts = []
        for i in self.output:
            if isinstance(i, dict) and i.get("type") == "message":
                for c in i.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "output_text" and isinstance(c.get("text"), str):
                        parts.append(c["text"])
        return "\n".join(parts).strip()

    def citations(self) -> list[dict]:
        """url_citation annotations of the prose: leads, never evidence."""
        out = []
        for i in self.output:
            if isinstance(i, dict) and i.get("type") == "message":
                for c in i.get("content") or []:
                    for a in (c.get("annotations") or []) if isinstance(c, dict) else []:
                        if isinstance(a, dict) and a.get("type") == "url_citation":
                            out.append({"url": a.get("url"), "title": a.get("title")})
        return out

    def unfinished_searches(self) -> list[dict]:
        """Built-in searches in this reply that did not complete (searching, in_progress, failed, or no status): their
        results are not complete, so a finish in the same reply is not accepted."""
        return [s for s in self.web_searches() if s.get("status") != "completed"]

    @property
    def length_cut(self) -> bool:
        """The answer stopped at max_output_tokens (finish_reason length)."""
        return self.status == "incomplete" and (self.incomplete_reason or "") in ("max_output_tokens", "length")

    @property
    def empty(self) -> bool:
        """No usable tool call, research output or structured completion: no function call, no web search, no prose.
        A reply that only reasoned until it ran out of output budget is empty. An error is not 'empty' (it is counted
        as an error)."""
        if self.error:
            return False
        return not self.function_calls() and not self.web_searches() and not self.text()


def count_searches(output: list) -> int | None:
    """Billable built-in searches in one response: each completed web_search_call is one search, or as many as the
    queries it lists. None when a search item has a status other than completed/failed/searching that cannot be read
    (the count is then unknown, never zero)."""
    n = 0
    for i in output or []:
        if not isinstance(i, dict) or i.get("type") != "web_search_call":
            continue
        status = i.get("status")
        if status not in ("completed", "failed", "searching", "in_progress", None):
            return None
        action = i.get("action") if isinstance(i.get("action"), dict) else {}
        queries = action.get("queries")
        n += len(queries) if isinstance(queries, list) and queries else 1
    return n


MALFORMED = "The model service answered HTTP 200 with a response this code cannot read ({what}); it may have been billed."


def _count(v):
    """A reported count: a whole number from 0 (never a boolean or a negative); None when not reported."""
    return v if type(v) is int and v >= 0 else None


def malformed(what: str, requested_model: str, request_bytes: int, *, usage: dict | None = None,
              body: dict | None = None) -> ResponsesReply:
    """A successful transport whose envelope is not a readable response: a typed, possibly-billed error. Reported token
    counts are kept where they are valid; the search count is unknown (None), never 0."""
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    return ResponsesReply(model=(body or {}).get("model") if isinstance((body or {}).get("model"), str)
                          else requested_model,
                          response_id=(body or {}).get("id") if isinstance((body or {}).get("id"), str) else None,
                          status=(body or {}).get("status") if isinstance((body or {}).get("status"), str) else None,
                          input_tokens=_count(usage.get("input_tokens")), output_tokens=_count(usage.get("output_tokens")),
                          reasoning_tokens=_count(details.get("reasoning_tokens")), searches=None,
                          error=MALFORMED.format(what=what), may_have_been_billed=True, request_bytes=request_bytes)


def parse_response(body, requested_model: str = MODEL, request_bytes: int = 0) -> ResponsesReply:
    """A ResponsesReply from the decoded JSON body of a successful POST /responses. Never raises: a body that is not an
    object, has no output list, or holds an output item that is not an object is a malformed (possibly billed) reply.
    A valid empty output list is allowed (it is an empty answer, not an error)."""
    if not isinstance(body, dict):
        return malformed(f"the body is a JSON {type(body).__name__}, not an object", requested_model, request_bytes)
    usage = body.get("usage")
    if "output" not in body or not isinstance(body.get("output"), list):
        return malformed("it has no output list", requested_model, request_bytes, usage=usage, body=body)
    output = body["output"]
    if not all(isinstance(i, dict) and isinstance(i.get("type"), str) for i in output):
        return malformed("an output item is not a typed object", requested_model, request_bytes, usage=usage, body=body)
    status = body.get("status")
    if status not in ("completed", "incomplete"):
        # Manus 17:07: a response that is not finished (status missing, in_progress, queued, cancelled, failed) is
        # never taken as an answer: a typed, possibly billed failure; its search count is unknown.
        return malformed(f"its status is {status!r}, not a finished response", requested_model, request_bytes,
                         usage=usage, body=body)
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    incomplete = body.get("incomplete_details") if isinstance(body.get("incomplete_details"), dict) else {}
    return ResponsesReply(
        model=body.get("model") if isinstance(body.get("model"), str) and body.get("model") else requested_model,
        response_id=body.get("id"), status=body.get("status"),
        incomplete_reason=incomplete.get("reason"), output=output,
        input_tokens=_count(usage.get("input_tokens")), output_tokens=_count(usage.get("output_tokens")),
        reasoning_tokens=_count(details.get("reasoning_tokens")), searches=count_searches(output),
        request_bytes=request_bytes)


def build_request(*, instructions: str, input_items: list, functions: list[dict], searches_left: int,
                  max_output_tokens: int = MAX_OUTPUT_TOKENS, continuation: str = CONTINUE_REPLAY,
                  previous_response_id: str | None = None, model: str = MODEL,
                  search_context_size: str = "medium") -> dict:
    """The JSON body of one request. web_search is offered only while searches are left (the API's documented minimum
    max_tool_calls is one, so a zero allowance removes the tool rather than sending zero)."""
    tools = []
    if searches_left > 0:
        tools.append({"type": "web_search", "search_context_size": search_context_size})
    tools += functions
    body = {"model": model, "instructions": instructions, "input": input_items, "tools": tools,
            "tool_choice": "auto", "parallel_tool_calls": False, "max_output_tokens": max_output_tokens,
            "reasoning": dict(REASONING), "include": list(INCLUDE),
            "store": continuation == CONTINUE_PREVIOUS}
    if searches_left > 0:
        body["max_tool_calls"] = searches_left
    if continuation == CONTINUE_PREVIOUS and previous_response_id:
        body["previous_response_id"] = previous_response_id
    return body


def request_size(body: dict) -> int:
    """The request's size in bytes: an upper bound on its input tokens (a token is at least one byte of text)."""
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


class LiveResponsesReader:
    """Muse Spark 1.3 on the Meta Model API's Responses endpoint."""

    model = MODEL
    name = NAME

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")

    def _key(self) -> str:
        read_table.load_env_file()
        return os.environ.get("MODEL_API_KEY", "").strip()

    def ready(self) -> str | None:
        return None if self._key() else NO_KEY

    def respond(self, body: dict) -> ResponsesReply:
        """One POST /responses. Never raises; never retries."""
        import httpx

        size = request_size(body)
        try:
            with httpx.Client(timeout=research_timeout()) as client:
                resp = client.post(f"{self.base_url}/responses", json=body,
                                   headers={"Authorization": f"Bearer {self._key()}",
                                            "Content-Type": "application/json"})
        except Exception as e:  # the type only: nothing that could carry the key or request details
            silent = http.is_silent(e)
            return ResponsesReply(model=body.get("model") or self.model, timed_out=silent,
                                  error=(http.did_not_answer(self.name) if silent else
                                         f"The model call did not succeed ({type(e).__name__})."),
                                  may_have_been_billed=http.may_have_been_billed(e) if silent else True,
                                  request_bytes=size)
        if resp.status_code >= 400:
            status = resp.status_code
            billed = status not in read_table.NO_INFERENCE_STATUSES
            return ResponsesReply(model=body.get("model") or self.model, http_status=status,
                                  error=f"The model call did not succeed (HTTP {status}).",
                                  may_have_been_billed=billed, request_bytes=size)
        try:
            data = resp.json()
        except ValueError:
            return ResponsesReply(model=body.get("model") or self.model, http_status=resp.status_code,
                                  error="The model service answered with something that is not JSON.",
                                  may_have_been_billed=True, request_bytes=size)
        try:
            reply = parse_response(data, body.get("model") or self.model, size)
        except Exception as e:  # parse_response never raises; this keeps respond()'s promise if it ever did
            reply = malformed(f"reading it failed ({type(e).__name__})", body.get("model") or self.model, size)
        reply.http_status = resp.status_code
        if reply.status == "failed":
            reply.error = "The model service reported the response as failed."
            reply.may_have_been_billed = True
        return reply
