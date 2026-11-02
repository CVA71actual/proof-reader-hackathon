"""The checks as tools the first reader can call while it reads one page (run choice "reader_tools").

Two tools, each a thin wrapper over the real check function. They only answer: they never change the page, the
row or the reader's proposal. Each returns {"result", "reason", "found"}.

    quote_on_page(quote)                       checks.event_checks.quote_match on the page words
    date_as_written(date_text, date, date_end, quote, event_type, program)
                                               checks.event_checks.date_match, with the source's filing date and the
                                               release day (the dateline, else the filing date)
    The retired event_type_for_words request answers could_not_run; keywords do not establish meaning.

How the read goes (Job.read_with_tools in server/jobs.py drives it):
  1. The model gets the usual instructions plus a short note about the tools and may call them about its own proposals.
  2. Native tool calling first: the OpenAI "tools" / "tool_calls" shape. If api.meta.ai rejects the tools parameter
     (HTTP 400 or 422), the same loop goes on with a JSON tool request the model writes and the code runs (below).
  3. Caps: at most MAX_PER_EVENT (3) tool calls per proposed event (an event is its quote) and MAX_PER_PAGE (30) per
     page. A call over a cap is not run; the answer says so. Every model call in the loop is a turn and is priced.
  4. When a tool says fail, the code tells the model once, per row, to read the page again for that row and fix
     it or leave it out. After that one re-read the row's answer stands as it is.
  5. The model hands over the final events as before. The code's own checks still run on every row afterwards,
     exactly as without tools: the model's tool use is advice to itself, not the verdict.

The JSON tool request (fallback path). The model's whole answer is one JSON object:
    {"tool_calls": [{"name": "quote_on_page", "arguments": {"quote": "..."}}, ...]}
and the code answers in the next message with:
    {"tool_answers": [{"name": "quote_on_page", "result": "pass", "reason": "...", "found": "..."}, ...]}
When the model has no more checks to ask for, its whole answer is the final {"events": [...]} object.
"""

from __future__ import annotations

import json
from datetime import date

from checks import event_checks
from checks.common import normalize

MAX_PER_EVENT = 3
MAX_PER_PAGE = 30
MAX_MODEL_CALLS = 12      # a stop for a model that never stops asking (each call is also a turn and priced)

# plain names for the trail: "Called the check: <name>"
PLAIN = {"quote_on_page": "Quote found on page", "date_as_written": event_checks.DATE_LABEL,
         "event_type_for_words": "Event meaning needs review"}
WHO = {"quote_on_page": "checks.event_checks.quote_match", "date_as_written": "checks.event_checks.date_match",
       "event_type_for_words": "capture.reader_tools"}

TOOLS = [
    {"type": "function", "function": {
        "name": "quote_on_page",
        "description": "Is this quote printed on the page, word for word? Answers pass or fail.",
        "parameters": {"type": "object", "properties": {"quote": {"type": "string"}}, "required": ["quote"]}}},
    {"type": "function", "function": {
        "name": "date_as_written",
        "description": "Is the date text printed in the quote, and do date and date_end say the same thing? "
                       "Answers pass, fail or could_not_run.",
        "parameters": {"type": "object", "properties": {
            "date_text": {"type": "string"}, "date": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
            "date_end": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
            "quote": {"type": "string"}, "event_type": {"type": ["string", "null"]},
            "program": {"type": ["string", "null"]}}, "required": ["date_text", "quote"]}}},

]

NOTE_NATIVE = """

You may check your own proposals before you hand them over, by calling these tools: quote_on_page(quote),
date_as_written(date_text, date, date_end, quote, event_type, program). They only
answer; they never change the page. At most {per_event} calls per event and {per_page} per page. If a tool answers
fail, read the page again for that row, once, and fix it or leave it out. When you are done checking, answer
with the JSON object of events only, in the shape above.
"""

NOTE_JSON = """

You may check your own proposals before you hand them over. To ask for checks, answer with this JSON object and
nothing else: {{"tool_calls": [{{"name": "<tool>", "arguments": {{...}}}}, ...]}}. The tools are
quote_on_page(quote) and date_as_written(date_text, date, date_end, quote, event_type, program).
They only answer; they never change the page. I will answer with
{{"tool_answers": [{{"name": ..., "result": ..., "reason": ..., "found": ...}}]}}. At most {per_event} calls per event and
{per_page} per page. If a tool answers fail, read the page again for that row, once, and fix it or leave it
out. When you are done checking, answer with the JSON object of events only, in the shape above.
"""

REREAD = "A check failed: {reason}. Re-read this row once, then hand over."
NO_REREAD = "A check failed: {reason}. The row has had its one re-read; its answer stands as it is."
REREAD_USED = "No check was run: this row has had its one re-read."


def reread_note(reason, used: bool = False) -> str:
    return (NO_REREAD if used else REREAD).format(reason=str(reason or "").strip().rstrip("."))
OVER_EVENT = f"No check was run: this event has had its {MAX_PER_EVENT} checks."
OVER_PAGE = f"No check was run: this page has had its {MAX_PER_PAGE} checks."


def note(native: bool) -> str:
    return (NOTE_NATIVE if native else NOTE_JSON).format(per_event=MAX_PER_EVENT, per_page=MAX_PER_PAGE)


def _key(quote) -> str:
    return normalize(quote if isinstance(quote, str) else "")


def _iso(v):
    return v if isinstance(v, str) and v.strip() else None


class ToolBox:
    """The mechanical tools for one page, with the counts and the trail of the calls made on it."""

    def __init__(self, words: list[dict], filed: date | str | None):
        self.words, self.filed = words, filed
        self.day = (event_checks.release_day(words, filed) or (None,))[0]
        self.calls: list[dict] = []        # {name, args, answer, key, ran, at}: the page's whole record
        self.per_event: dict[str, int] = {}
        self.turn = 0                      # the model turn whose tool calls are being answered (begin_turn)
        self.failed_at: dict[str, int] = {}   # row -> the turn of its latest fail answer
        self.attempted: dict[str, int] = {}   # row -> the turn of the re-read the model has actually made
        self.alias: dict[str, str] = {}    # a re-read quote -> the row it replaces
        self._pending: list[tuple[str, int]] = []   # (row, turn of its fail) whose new quote has not appeared yet
        self.reread_count = 0              # re-read attempts seen, never fail answers issued
        self.model_calls = 0               # model calls made on this page as a whole (the cap is not reset by a re-ask)
        self.p_tokens = self.c_tokens = 0  # tokens of the calls that reported a count (priced calls only)
        self.unrecorded = 0                # calls that reported no token count

    def begin_turn(self) -> None:
        """Call once per model turn that asks for tools, before answering its calls."""
        self.turn += 1

    # ---- the mechanical tools (thin wrappers; they never touch the page)

    def run_tool(self, name: str, args) -> dict:
        try:
            if not isinstance(args, dict):
                raise TypeError
            if name == "quote_on_page":
                r = event_checks.quote_match(args["quote"], self.words)
                return {"result": r.result, "reason": r.reason, "found": r.compared.get("found")}
            if name == "date_as_written":
                r = event_checks.date_match(
                    args["date_text"], _iso(args.get("date")), args["quote"], date_end=_iso(args.get("date_end")),
                    event_type=args.get("event_type"), program=args.get("program"), filed=self.filed, today=self.day)
                return {"result": r.result, "reason": r.reason, "found": r.compared.get("found")}
            if name == "event_type_for_words":
                return {"result": "could_not_run",
                        "reason": "Code does not infer event meaning from keywords. Preserve your proposed interpretation for human review.",
                        "found": None}
        except (KeyError, TypeError):
            return {"result": "could_not_run", "reason": "The tool call was missing an argument or it was the wrong "
                                                         "kind.", "found": None}
        return {"result": "could_not_run", "reason": f"There is no tool called {name}.", "found": None}

    # ---- the loop's bookkeeping

    def event_key(self, args) -> str:
        k = _key(args.get("quote")) if isinstance(args, dict) else ""
        if k and k not in self.per_event and k not in self.alias:
            for i, (row, at) in enumerate(self._pending):
                if at < self.turn and row and (k in row or row in k):      # an edit of the failed quote, nothing else
                    self.alias[k] = self._pending.pop(i)[0]
                    break
        return self.alias.get(k, k)

    def note_final(self, quotes) -> None:
        """The hand-over: a failed row that was not tried again by tool call counts as re-read when its quote was
        changed (an edit of the failed quote). A row left as it was, left out, or swapped for an unrelated one does not."""
        keys = {_key(q) for q in quotes}
        new = [k for k in keys if k not in self.per_event and k not in self.alias]
        for key, _ in list(self._pending):
            same = [k for k in new if k and key and (k in key or key in k)]       # an edit of the failed quote
            if key not in self.attempted and key not in keys and same:
                self.alias[same[0]] = key
                new.remove(same[0])
                self.attempted[key] = self.turn
                self.reread_count += 1
        self._pending.clear()

    def call(self, name: str, args) -> tuple[dict, str | None]:
        """Run one tool call under the caps. Returns (answer, note for the model or None): the note is the re-read
        instruction for a first fail on a row, or its refusal for a second."""
        key = self.event_key(args)
        ran = True
        again = key in self.failed_at and self.failed_at[key] < self.turn      # a later turn than its fail answer
        if again and key not in self.attempted:
            self.attempted[key] = self.turn            # the model tried the row again: that is the re-read
            self.reread_count += 1
            self._pending = [p for p in self._pending if p[0] != key]
        if again and self.failed_at[key] >= self.attempted[key]:        # the re-read failed too: no third attempt
            answer, ran = {"result": "could_not_run", "reason": REREAD_USED, "found": None}, False
        elif sum(1 for c in self.calls if c["ran"]) >= MAX_PER_PAGE:
            answer, ran = {"result": "could_not_run", "reason": OVER_PAGE, "found": None}, False
        elif self.per_event.get(key, 0) >= MAX_PER_EVENT:
            answer, ran = {"result": "could_not_run", "reason": OVER_EVENT, "found": None}, False
        else:
            self.per_event[key] = self.per_event.get(key, 0) + 1
            answer = self.run_tool(name, args)
        self.calls.append({"name": name, "args": args if isinstance(args, dict) else {}, "answer": answer, "key": key,
                           "ran": ran, "turn": self.turn})
        hint = None
        if ran and answer["result"] == "fail":
            used = key in self.attempted
            self.failed_at[key] = self.turn
            if not used and not any(p[0] == key for p in self._pending):
                self._pending.append((key, self.turn))
            hint = reread_note(answer["reason"], used)
        return answer, hint

    def type_found(self, quote) -> str | None:
        """The type the tool's latest answer found for this row's quote, or None when it was not asked."""
        key = self.alias.get(_key(quote), _key(quote))
        found = [c["answer"].get("found") for c in self.calls
                 if c["key"] == key and c["ran"] and c["name"] == "event_type_for_words"]
        return found[-1] if found else None

    def unrecorded_steps(self, at: str, model_name: str) -> list[dict]:
        """One trail step for the calls that gave no token count: they are not priced and do not null a row's cost."""
        if not self.unrecorded:
            return []
        n = self.unrecorded
        return [{"step": "Model call: tokens not recorded", "tag": "Code", "who": "server.jobs", "at": at,
                 "text": f"{n} call{'s' if n != 1 else ''} to {model_name} gave no token count; "
                         f"{'it was' if n == 1 else 'they were'} not priced."}]

    def steps_for(self, quote, at: str, model_name: str) -> list[dict]:
        """The trail steps of the calls about this row's quote: a Model step, then a Code step, for each call."""
        key = self.alias.get(_key(quote), _key(quote))
        steps = []
        for c in self.calls:
            if c["key"] != key:
                continue
            shown = ", ".join(f"{k}={v!r}" for k, v in c["args"].items() if v is not None)
            steps.append({"step": f"Called the check: {PLAIN.get(c['name'], c['name'])}", "tag": "Model",
                          "who": model_name, "text": f"{c['name']}({shown})", "at": at})
            a = c["answer"]
            steps.append({"step": f"Check answered: {a['reason']}", "tag": "Code", "who": WHO.get(c["name"], "capture.reader_tools"),
                          "text": f"{a['result']}" + (f"; found: {a['found']}" if a.get("found") else ""), "at": at})
        return steps

    def summary(self) -> dict:
        ran = [c for c in self.calls if c["ran"]]
        return {"tool_calls": len(self.calls), "tool_calls_run": len(ran), "rereads": self.reread_count,
                "failed_answers": sum(1 for c in ran if c["answer"]["result"] == "fail")}


# ---------------------------------------------------------------- the JSON tool request (fallback path)


def parse_tool_request(raw: str | None) -> list[dict] | None:
    """[{name, arguments}] when the whole answer is a JSON tool request, else None (it is the final answer)."""
    if not raw or "tool_calls" not in raw:
        return None
    import re

    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    text = fence.group(1) if fence else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[s:e + 1]) if s >= 0 and e > s else None
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict) or not isinstance(data.get("tool_calls"), list) or "events" in data:
        return None
    out = []
    for c in data["tool_calls"]:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            out.append({"name": c["name"], "arguments": c.get("arguments") if isinstance(c.get("arguments"), dict) else {}})
    return out


def native_calls(tool_calls: list[dict] | None) -> list[dict]:
    """[{id, name, arguments}] from OpenAI-shape tool_calls; arguments that are not a JSON object come back as {}."""
    out = []
    for c in tool_calls or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {}
        out.append({"id": c.get("id"), "name": fn.get("name") or "", "arguments": args if isinstance(args, dict) else {}})
    return out
