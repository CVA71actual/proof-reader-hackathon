"""The second reader: Muse Spark Open, an independent read of the same page.

Primary route: Baseten (OpenAI-compatible), model string exactly `meta-models/muse-spark-open-1026` (BUILD_CONTROL.md,
"Second reader route"). Fallback: OpenRouter, only if OPENROUTER_API_KEY is present, and only while the run's
OpenRouter spend (recorded in its job.json) is under the $20 cap; the job sets `openrouter_allowed` before each call.

It has the same interface as capture/reader.py LiveReader (ready(), model, call(prompt) -> Reply) and is given the
same kind of prompt, built by the same builders from the page's words. It is never given the first reader's answer:
nothing in this module takes one. Keys come from the environment or ~/.config/proof-reader/env through
read_table.load_env_file and are never printed, logged or saved; a failed call reports only the error type and
status code. Cost is never decided here: the job prices the token counts with runs/prices.py ("Muse Spark Open").

    python -m capture.second_reader      one live second read of the pinned PTGX page from its IMAGE, with no word
                                         list, so it does not depend on the text layer (needs a key); saves
                                         fixtures/ptgx_8k_2026-08-05_ex991_p3.events.second.json
    python -m capture.second_reader --words
                                         the same read from the page's words (the earlier form); saves
                                         fixtures/ptgx_8k_2026-08-05_ex991_p3.events.second_words.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from capture import read_events, read_table
from capture.capture_source import FIXTURES, load_entry
from capture import http
from capture.reader import Reply, create, messages_for, retried, timeout_reply

NAME = "Muse Spark Open"
BASETEN_BASE_URL = "https://inference.baseten.co/v1"
BASETEN_MODEL = "meta-models/muse-spark-open-1026"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "meta/muse-spark-open"
OPENROUTER_CAP_USD = 20.0
# Authentication/permission refusal establishes no inference. Other 4xx
# statuses are not sufficient here to establish pre-inference rejection.
NO_INFERENCE_STATUSES = frozenset({401, 403})
CAP_REACHED = "The OpenRouter fallback has reached its $20 cap, so there was no second read."
UNPRICED = ("The OpenRouter fallback returned no token counts, so its spend cannot be held under the $20 cap; no "
            "more fallback calls were made.")
NO_KEY = ("No second-reader key was found (BASETEN_API_KEY, or OPENROUTER_API_KEY for the fallback), so there was "
          "no second read.")
CLI_REFUSED = "The paid OpenRouter fallback was not called: pass --allow-paid-fallback to use it."
CLI_NO_FALLBACK = "No paid fallback is configured (OPENROUTER_API_KEY is not set), so no fallback call was made."

SECOND_READING_PATH = FIXTURES / f"{read_events.PIN}.events.second.json"
SECOND_WORDS_READING_PATH = FIXTURES / f"{read_events.PIN}.events.second_words.json"


class SecondReader:
    name = NAME

    def __init__(self) -> None:
        self.openrouter_allowed = True   # the job turns this off once the $20 cap is reached (or a call went unpriced)
        self.openrouter_blocked: str | None = None   # and says why, in one plain sentence
        self.before_fallback = None   # the job's gate: every failed Baseten Reply; False = no fallback call

    def _key(self, name: str) -> str:
        read_table.load_env_file()
        return os.environ.get(name, "").strip()

    def route(self) -> str | None:
        if self._key("BASETEN_API_KEY"):
            return "baseten"
        if self._key("OPENROUTER_API_KEY") and self.openrouter_allowed:
            return "openrouter"
        return None

    @property
    def model(self) -> str:
        return OPENROUTER_MODEL if self.route() == "openrouter" else BASETEN_MODEL

    def ready(self) -> str | None:
        if self.route():
            return None
        if not self._key("BASETEN_API_KEY") and self._key("OPENROUTER_API_KEY") and not self.openrouter_allowed:
            return self.openrouter_blocked or CAP_REACHED     # a key is there; the cap, not the key, is why not
        return NO_KEY

    def call(self, prompt: str, image: bytes | None = None) -> Reply:
        route = self.route()
        if route is None:
            return Reply(None, BASETEN_MODEL, error=self.ready() or NO_KEY)
        reply = self._ask(route, prompt, image)
        if reply.error and route == "baseten":
            # An error response alone does not establish that the attempt was
            # free. Preserve an explicit known-unbilled classification, otherwise
            # let the ordinary job gate account for the unknown cost.
            reply.extra = {"may_have_been_billed": True, **(reply.extra or {})}
        if reply.error and route == "baseten" and self._key("OPENROUTER_API_KEY") and self.openrouter_allowed:
            if self.before_fallback is not None:
                # Count the failed primary attempt before admitting the fallback.
                if not self.before_fallback(reply):
                    reply.extra = {**(reply.extra or {}), "already_counted": True, "fallback_skipped": True}
                    return reply
            reply = self._ask("openrouter", prompt, image)   # Baseten did not answer: the fallback, under its cap
        return reply

    def _ask(self, route: str, prompt: str, image: bytes | None = None) -> Reply:
        base, model, key = ((BASETEN_BASE_URL, BASETEN_MODEL, self._key("BASETEN_API_KEY")) if route == "baseten"
                            else (OPENROUTER_BASE_URL, OPENROUTER_MODEL, self._key("OPENROUTER_API_KEY")))
        client = http.openai_client(base, key)
        messages = messages_for(prompt, image)
        response_format = "json_object"
        http.take_billed()
        def failed(exc, *, prior_unknown=False):
            status = read_table._status(exc)
            unknown = http.take_billed() or prior_unknown or status not in NO_INFERENCE_STATUSES
            reason = ("A failed attempt may have been billed; cost is unknown." if unknown else
                      f"HTTP {status}: authentication or permission rejected before inference.")
            return Reply(None, model, error=read_table._plain_error(exc), extra={
                "route": route, "http_status": status, "may_have_been_billed": unknown,
                "billing_classification_reason": reason})
        try:
            resp = self._create(client, model, messages, json_mode=True)
        except Exception as e:  # some endpoints reject response_format; retry once without it
            if http.is_silent(e):
                return timeout_reply(NAME, model, extra={"route": route}, exc=e)
            if read_table._status(e) not in (400, 422):
                return failed(e)
            response_format = "none"
            try:
                resp = self._create(client, model, messages, json_mode=False)
            except Exception as e2:
                if http.is_silent(e2):
                    return timeout_reply(NAME, model, extra={"route": route}, exc=e2)
                return failed(e2, prior_unknown=True)
        raw = resp.choices[0].message.content if resp.choices else None
        usage = getattr(resp, "usage", None)
        extra = {"route": route, "base_url": base, "requested_model": model, "response_format": response_format,
                 "input": "image" if image is not None else "words"}
        return Reply(raw, getattr(resp, "model", None) or model,
                     prompt_tokens=getattr(usage, "prompt_tokens", None),
                     completion_tokens=getattr(usage, "completion_tokens", None), extra=retried(extra))

    @staticmethod
    def _create(client, model: str, messages: list, json_mode: bool):
        return create(client, model, messages, json_mode)


def reading_record(reply: Reply, prompt: str, parsed, problem, entry: dict, input: str = "words") -> dict:
    """The saved second reading of a page: the exact prompt and its SHA-256, the raw answer, the parsed events and the
    tokens. The same shape as the first reading, so the same builders and the same checks take it."""
    extra = {**(reply.extra or {}), "second_reader": True, "input": input,
             "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}}
    if reply.error:
        extra["call_failed"] = True
    return read_events._record(model=reply.model, prompt=prompt, raw=reply.raw, events=parsed,
                               problem=reply.error or problem, entry=entry, extra=extra)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--words", action="store_true", help="read from the page's words instead of its image")
    ap.add_argument("--allow-paid-fallback", action="store_true",
                    help="allow the paid OpenRouter fallback if the primary call fails")
    args = ap.parse_args(argv)
    args.out = args.out or (SECOND_WORDS_READING_PATH if args.words else SECOND_READING_PATH)
    reader = SecondReader()
    # Not the Job's gate: the CLI makes one call per run and keeps no spend ledger or $20 cap. The flag alone
    # decides whether a failed primary call may be followed by one paid OpenRouter call.
    reader.before_fallback = lambda reply: args.allow_paid_fallback
    if not args.allow_paid_fallback:
        reader.openrouter_allowed, reader.openrouter_blocked = False, CLI_REFUSED
    problem = reader.ready()
    if problem:
        print(problem)
        return 2
    words_doc, entry = read_events.load_words(), load_entry(read_events.PIN)
    if args.words:
        prompt, image, got = read_events.build_prompt(words_doc, entry), None, "words"
    else:
        from runs.build_run import render_page_bytes
        from runs.sources import builtin_index, pinned_source_snapshot
        import tempfile

        page_record = builtin_index().pages("PTGX")[0]["record"]
        pdf_bytes, _, _, _ = pinned_source_snapshot(page_record)
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "page.png"
            render_page_bytes(pdf_bytes, png)
            image = png.read_bytes()
        prompt, got = read_events.build_image_prompt(entry), "image"
    reply = reader.call(prompt, image=image)
    primary_failed = bool(reply.error) and (reply.extra or {}).get("route") == "baseten"
    fallback_configured = bool(reader._key("OPENROUTER_API_KEY"))
    refused = primary_failed and fallback_configured and not args.allow_paid_fallback
    if refused:   # record the gate's refusal in the saved file, not only on stdout
        reply.extra = {**(reply.extra or {}), "fallback_skipped": True, "fallback_refused_reason": CLI_REFUSED}
    parsed, parse_problem = (None, None) if reply.error else read_events.parse_events(reply.raw)
    rec = reading_record(reply, prompt, parsed, parse_problem, entry, got)
    args.out.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
    if reply.error:
        print(f"The second read did not succeed ({reply.error}); the failure is saved -> {args.out}")
        if refused:
            print(CLI_REFUSED)
        elif primary_failed and not fallback_configured:
            print(CLI_NO_FALLBACK)
        return 1
    if parsed is None:
        print(f"Saved an unreadable answer from {rec['model']} ({parse_problem}) -> {args.out}")
        return 0
    print(f"Saved second reading ({got}) by {rec['model']} via {reply.extra['route']}: {len(parsed)} events, "
          f"tokens {reply.prompt_tokens} in / {reply.completion_tokens} out -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
