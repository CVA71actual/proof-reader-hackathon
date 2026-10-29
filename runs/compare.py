"""Compare the second reader's reading of a page with the first reader's rows, by code, and record what differs.

    apply_events(rows, second_events, second_reading, source, words_doc, at, next_index)
    apply_table(first_table, second_table)

The second reader (Muse Spark Open, capture/second_reader.py) read the same page without seeing the first
reader's answer. Nothing here asks a model anything.

Events. A first-reader row and a second-reader event are the same event when their quotes overlap (one holds the
other, or one holds 80% of the other's words: the coverage check's own rule) and, when both name a program or an
indication, they share a name. Each second event pairs with at most one row. For a pair these fields are compared:
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
from checks.coverage import sentence_covered
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


def _same_quote(a, b) -> bool:
    return bool(a and b) and (sentence_covered(a, [b]) or sentence_covered(b, [a]))


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


def _instructions_step(reading: dict, at: str) -> dict:
    pt = (reading.get("usage") or {}).get("prompt_tokens")
    return {"step": "Instructions sent to the second reader", "tag": "Code", "who": SECOND_WHO, "at": at,
            "text": reading["prompt_text"], "tokens": {"prompt": pt, "completion": None} if isinstance(pt, int) else None}


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
        row["trail"] += [_instructions_step(second_reading, at), _model_step(ev, shares[j], at)]
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


def apply_table(first_table: dict, second_table: dict) -> list[str]:
    """Mark each value the two readers read differently. Returns the ids of the cells marked."""
    theirs = {}
    for r in second_table.get("rows") or []:
        for c in r.get("cells") or []:
            theirs.setdefault((_norm(r.get("label")), column_key(c.get("column"))), c.get("value"))
    marked = []
    for r in first_table.get("rows") or []:
        for c in r.get("cells") or []:
            key = (_norm(r.get("label")), column_key(c.get("column")))
            if key not in theirs or _norm(c.get("value")) == _norm(theirs[key]):
                continue
            c["status"] = "failed" if any(k["result"] == "fail" for k in c["checks"]) else "undecided"
            c["disagreement"] = {"field": "value", "first_reader": c.get("value"), "second_reader": theirs[key]}
            marked.append(c["id"])
    return marked
