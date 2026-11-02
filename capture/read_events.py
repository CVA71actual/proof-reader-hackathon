"""Read the catalyst events on a captured page with Muse Spark (Meta), or build a labelled fake reading.

Usage (from the repo root):
    python -m capture.read_events            live reading, needs MODEL_API_KEY
    python -m capture.read_events --fake     offline fake reading built from the words file

The live reading is saved to fixtures/ptgx_8k_2026-08-05_ex991_p3.events.json: the exact text sent (and its
SHA-256), the raw answer, and the parsed events. The fake reading is saved to
fixtures/ptgx_8k_2026-08-05_ex991_p3.events.fake.json (not committed) and is always marked model "fake".
It is never presented as a model reading.

Same call pattern as capture/read_table.py: base_url https://api.meta.ai/v1, model muse-spark-1.3, the key from
the environment (MODEL_API_KEY) or ~/.config/proof-reader/env. The key is never printed, logged or saved.
If the call fails, the failure is saved (no events) and nothing is made up.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from capture import read_table
from capture import research_instructions
from capture.capture_source import FIXTURES, load_entry
from capture.read_table import BASE_URL, ENV_FILE, FAKE_MODEL, MODEL, _num, _plain_error, _status, sha256_text

PIN = "ptgx_8k_2026-08-05_ex991_p3"
WORDS_PATH = FIXTURES / f"{PIN}.words.json"
READING_PATH = FIXTURES / f"{PIN}.events.json"
FAKE_READING_PATH = FIXTURES / f"{PIN}.events.fake.json"

from checks.event_checks import EVENT_TYPES, type_guide   # the one event-type word table (checks/event_checks.py)

INSTRUCTIONS = """\
{assignment}Read {company} ({ticker}), {form} exhibit {exhibit}, filed {filing_date}. The page is {width} by {height}
PDF points. Words below are id|text|x0|y0|x1|y1; origin top-left, y down.
Return one compact JSON object: {{"events": [{{"ticker":"{ticker}","company":"{company}","program":null,
"indication":null,"type_hint":null,"relevance":null,"conditions":[],"subject_company":null,"relation":null,
"title":"...","date_text":"...","date":null,"date_end":null,"quote":"...","word_ids":[],
"box":[x0,y0,x1,y1]}}]}}. Use only source evidence. Preserve separate developments and qualifications. Quote exact
consecutive printed words joined by single spaces, punctuation included; include every quote word id in order.
Program is the printed drug or program; indication is the printed disease or condition; use source-supported wording or null.
Box is the
smallest PDF-point box enclosing the quote; use null if it cannot be placed. Copy date_text exactly. Exact dates use
YYYY-MM-DD with date_end null; quarters, halves, months and years use first and last day in date/date_end; vague timing
has null bounds. Fill context fields only when supported, otherwise null (conditions may be []). No responsive
development means {{"events": []}}. JSON only.

Words:
"""


INSTRUCTIONS_IMAGE = """\
{assignment}Read {company} ({ticker}), {form} exhibit {exhibit}, filed {filing_date}, from the attached page image.
Return one compact JSON object: {{"events": [{{"ticker":"{ticker}","company":"{company}","program":null,
"indication":null,"type_hint":null,"relevance":null,"conditions":[],"subject_company":null,"relation":null,
"title":"...","date_text":"...","date":null,"date_end":null,"quote":"...","box":[x0,y0,x1,y1]}}]}}.
Use only source evidence. Preserve separate developments and qualifications. Quote exact consecutive printed words.
Program is the printed drug or program; indication is the printed disease or condition; use source-supported wording or null. Box is
the smallest quote box on a 0-1000 image grid (origin top-left, x right and y down); use null if it cannot be placed.
Copy date_text exactly. Exact dates use YYYY-MM-DD with date_end null; quarters, halves, months and years use first and
last day in date/date_end; vague timing has null bounds. Fill context fields only when supported, otherwise null
(conditions may be []). No responsive development means {{"events": []}}. JSON only.
"""


def build_image_prompt(entry: dict, event_types: tuple[str, ...] = EVENT_TYPES, note: str | None = None,
                       *, as_of: str | None = None) -> str:
    """The prompt for a read from the page image alone: no word list, so the read is independent of the text layer.
    The attached image is the page; boxes come back on a 0-1000 grid (grid_to_points turns them into PDF points)."""
    brief = note
    return INSTRUCTIONS_IMAGE.format(assignment=research_instructions.prompt_block(brief, as_of),
        company=entry["company"], ticker=entry["ticker"], form=entry["form"], exhibit=entry["exhibit"],
        filing_date=entry["filing_date"], types=", ".join(EVENT_TYPES), guide=type_guide(EVENT_TYPES))


def grid_to_points(box, width_pt: float, height_pt: float) -> list[float] | None:
    """A box on the 0-1000 grid over the page image -> [x0, y0, x1, y1] in PDF points; None if it is not a usable box."""
    if not (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
        return None
    x0, y0, x1, y1 = (min(max(float(v), 0.0), 1000.0) for v in box)
    if x1 <= x0 or y1 <= y0:
        return None
    return [round(x0 * width_pt / 1000, 2), round(y0 * height_pt / 1000, 2),
            round(x1 * width_pt / 1000, 2), round(y1 * height_pt / 1000, 2)]


def load_words(path: Path = WORDS_PATH) -> dict:
    if path == WORDS_PATH:
        from runs.sources import pinned_words
        return pinned_words("PTGX")
    return json.loads(path.read_text())


def build_prompt(words_doc: dict, entry: dict, event_types: tuple[str, ...] = EVENT_TYPES,
                 note: str | None = None, *, as_of: str | None = None) -> str:
    brief = note
    head = INSTRUCTIONS.format(assignment=research_instructions.prompt_block(brief, as_of),
        company=entry["company"], ticker=entry["ticker"], form=entry["form"], exhibit=entry["exhibit"],
        filing_date=entry["filing_date"], width=_num(words_doc["page_width"]),
        height=_num(words_doc["page_height"]), types=", ".join(EVENT_TYPES), guide=type_guide(EVENT_TYPES))
    lines = [
        f"{w['id']}|{w['text']}|{_num(w['x0'])}|{_num(w['y0'])}|{_num(w['x1'])}|{_num(w['y1'])}"
        for w in words_doc["words"]
    ]
    return head + "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parsing


def parse_events(raw: str | None) -> tuple[list | None, str | None]:
    """Return (events, problem). Never guesses: if the shape is wrong, events is None."""
    if not raw or not raw.strip():
        return None, "The model returned an empty answer."
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None, "The answer is not JSON."
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None, "The answer is not JSON."
    if not isinstance(data, dict):
        return None, "The answer is not a JSON object."
    events = data.get("events")
    if not isinstance(events, list):
        return None, "The answer has no list of events."
    if not all(isinstance(e, dict) for e in events):
        return None, "An event is not a JSON object."
    return events, None


# ---------------------------------------------------------------- record


def _record(*, model, prompt, raw, events, problem, entry, extra=None) -> dict:
    rec = {
        "model": model,
        "fake": model == FAKE_MODEL,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pin": PIN,
        "page_sha256": entry.get("page_sha256"),
        # load_words() checks this exact pinned artifact before parsing it; do not reopen it to stamp a digest.
        "words_sha256": entry.get("words_sha256"),
        "prompt_sha256": sha256_text(prompt),
        "prompt_text": prompt,
        "raw_response": raw,
        "readable": events is not None,
        "parse_problem": problem,
        "events": events,
    }
    if extra:
        rec.update(extra)
    return rec


# ---------------------------------------------------------------- the date send-back

SEND_BACK = """\
You read this page earlier and proposed the events below, each with a date text. For each one the date text you gave is
NOT printed in the quote you gave, so the quote does not show the date.
Find, on the page below, the sentence that states the date for the event, and copy it exactly as printed: the quote
must be one run of consecutive words from the page, and the date text must be printed inside it. If the page states
no date for the event, answer null for it. Do not guess a date.
Answer with JSON only: {{"rows": [{{"id": "<the id below>", "quote": "<sentence or null>", "date_text": "<the date as
printed in it or null>"}}]}}

{company} ({ticker}), {form} exhibit {exhibit}, filed {filing_date}.

Events:
{rows}

Page text:
"""


def build_send_back_prompt(words_doc: dict, entry: dict, rows: list[dict]) -> str:
    listed = "\n".join(f"{r['id']}: {r.get('program') or r.get('title') or 'event'}, {r['event_type']}; you gave date text "
                       f"“{r.get('date_text')}” and quote “{r.get('quote')}”" for r in rows)
    text = " ".join(w["text"] for w in words_doc["words"])
    return SEND_BACK.format(company=entry["company"], ticker=entry["ticker"], form=entry.get("form") or "8-K",
                            exhibit=entry.get("exhibit") or "", filing_date=entry["filing_date"], rows=listed) + text + "\n"


def parse_send_back(raw: str | None) -> list[dict] | None:
    """[{id, quote, date_text}] from the answer, or None when it is not that shape. Nothing is guessed."""
    events, _ = parse_events(re.sub(r'"rows"\s*:', '"events":', raw or "", count=1))
    if events is None:
        return None
    return [{"id": e.get("id"), "quote": e.get("quote"), "date_text": e.get("date_text")} for e in events
            if isinstance(e.get("id"), str)]


# ---------------------------------------------------------------- live reading


def read_live(words_doc: dict, entry: dict) -> dict:
    read_table.load_env_file()
    key = os.environ.get("MODEL_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "No model key found. Set MODEL_API_KEY in the environment or in "
            f"{ENV_FILE} as MODEL_API_KEY=\"...\", then run again. (Use --fake to work offline.)"
        )
    from capture import http

    client = http.openai_client(BASE_URL, key)
    del key
    prompt = build_prompt(words_doc, entry)
    messages = [{"role": "user", "content": prompt}]
    base = {"base_url": BASE_URL, "requested_model": MODEL}

    response_format_used = "json_object"
    http.take_billed()
    try:
        resp = read_table._call(client, messages, json_mode=True)
    except Exception as e:  # PIVOT RED 3: no automatic retry on 400/422; the failed attempt is the outcome
        return _failed(prompt, entry, e, {**base, "response_format": response_format_used})

    raw = resp.choices[0].message.content if resp.choices else None
    events, problem = parse_events(raw)
    usage = getattr(resp, "usage", None)
    extra = {**base, "response_format": response_format_used}
    if http.take_billed():
        extra["retried_after_timeout"] = True
    if usage is not None:
        extra["usage"] = {k: getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return _record(model=getattr(resp, "model", None) or MODEL, prompt=prompt, raw=raw, events=events,
                   problem=problem, entry=entry, extra=extra)


def _failed(prompt: str, entry: dict, e: Exception, extra: dict) -> dict:
    """The call did not succeed: record that, with no events. Nothing is made up."""
    from capture import http

    silent = http.is_silent(e)
    return _record(model=MODEL, prompt=prompt, raw=None, events=None,
                   problem=http.did_not_answer(read_table.SPARK_NAME) if silent else _plain_error(e), entry=entry,
                   extra={**extra, "call_failed": True, **({"timed_out": True, "may_have_been_billed": http.may_have_been_billed(e)}
                                                           if silent else read_table.failure_billing(e))})


# ---------------------------------------------------------------- fake reading

_PERIOD = re.compile(r"(Q[1-4] \d{4}|(?:first|second) half of \d{4})")


def build_fake_events(words_doc: dict, entry: dict) -> list[dict]:
    """Plain layout rules: every sentence that starts with a program code ("PN-...") and says "anticipated in"
    becomes a milestone event. No model is called."""
    words = words_doc["words"]
    events = []
    for i, w in enumerate(words):
        if not re.match(r"^PN-\w+$", w["text"]):
            continue
        j = i
        while j < len(words) and not words[j]["text"].endswith("."):
            j += 1
        run = words[i : j + 1]
        quote = " ".join(x["text"] for x in run)
        m = _PERIOD.search(quote)
        if "anticipated in" not in quote or not m:
            continue
        date_text = m[1]
        if date_text.startswith("Q"):
            q, y = int(date_text[1]), int(date_text[3:])
            first, last = {1: ("01-01", "03-31"), 2: ("04-01", "06-30"), 3: ("07-01", "09-30"),
                           4: ("10-01", "12-31")}[q]
        else:
            y = int(date_text[-4:])
            first, last = ("01-01", "06-30") if date_text.startswith("first") else ("07-01", "12-31")
        events.append({
            "ticker": entry["ticker"], "company": entry["company"], "program": w["text"],
            "event_type": "milestone", "title": f"{w['text']} Phase 1 initiation",
            "date_text": date_text, "date": f"{y}-{first}", "date_end": f"{y}-{last}", "quote": quote,
            "word_ids": [x["id"] for x in run],
            "box": [min(x["x0"] for x in run), min(x["y0"] for x in run),
                    max(x["x1"] for x in run), max(x["y1"] for x in run)],
        })
    return events


def read_fake(words_doc: dict, entry: dict) -> dict:
    events = build_fake_events(words_doc, entry)
    return _record(model=FAKE_MODEL, prompt=build_prompt(words_doc, entry), raw=json.dumps({"events": events}),
                   events=events, problem=None, entry=entry,
                   extra={"fake_note": "FAKE reading built by code from the words file. No model was called."})


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="build a labelled fake reading offline")
    ap.add_argument("--out", type=Path, default=None, help="where to save the reading")
    args = ap.parse_args(argv)

    words_doc, entry = load_words(), load_entry(PIN)
    if args.fake:
        reading, out = read_fake(words_doc, entry), args.out or FAKE_READING_PATH
    else:
        reading, out = read_live(words_doc, entry), args.out or READING_PATH
    out.write_text(json.dumps(reading, indent=1, ensure_ascii=False) + "\n")
    if reading.get("call_failed"):
        print(f"The model call failed ({reading['parse_problem']}); the failure is saved -> {out}")
        return 1
    if reading["readable"]:
        print(f"Saved reading by {reading['model']}: {len(reading['events'])} events -> {out}")
    else:
        print(f"Saved an unreadable answer from {reading['model']} ({reading['parse_problem']}) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
