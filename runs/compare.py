"""Compare the second reader's reading of a page with the first reader's rows, by code, and record what differs.

    apply_events(rows, second_events, second_reading, source, words_doc, at, next_index)
    apply_table(first_table, second_table)

The second reader (Muse Spark Open, capture/second_reader.py) read the same page without seeing the first
reader's answer. Nothing here asks a model anything.

Events. A first-reader row and a second-reader event are the same event when one quote is a contiguous run of
words of the other (after normalization: case, spacing and the punctuation at a word's edge ignored) and, when both
name a program or an indication, they share a name. A handful of the same words in another order, or a bag of words
that happens to overlap, never pairs two passages. Each second event pairs with at most one row. For a pair these fields are compared:
date, date_end, event_type, program, indication (case and spacing ignored; none against a value is a difference).
The quote is the match key: quotes that overlap but run to different lengths are not a disagreement.
- Any difference: the row becomes `undecided` (`failed` if a check had failed), group `open`, and carries
  `disagreement` {field, first_reader, second_reader} for the first differing field in the order above (`also`
  lists the other differing fields the same way), so it goes to Judgment needed. Its trail names every differing field. A person's fix that puts in the second reader's value
  is counted as the second reader's catch (runs/state.py).
- No difference: nothing changes. Agreement is not evidence: only the checks say a row matched, and the trail says so.
- A first-reader row with no matching second event: nothing changes; the trail says the second reader read no such event.
- A second-reader event no first-reader row matches: a new open row "Proposed by the second reader only", its own
  checks run on it, so an event the first reader missed gets a second chance in front of a person.

Tables. A value is the same cell when its row label and column (column_key) are the same; a different value makes
the cell `undecided` (`failed` if a check had failed) with `disagreement` {field "value", ...}.

Every trail step has one actor: the instructions sent to the second reader (Code, capture.second_reader), its
reading (Model, Muse Spark Open, with its share of the call's tokens and cost), the comparison (Code, runs.compare).
"""

from __future__ import annotations

from checks import event_checks
from checks.common import normalize
from checks.event_checks import _norm_quote
from checks.row_column_match import column_key
from runs import build_entry, prices

SECOND = "Muse Spark Open"
WHO = "runs.compare"
SECOND_WHO = "capture.second_reader"
FIELDS = ("date", "date_end", "event_type", "program", "indication")
AGREEMENT_NOTE = "Agreement is not a check: the checks above decide."
ONLY_REASON = ("Proposed by the second reader only: the first reader did not read it. Look at it: keep it as an "
               "event, fix it, or remove it.")


def _norm(v) -> str | None:
    s = normalize(v if v is None else str(v)).lower()
    return s or None


def _names(item: dict) -> set[str]:
    return {n for n in (_norm(item.get("program")), _norm(item.get("indication"))) if n}


_EDGE_PUNCT = ".,;:"


def _words(text) -> list[str]:
    return [w for w in (t.strip(_EDGE_PUNCT) for t in _norm_quote(text).lower().split()) if w]


def _same_quote(a, b) -> bool:
    """One quote is a contiguous run of the other's words. Token bags never pair: 'planned and is Q3 2027' is not
    a run of 'Q3 2027 is planned and Q3 2027 is planned', however many of its words that sentence repeats."""
    x, y = _words(a), _words(b)
    if not x or not y:
        return False
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    return any(long_[i : i + len(short)] == short for i in range(len(long_) - len(short) + 1))


def match_events(rows: list[dict], events: list[dict]) -> dict[int, int]:
    """row index -> second-event index. A second event pairs with the first row (in page order) it matches."""
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for i, row in enumerate(rows):
        for j, ev in enumerate(events):
            if j in used or not _same_quote(row.get("quote"), ev.get("quote")):
                continue
            mine, theirs = _names(row), _names(ev)
            if mine and theirs and not mine & theirs:
                continue
            pairs[i] = j
            used.add(j)
            break
    return pairs


def differences(row: dict, ev: dict) -> list[dict]:
    """[{field, first_reader, second_reader}] for each compared field the readers read differently."""
    out = []
    for f in FIELDS:
        a, b = _norm(row.get(f)), _norm(ev.get(f))
        if a != b:
            out.append({"field": f, "first_reader": row.get(f), "second_reader": ev.get(f)})
    return out


def _say(v) -> str:
    return "nothing" if v in (None, "") else str(v)


def _when(ev: dict) -> str:
    if ev.get("date") and ev.get("date_end"):
        return f"{ev['date']} to {ev['date_end']}"
    return ev.get("date") or ev.get("date_text") or "no date"


def _model_step(ev: dict, share: tuple, at: str) -> dict:
    return {"step": "Second reading", "tag": "Model", "who": SECOND, "at": at,
            "text": (f"{ev.get('ticker') or ''} {ev.get('program') or ''} {(ev.get('event_type') or 'unknown')} on "
                     f"{_when(ev)}, printed as “{ev.get('date_text')}”: “{ev.get('quote')}”").replace("  ", " "),
            **prices.model_cost_fields(SECOND, share[0], share[1], call_rows=share[2])}


INPUT_WORDS = {
    "image": "The page image only. The page's words were not sent: this read does not depend on the text layer.",
    "image_and_words": "The page image and the page's words (the fallback after the image-only answer could not be "
                       "read).",
    "words": "The page's words only.",
}


def _instructions_steps(reading: dict, at: str) -> list[dict]:
    """The second reader's own "Instructions sent" (the exact prompt) and which input it got."""
    pt = (reading.get("usage") or {}).get("prompt_tokens")
    got = reading.get("input") or "words"
    return [
        {"step": "Second reader input", "tag": "Code", "who": SECOND_WHO, "at": at, "text": INPUT_WORDS[got]},
        {"step": "Instructions sent to the second reader", "tag": "Code", "who": SECOND_WHO, "at": at,
         "text": reading["prompt_text"], "tokens": {"prompt": pt, "completion": None} if isinstance(pt, int) else None},
    ]


def apply_events(rows: list[dict], second_events: list[dict], second_reading: dict, source: dict, words_doc: dict,
                 at: str, next_index: int) -> tuple[list[dict], list[dict]]:
    """Returns (rows with the comparison recorded, the new second-reader-only rows). `rows`: the first reader's
    rows for this page (event rows the coverage check added are not compared)."""
    mine = [r for r in rows if r.get("found_by") is None and r.get("status") != "unreadable"]
    pairs = match_events(mine, second_events)
    n = len(second_events)
    shares = list(zip(prices.split_tokens((second_reading.get("usage") or {}).get("prompt_tokens"), n),
                      prices.split_tokens((second_reading.get("usage") or {}).get("completion_tokens"), n),
                      [n] * n))
    for i, row in enumerate(mine):
        j = pairs.get(i)
        if j is None:
            row["trail"].append({"step": "Compared", "tag": "Code", "who": WHO, "at": at,
                                 "text": f"The second reader's reading holds no matching event. Nothing changes. "
                                         f"{AGREEMENT_NOTE}"})
            continue
        ev, diffs = second_events[j], differences(row, second_events[j])
        row["trail"] += [*_instructions_steps(second_reading, at), _model_step(ev, shares[j], at)]
        if not diffs:
            row["trail"].append({"step": "Compared", "tag": "Code", "who": WHO, "at": at,
                                 "text": f"Both readers read the same date, event type, program and indication. "
                                         f"{AGREEMENT_NOTE}"})
            continue
        first = diffs[0]
        said = "; ".join(f"{d['field']}: {SECOND_FIRST} read {_say(d['first_reader'])}, {SECOND} read "
                         f"{_say(d['second_reader'])}" for d in diffs)
        row["disagreement"] = {**first, **({"also": diffs[1:]} if len(diffs) > 1 else {})}
        row["status"] = "failed" if any(c["result"] == "fail" for c in row["checks"]) else "undecided"
        row["group"] = "open"
        row["reason"] = (f"{row['reason']} " if row["status"] == "failed" else "") + (
            f"The two readers disagree on {first['field']}: {SECOND_FIRST} read {_say(first['first_reader'])}, "
            f"{SECOND} read {_say(first['second_reader'])}. A person decides.")
        row["trail"].append({"step": "Compared", "tag": "Code", "who": WHO, "at": at,
                             "text": f"The readers differ on {len(diffs)} field{'s' if len(diffs) != 1 else ''}. {said}. "
                                     f"The row goes to a person; the disagreement is on {first['field']}."})
    new_rows = []
    matched = set(pairs.values())
    for j, ev in enumerate(second_events):
        if j in matched:
            continue
        if not (source or {}).get("pages") or "words" not in (words_doc or {}):
            row = _uncheckable_row(next_index + len(new_rows), ev, source, second_reading, shares[j], at)
            new_rows.append(row)
            continue
        row = build_entry.build_row(next_index + len(new_rows), ev, second_reading, source, words_doc, at,
                                    share=shares[j], model_name=SECOND, instructions_who=SECOND_WHO)
        failed = any(c["result"] == "fail" for c in row["checks"])
        row["status"] = event_checks.row_status(row["checks"], second_reader_agrees=False)
        row["group"], row["found_by"] = "open", "second_reader"
        row["reason"] = (f"{row['reason']} " if failed else "") + ONLY_REASON
        row["trail"].append({"step": "Compared", "tag": "Code", "who": WHO, "at": at,
                             "text": f"No row from the first reader matches this event. {AGREEMENT_NOTE}"})
        new_rows.append(row)
    return rows, new_rows


SECOND_FIRST = "Muse Spark"


def _uncheckable_row(index: int, ev: dict, source: dict | None, reading: dict, share: tuple, at: str) -> dict:
    """An event only the second reader gave, on a page that was not captured (no source page or no words): it is kept as
    a second-reader row, undecided and open, with no check run on it and the reason said plainly."""
    return {
        "id": f"e{index}", "ticker": ev.get("ticker") or "", "company": ev.get("company") or "",
        "event_type": (ev.get("event_type") or "unknown"), "title": ev.get("title") or "",
        "date_text": ev.get("date_text"), "date": ev.get("date"), "date_end": ev.get("date_end"),
        "quote": ev.get("quote"), "program": ev.get("program"), "indication": ev.get("indication"),
        "source_id": (source or {}).get("id"), "page_number": None, "box": None, "proposed_by": SECOND,
        "found_by": "second_reader", "status": "undecided", "group": "open",
        "reason": f"{ONLY_REASON} The page was not captured, so no check could run on it.", "sample": False,
        "checks": [],
        "trail": [*_instructions_steps(reading, at), _model_step(ev, share, at),
                  {"step": "Compared", "tag": "Code", "who": WHO, "at": at,
                   "text": f"No row from the first reader matches this event, and the page was not captured, so no "
                           f"check ran. {AGREEMENT_NOTE}"}],
    }


def apply_table(first_table: dict, second_table: dict) -> list[str]:
    """Mark each value the two readers read differently. Returns the ids of the cells marked. The second reader may give
    more than one value for a cell (two rows with the same label, a repeated column): every one is kept, and a value that
    differs from the first reader's is a disagreement even when another of its values agrees. None is dropped."""
    theirs: dict[tuple, list] = {}
    for r in second_table.get("rows") or []:
        for c in r.get("cells") or []:
            theirs.setdefault((_norm(r.get("label")), column_key(c.get("column"))), []).append(c.get("value"))
    marked = []
    for r in first_table.get("rows") or []:
        for c in r.get("cells") or []:
            values = theirs.get((_norm(r.get("label")), column_key(c.get("column"))))
            differing = [v for v in values or [] if _norm(c.get("value")) != _norm(v)]
            if not differing:
                continue
            c["status"] = "failed" if any(k["result"] == "fail" for k in c["checks"]) else "undecided"
            c["disagreement"] = {"field": "value", "first_reader": c.get("value"), "second_reader": differing[0]}
            marked.append(c["id"])
    return marked
