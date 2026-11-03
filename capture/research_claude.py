"""The Claude reviewer adapter (CVA decision 2026-11-03: "Claude for reviewer for now"; MULTI-FAMILY-001).

ClaudeChat has the same interface as capture.research_reviewer.OneShotChat (ready / available / probe / chat ->
ChatReply), so review_draft's manual tool loop, its A-TO-Z gates, the spend gate and the labels are unchanged. It
translates the loop's chat messages to the Messages API and back:

    client.beta.messages.stream(**request) as stream; stream.get_final_message()
    model claude-opus-5-5; max_tokens 16384 (LIMITS-005); tool_choice auto only (forced choice 400s on Opus 5.5);
    thinking {"type": "adaptive"} (it cannot be disabled on Opus 5.5) with block_binding drop_block (below);
    output_config {"effort": "high"} set explicitly (the model's default is medium);
    betas thinking-binding-controls-2026-08-01. No server-side fallback (Codex ruling, CAP-001): one request is one
    attempt; the model that answered and every attempt the API reports are still recorded and priced at their own
    rates (an unknown model is unpriced).

Every assistant turn's full response content (thinking blocks included) is put back into the history unchanged
(ChatReply.raw_content -> the loop's assistant message "provider_content"); all tool results of a turn go back in ONE
user message as tool_result blocks, with is_error on a failed or refused call. stop_reason is classified before any
tool runs: max_tokens -> finish reason "length" (the runaway rule: nothing of it is used), refusal -> "refusal" with
stop_details recorded.

Why drop_block: the review loop retires delivered Brave payloads and page text, and replays only the last turns
(BRAVE-001 / COMPACT-001). On Opus 5.5 a thinking block is bound to the exact conversation before it, so those edits
invalidate later thinking blocks; with prefix_mismatch_behavior "drop_block" the API drops such blocks instead of
refusing the request (the reasoning in them is lost; the conversation goes on).

The anthropic SDK is imported lazily, only when a live client is built; tests inject a fake client. The key comes from
ANTHROPIC_API_KEY through the existing env loader and is never printed, logged or stored. One request is one attempt:
the client is built with max_retries=0 (the SDK otherwise retries 408/409/429/5xx itself)."""

from __future__ import annotations

import json
import os
from typing import Any

from capture.research_reviewer import ChatReply

MODEL = "claude-opus-5-5"
NAME = "Claude Opus 5.5"
PROVIDER = "anthropic"
FAMILY = "Claude"
KEY_ENV = "ANTHROPIC_API_KEY"
MAX_TOKENS = 16384
EFFORT = "high"
# Codex ruling for S2-01 / CAP-001 (2026-11-03): NO automatic server-side fallback. One request is one attempt and the
# gate reserves exactly that; a refusal is a recorded stop with its category; the only fallback is the explicitly
# admitted Muse Spark Open reviewer, honestly labelled.
BETAS = ["thinking-binding-controls-2026-08-01"]
THINKING = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
PROBE_MAX_TOKENS = 64          # the access check's reply allowance (thinking is always on)
# Each attempt the API reports is priced at its own model's rates. With no server-side fallback there is one attempt
# on claude-opus-5-5; any other model answering has no price here, so that call is unpriced (never priced low).
PRICE_NAME_OF = {"claude-opus-5-5": "Claude Opus 5.5"}
NO_SDK = ("The anthropic Python SDK is not installed in this environment, so the Claude reviewer cannot run "
          "(installing it is pending CVA's approval).")
STOP_TO_FINISH = {"tool_use": "tool_calls", "end_turn": "stop", "max_tokens": "length", "refusal": "refusal",
                  "stop_sequence": "stop_sequence", "pause_turn": "pause_turn"}
ERROR_STATUSES = ("error", "refused", "rejected")


def _get(o, k, d=None):
    return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _block_dict(b) -> dict:
    """A response content block as a plain dict, unchanged (thinking signatures included)."""
    if isinstance(b, dict):
        return dict(b)
    for attr in ("to_dict", "model_dump"):
        fn = getattr(b, attr, None)
        if callable(fn):
            try:
                return fn(exclude_none=True) if attr == "model_dump" else fn()
            except TypeError:
                return fn()
    return {k: v for k, v in vars(b).items() if not k.startswith("_")}


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools or []:
        f = t.get("function") or {}
        out.append({"name": f.get("name"), "description": f.get("description") or "",
                    "input_schema": f.get("parameters") or {"type": "object"}})
    return out


def to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """(system, messages) for the Messages API from the review loop's chat messages."""
    system, out, pending = [], [], []

    def flush():
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append(m.get("content") or "")
        elif role == "tool":
            content = m.get("content") or ""
            try:
                status = json.loads(content).get("status") if isinstance(content, str) else None
            except (ValueError, AttributeError):
                status = None
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": content}
            if status in ERROR_STATUSES:
                block["is_error"] = True
            pending.append(block)
        else:
            flush()
            if role == "assistant":
                content = m.get("provider_content")
                if content is None:      # an assistant turn not produced by Claude: rebuild it
                    content = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [
                        {"type": "tool_use", "id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                         "input": json.loads((c.get("function") or {}).get("arguments") or "{}")}
                        for c in m.get("tool_calls") or []]
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "user", "content": m.get("content") or ""})
    flush()
    return "\n\n".join(system), out


class Malformed(ValueError):
    pass


def _component(part, key, required: bool) -> int | None:
    """One usage count. Absent optional -> 0; absent required -> None (unknown); an explicit invalid value (a bool, a
    negative, a string, a float) -> Malformed, so the whole call's accounting is unknown (S2-03)."""
    v = _get(part, key)
    if v is None:
        return None if required else 0
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise Malformed(key)
    return v


def _attempt_usage(part) -> tuple[int, int] | None:
    try:
        i = _component(part, "input_tokens", True)
        o = _component(part, "output_tokens", True)
        w = _component(part, "cache_creation_input_tokens", False)
        r = _component(part, "cache_read_input_tokens", False)
    except Malformed:
        return None
    if i is None or o is None:
        return None
    # cache writes cost 1.25x input; reads are counted at the full input price (never priced low)
    return i + (-(-w * 5 // 4)) + r, o


def _tool_calls(blocks: list[dict]) -> list[dict]:
    """Every tool_use block, validated (S2-02): a non-empty string id and name and an object input. Anything else is
    a malformed reply, never a clean completion."""
    calls = []
    for b in blocks:
        if b.get("type") != "tool_use":
            continue
        tid, name, args = b.get("id"), b.get("name"), b.get("input")
        if not (isinstance(tid, str) and tid and isinstance(name, str) and name and isinstance(args, dict)):
            raise Malformed("tool_use")
        calls.append({"id": tid, "name": name, "arguments": json.dumps(args)})
    return calls


def parse_message(msg, requested: str) -> ChatReply:
    """A ChatReply from a Messages API message (an SDK object or a dict). Unknown usage stays None."""
    blocks = [_block_dict(b) for b in (_get(msg, "content") or [])]
    try:
        calls = _tool_calls(blocks)
    except Malformed:
        return ChatReply(_get(msg, "model") or requested, may_have_been_billed=True,
                         error="The reply had a malformed tool call (a tool_use without an id, a name or an object "
                               "input), so nothing in it was used.")
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text") or None
    stop = _get(msg, "stop_reason")
    usage = _get(msg, "usage")
    iterations = _get(usage, "iterations") or []
    served = _get(msg, "model") or requested
    served = served if isinstance(served, str) else requested
    # billing: usage.iterations is the per-attempt source of truth (declined attempts are plain "message" entries,
    # the serving fallback attempt is "fallback_message"); top-level usage covers only the attempt that answered.
    # Each attempt is priced at its own model's rates (a declined attempt at the requested model's).
    parts = list(iterations) if iterations else [usage]
    attempts: list[dict] | None = []
    for part in parts:
        got = _attempt_usage(part)
        if got is None:
            attempts = None
            break
        fb = _get(part, "type") == "fallback_message"
        # a declined attempt ran on the requested model; the serving one (or a reply with no iterations, including a
        # sticky fallback turn) on the model that answered
        model = served if (fb or not iterations) else requested
        attempts.append({"kind": "fallback" if fb else "attempt", "model": model,
                         "price_name": PRICE_NAME_OF.get(model),
                         "prompt_tokens": got[0], "completion_tokens": got[1]})
    p = sum(a["prompt_tokens"] for a in attempts) if attempts else None
    c = sum(a["completion_tokens"] for a in attempts) if attempts else None
    fallback_ran = any(_get(i, "type") == "fallback_message" for i in iterations)
    details = _get(msg, "stop_details")
    reply = ChatReply(served, content=text, tool_calls=calls,
                      finish_reason=STOP_TO_FINISH.get(stop, stop), prompt_tokens=p, completion_tokens=c)
    reply.attempts = attempts
    reply.raw_content = blocks
    reply.stop_details = ({"category": _get(details, "category"), "explanation": _get(details, "explanation")}
                          if details is not None else None)
    reply.served_by = {"model": reply.model, "requested": requested, "fallback_ran": fallback_ran}
    return reply


class ClaudeChat:
    name, model, provider, family, price_name = NAME, MODEL, PROVIDER, FAMILY, NAME
    probe_max_tokens = PROBE_MAX_TOKENS
    max_tokens = MAX_TOKENS        # the spend gate reserves this reply allowance

    def __init__(self, client=None):
        self._injected = client
        self._live = None

    def _key(self) -> str:
        from capture import read_table
        read_table.load_env_file()
        return os.environ.get(KEY_ENV, "").strip()

    def ready(self) -> str | None:
        if self._injected is not None:
            return None
        if not self._key():
            return f"No {NAME} key was found ({KEY_ENV}), so this stage did not run."
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return NO_SDK
        return None

    def available(self) -> str | None:
        return self.ready()

    def _client(self):
        if self._injected is not None:
            return self._injected
        if self._live is None:
            import anthropic
            self._live = anthropic.Anthropic(api_key=self._key(), max_retries=0)
        return self._live

    def probe(self) -> ChatReply:
        return self.chat([{"role": "user", "content": "Reply with: ok"}], max_tokens=PROBE_MAX_TOKENS)

    def request(self, messages: list[dict], tools: list[dict] | None, max_tokens: int | None) -> dict:
        system, msgs = to_anthropic(messages)
        req: dict[str, Any] = {"model": MODEL, "max_tokens": max_tokens or MAX_TOKENS, "messages": msgs,
                               "thinking": THINKING, "output_config": {"effort": EFFORT}, "betas": list(BETAS)}
        if system:
            req["system"] = system
        if tools:
            req["tools"] = to_anthropic_tools(tools)
            req["tool_choice"] = {"type": "auto"}
        return req

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
             max_tokens: int | None = None) -> ChatReply:
        req = self.request(messages, tools, max_tokens)
        try:
            with self._client().beta.messages.stream(**req) as stream:
                msg = stream.get_final_message()
        except Exception as e:     # one request, one attempt; the type, status and provider message only
            status = getattr(e, "status_code", None)
            status = status if isinstance(status, int) else None
            timed = "timeout" in type(e).__name__.lower()
            text = str(getattr(e, "message", "") or "")[:300]
            return ChatReply(MODEL, error=f"{NAME} did not answer: {type(e).__name__}"
                                          + (f" (HTTP {status})" if status else "") + (f": {text}" if text else ""),
                             http_status=status, timed_out=timed,
                             may_have_been_billed=status is None or status >= 500)
        try:
            return parse_message(msg, MODEL)
        except Exception as e:
            return ChatReply(MODEL, may_have_been_billed=True,
                             error=f"The reply could not be read ({type(e).__name__}), so it could not be used.")
