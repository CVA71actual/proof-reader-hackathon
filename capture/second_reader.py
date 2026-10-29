"""The second reader: Muse Spark Open, an independent read of the same page.

Primary route: Baseten (OpenAI-compatible), model string exactly `meta-models/muse-spark-open-1026` (BUILD_CONTROL.md,
"Second reader route"). Fallback: OpenRouter, only if OPENROUTER_API_KEY is present, and only while the run's
OpenRouter spend (recorded in its job.json) is under the $20 cap; the job sets `openrouter_allowed` before each call.

It has the same interface as capture/reader.py LiveReader (ready(), model, call(prompt) -> Reply) and is given the
same kind of prompt, built by the same builders from the page's words. It is never given the first reader's answer:
nothing in this module takes one. Keys come from the environment or ~/.config/proof-reader/env through
read_table.load_env_file and are never printed, logged or saved; a failed call reports only the error type and
status code. Cost is never decided here: the job prices the token counts with runs/prices.py ("Muse Spark Open").

    python -m capture.second_reader      one live second read of the pinned PTGX page (needs a key); saves
                                         fixtures/ptgx_8k_2026-08-05_ex991_p3.events.second.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from capture import read_events, read_table
from capture.capture_source import FIXTURES, load_entry
from capture.reader import Reply

NAME = "Muse Spark Open"
BASETEN_BASE_URL = "https://inference.baseten.co/v1"
BASETEN_MODEL = "meta-models/muse-spark-open-1026"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "meta/muse-spark-open"
OPENROUTER_CAP_USD = 20.0
NO_KEY = ("No second-reader key was found (BASETEN_API_KEY, or OPENROUTER_API_KEY for the fallback), so there was "
          "no second read.")

SECOND_READING_PATH = FIXTURES / f"{read_events.PIN}.events.second.json"


class SecondReader:
    name = NAME

    def __init__(self) -> None:
        self.openrouter_allowed = True   # the job turns this off once the $20 cap is reached

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
        return None if self.route() else NO_KEY

    def call(self, prompt: str) -> Reply:
        route = self.route()
        if route is None:
            return Reply(None, BASETEN_MODEL, error=NO_KEY)
        reply = self._ask(route, prompt)
        if reply.error and route == "baseten" and self._key("OPENROUTER_API_KEY") and self.openrouter_allowed:
            reply = self._ask("openrouter", prompt)     # Baseten did not answer: the fallback, under its cap
        return reply

    def _ask(self, route: str, prompt: str) -> Reply:
        from openai import OpenAI  # imported here so tests and offline use need no network client

        base, model, key = ((BASETEN_BASE_URL, BASETEN_MODEL, self._key("BASETEN_API_KEY")) if route == "baseten"
                            else (OPENROUTER_BASE_URL, OPENROUTER_MODEL, self._key("OPENROUTER_API_KEY")))
        client = OpenAI(base_url=base, api_key=key)
        messages = [{"role": "user", "content": prompt}]
        response_format = "json_object"
        try:
            resp = self._create(client, model, messages, json_mode=True)
        except Exception as e:  # some endpoints reject response_format; retry once without it
            if read_table._status(e) not in (400, 422):
                return Reply(None, model, error=read_table._plain_error(e), extra={"route": route})
            response_format = "none"
            try:
                resp = self._create(client, model, messages, json_mode=False)
            except Exception as e2:
                return Reply(None, model, error=read_table._plain_error(e2), extra={"route": route})
        raw = resp.choices[0].message.content if resp.choices else None
        usage = getattr(resp, "usage", None)
        extra = {"route": route, "base_url": base, "requested_model": model, "response_format": response_format}
        return Reply(raw, getattr(resp, "model", None) or model,
                     prompt_tokens=getattr(usage, "prompt_tokens", None),
                     completion_tokens=getattr(usage, "completion_tokens", None), extra=extra)

    @staticmethod
    def _create(client, model: str, messages: list, json_mode: bool):
        kwargs = {"model": model, "messages": messages, "temperature": 0}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return client.chat.completions.create(**kwargs)


def reading_record(reply: Reply, prompt: str, parsed, problem, entry: dict) -> dict:
    """The saved second reading of a page: the exact prompt and its SHA-256, the raw answer, the parsed events and the
    tokens. The same shape as the first reading, so the same builders and the same checks take it."""
    extra = {**(reply.extra or {}), "second_reader": True,
             "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}}
    if reply.error:
        extra["call_failed"] = True
    return read_events._record(model=reply.model, prompt=prompt, raw=reply.raw, events=parsed,
                               problem=reply.error or problem, entry=entry, extra=extra)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=SECOND_READING_PATH)
    args = ap.parse_args(argv)
    reader = SecondReader()
    problem = reader.ready()
    if problem:
        print(problem)
        return 2
    words_doc, entry = read_events.load_words(), load_entry(read_events.PIN)
    prompt = read_events.build_prompt(words_doc, entry)
    reply = reader.call(prompt)
    parsed, parse_problem = (None, None) if reply.error else read_events.parse_events(reply.raw)
    rec = reading_record(reply, prompt, parsed, parse_problem, entry)
    args.out.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
    if reply.error:
        print(f"The second read did not succeed ({reply.error}); the failure is saved -> {args.out}")
        return 1
    if parsed is None:
        print(f"Saved an unreadable answer from {rec['model']} ({parse_problem}) -> {args.out}")
        return 0
    print(f"Saved second reading by {rec['model']} via {reply.extra['route']}: {len(parsed)} events, "
          f"tokens {reply.prompt_tokens} in / {reply.completion_tokens} out -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
