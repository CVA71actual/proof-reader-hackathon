"""Score what a reader proposed against a checked answer key.

    score_table(table, key, model=None)        table: check_reading()'s output, or a run.json table
    score_events(rows, key, exclusions=None)   rows: run.json rows (each with its checks and status)
    score_planted(reading, words_doc, plants)  plants: planted wrong values; runs the real checks on each

Table values, per key value (N = len(key cells)):
    found        right value, saved under the key's row and column, and every check passed (status matched:
                 the box holds the value and sits on the row's line under the column)
    wrong_value  proposed for the key's row and column with a different value
    wrong_place  the right value for the key's row and column, but a check on its place failed or could not run
                 (the box drifts off the value, or sits in another row or column)
    missed       in the key, never proposed
    extra        proposed for a row and column the key does not hold, or proposed twice (DUPLICATE)
    found + wrong_value + wrong_place + missed = N.

Events, per key event (M = len(key events)):
    found        same ticker, program, event type and date (the same day or period), and every check passed
    wrong_date   same ticker, program and event type, another day or period (PERIOD_MISREAD)
    wrong_type   same ticker and program, another event type (WRONG_EVENT_TYPE)
    unsupported  same ticker, program, event type and date, but a check failed or could not run (the quote does
                 not back it)
    missed       in the key, never proposed
    invented     proposed but not in the key: another program, another company (WRONG_COMPANY), the same event
                 twice (DUPLICATE), or an answer that could not be read (UNREADABLE_OUTPUT)
    found + wrong_date + wrong_type + unsupported + missed = M.

The six-month horizon organizes the calendar only; run scoring passes no window, so every key event is scored.
`score_events` can still accept an explicit window for standalone callers. With one, every key event gets
`in_window`: True when its first day (the day, or the first day of its period) lies in [start, start + months],
False when outside, None when its date is a vague period that cannot be placed ("mid-2027"). The other events
are not dropped:
    outside_window  key events outside the window: proposed_excluded (a row the run proposed and left out under
                    outside_time_window), proposed_not_excluded (proposed, not left out), never_proposed
    unplaced        key events with in_window None, split the same way
    found + wrong_date + wrong_type + unsupported + missed + outside_window + unplaced = M.
A proposed row not in the key is `invented` (in the window: it counts against the model) or `excluded_by_window`
(left out by the window: it does not). With no window, every key event is in the window.

A key says whether it is complete (key["complete"]). On a partial key (complete false) a proposal outside the key
is "unscored" (tables: a row and column the key does not hold; events: a program the key does not hold): it is not
a mistake and carries no type. On a complete key those are extra / invented, as before. A column is the same
column however the year is printed ("2025", "FY2025", "December 31, 2025"), and a key event's `aliases` are other
names for its program; the scorer matches any.

Every item that is not found carries a type code (accuracy.types) where one applies, and `caught`: whether the
checks flagged it themselves (a check failed). A wrong item the checks passed is listed with caught false.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

from accuracy import types as T
from accuracy.key import cell_id, event_indication_names, event_program_names
from checks.common import FAIL, normalize
from checks.coverage import sentence_covered
from checks.event_checks import (AMBIGUOUS_TYPES, _words_in, parse_exact_date, parse_period, type_group,
                                 vague_period_window)
from checks.status import FAILED, MATCHED, check_cell

TABLE_BUCKETS = ("found", "wrong_value", "wrong_place", "missed", "extra", "unscored")
EVENT_BUCKETS = ("found", "wrong_date", "wrong_type", "unsupported", "missed", "invented", "unscored")
KEY_EVENT_BUCKETS = ("found", "wrong_date", "wrong_type", "unsupported", "missed")
WINDOW_RULE = "outside_time_window"


BOTH_READERS = "Both readers"      # the bucket for rows both readers touched; its "models" lists the two names


def model_family(name: str | None) -> str:
    """'Muse Spark (muse-spark-1.3)' or 'muse-spark-1.3' -> 'Muse Spark'. Unknown names are kept as given."""
    if not name:
        return "unknown model"
    s = re.sub(r"\s*\(.*\)\s*$", "", str(name)).strip().split("/")[-1]
    known = {"muse-spark-open": "Muse Spark Open", "muse-spark": "Muse Spark", "muse-glimmer": "Muse Glimmer",
             "muse-wonder": "Muse Wonder"}
    low = s.lower()
    for prefix, fam in known.items():
        if low.startswith(prefix):
            return fam
    return s


def _caught(checks: list[dict]) -> bool:
    return any(c.get("result") == FAIL for c in checks or [])


def check_reason(checks: list[dict]) -> dict:
    """The check that stopped an item (the first failed one, else the first that could not run): its name, result
    and its reason text as the check wrote it. All None when no check stopped it."""
    for result in (FAIL, "could_not_run"):
        for c in checks or []:
            if c.get("result") == result:
                return {"check": c.get("name") or c.get("check"), "result": result, "reason": c.get("reason")}
    return {"check": None, "result": None, "reason": None}


# ---------------------------------------------------------------- table


def _table_cells(table: dict) -> list[dict]:
    out = []
    for row in table.get("rows") or []:
        for c in row.get("cells") or []:
            out.append({"id": c.get("id"), "row_label": row.get("label") or "", "column": c.get("column"),
                        "value": c.get("value"), "box": c.get("box"), "status": c.get("status"),
                        "checks": c.get("checks") or []})
    return out


def _cell_item(p: dict, expected, typ, types=None) -> dict:
    return {"id": p.get("id"), "row_label": p["row_label"], "column": p["column"], "value": p["value"],
            "expected": expected, "type": typ, "types": types if types is not None else T.failed_types(p["checks"]),
            "status": p["status"], "caught": _caught(p["checks"]), "stopped_by": check_reason(p["checks"])}


def is_complete(key: dict) -> bool:
    """A key that does not say otherwise counts everything outside it (check_key refuses a key that is silent)."""
    return key.get("complete") is not False


def score_table(table: dict, key: dict, model: str | None = None) -> dict:
    key_cells = {cell_id(c): c for c in key.get("cells") or []}
    complete = is_complete(key)
    out = {b: [] for b in TABLE_BUCKETS}
    tm = table.get("model")
    model = model_family(model or (tm.get("name") if isinstance(tm, dict) else tm))
    if table.get("status") == "unreadable":
        out["unreadable"] = True
        for c in key_cells.values():
            out["missed"].append({"row_label": c["row_label"], "column": c["column"], "value": None,
                                  "expected": c["value"], "type": T.UNREADABLE_OUTPUT, "types": [T.UNREADABLE_OUTPUT],
                                  "status": "unreadable", "caught": True})
        return _table_totals(out, key_cells, model, complete)

    props = _table_cells(table)
    taken: set = set()
    rest = []
    # first pass: the proposals that are found, so a wrong proposal earlier in the table cannot hide one
    for p in props:
        k = key_cells.get(cell_id(p))
        if k and cell_id(p) not in taken and normalize(p["value"]) == normalize(k["value"]) \
                and p["status"] == MATCHED:
            taken.add(cell_id(p))
            out["found"].append(_cell_item(p, k["value"], None, []))
        else:
            rest.append(p)
    key_rows = {cell_id(c)[0] for c in key_cells.values()}
    key_cols = {cell_id(c)[1] for c in key_cells.values()}
    for p in rest:
        k = key_cells.get(cell_id(p))
        if k is None and not complete:
            out["unscored"].append(_cell_item(p, None, None, []))
        elif k is None:
            typ = T.primary_type(p["checks"])
            if typ is None:
                typ = T.WRONG_COLUMN if cell_id(p)[0] in key_rows and cell_id(p)[1] not in key_cols else T.WRONG_ROW
            out["extra"].append(_cell_item(p, None, typ))
        elif cell_id(p) in taken:
            out["extra"].append(_cell_item(p, k["value"], T.DUPLICATE))
        elif normalize(p["value"]) != normalize(k["value"]):
            taken.add(cell_id(p))
            out["wrong_value"].append(_cell_item(p, k["value"], T.WRONG_VALUE))
        else:
            taken.add(cell_id(p))
            typ = T.primary_type(p["checks"]) or T.BOX_DRIFT
            out["wrong_place"].append(_cell_item(p, k["value"], typ))
    for cid, k in key_cells.items():
        if cid not in taken:
            out["missed"].append({"row_label": k["row_label"], "column": k["column"], "value": None,
                                  "expected": k["value"], "type": None, "types": [], "status": None,
                                  "caught": False})
    return _table_totals(out, key_cells, model, complete)


def _table_totals(out: dict, key_cells: dict, model: str, complete: bool) -> dict:
    out["model"] = model
    out["complete"] = complete
    out["N"] = len(key_cells)
    out["counts"] = {b: len(out[b]) for b in TABLE_BUCKETS}
    return out


# ---------------------------------------------------------------- events


def period_of(text: str | None):
    """A date or period -> (first day, last day) as ISO strings; a vague period -> its rough window;
    anything else -> its canonical words."""
    d = parse_exact_date(text)
    if d is not None:
        return d.isoformat(), d.isoformat()
    p = parse_period(text)
    if p is not None and p[0] is not None:
        return p[0].isoformat(), p[1].isoformat()
    w = vague_period_window(_strip(text))
    if w is not None:
        return w[0].isoformat(), w[1].isoformat()
    return normalize(text).rstrip(".").lower()


def add_months(d: date, months: int) -> date:
    """The same day `months` later, or the month's last day (the rule server.jobs.add_months applies)."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def run_window(run: dict) -> tuple[date, int] | None:
    """There is no score window: the six-month horizon organizes the calendar, not discovery or accuracy."""
    return None


def in_window(date_text: str | None, window: tuple[date, int] | None) -> bool | None:
    """Whether a key event's first day lies in [start, start + months]. No window: True. A date or period with a
    fixed first day is placed; a vague period ("mid-2027") or text that is no date is None."""
    if window is None:
        return True
    first = parse_exact_date(date_text)
    if first is None:
        p = parse_period(date_text)
        first = p[0] if p is not None else None
    if first is None:
        return None
    start, months = window
    return start <= first <= add_months(start, months)


def _strip(text: str | None) -> str:
    return re.sub(r"^(?:in |during |by )?(?:the )?", "", normalize(text).rstrip(".").lower())


def _row_names(row: dict, every_name: set[str]) -> set[str]:
    """The names a row goes by: its program and its indication; a row that gives neither is searched for the one key
    name its title or quote holds (the longest names first)."""
    own = {normalize(row.get(f)).lower() for f in ("program", "indication") if normalize(row.get(f))}
    if own:
        return own
    hay = " ".join(normalize(row.get(f)) for f in ("title", "quote")).lower()
    hits = [n for n in sorted(every_name, key=len, reverse=True) if n in hay]
    return {hits[0]} if hits else set()


def _event_item(row: dict, expected: dict | None, typ, types=None, note: str | None = None) -> dict:
    checks = row.get("checks") or []
    item = {"id": row.get("id"), "ticker": row.get("ticker"), "program": row.get("program"),
            "indication": row.get("indication"), "event_type": row.get("event_type"),
            "date_text": row.get("date_text"), "page_number": row.get("page_number"), "expected": expected,
            "type": typ, "types": types if types is not None else T.failed_types(checks),
            "status": row.get("status"), "caught": _caught(checks) or row.get("status") in ("unreadable", FAILED),
            "stopped_by": check_reason(checks)}
    if note:
        item["note"] = note
    return item


def _expected(k: dict) -> dict:
    out = {f: k[f] for f in ("ticker", "program", "event_type", "date_text")}
    if k.get("indication"):
        out["indication"] = k["indication"]
    return out


def _type_tie(row_type: str | None, key_type: str | None, quote: str | None) -> bool:
    """The reader and the key differ on milestone versus trial results, and the quote holds words of both: it does not
    say which, so the key's choice is not held against the reader (checks.event_checks.AMBIGUOUS_TYPES)."""
    groups = {type_group(row_type), type_group(key_type)}
    return (len(groups) == 2 and groups == set(AMBIGUOUS_TYPES)
            and all(_words_in(quote or "", g) for g in AMBIGUOUS_TYPES))


TIE_NOTE = ("The quote holds words of both milestone and trial results, so the event type the reader gave "
            "({row}) is not held against it; the key says {key}.")


def score_events(rows: list[dict], key: dict, exclusions: list[dict] | None = None,
                 model: str | None = None, window: tuple[date, int] | None = None) -> dict:
    """Match each proposed row to a key event: the same company and the same date or period, and the row's program OR
    its indication is a name of the key event (a program name or alias, an indication or indication alias). One drug
    with three indications on a page is three key events, matched by their dates and indications; a key may carry the
    same drug alias on several. The kind of event is compared after the match: a different kind is `wrong_type`
    unless the quote holds words of both milestone and trial results (then found, with a note).
    `window`: optional explicit (start day, months); run scoring currently passes None, so the six-month calendar
    horizon does not limit accuracy. When supplied, outside/unplaced key events leave the five main buckets."""
    kes = list(key.get("events") or [])
    complete = is_complete(key)
    # Rows code found (a dated sentence no reader row quotes) are not proposals: they are never scored as
    # events, and they name the key events the reader missed that the coverage check surfaced.
    surfaced = [r for r in rows if r.get("found_by") == "coverage"]
    rows = [r for r in rows if r.get("found_by") != "coverage"]
    prog = [event_program_names(e) for e in kes]
    ind = [event_indication_names(e) for e in kes]
    every = set().union(*prog, *ind) if kes else set()
    excluded = {x.get("target"): x.get("rule") for x in exclusions or []}
    out = {b: [] for b in EVENT_BUCKETS}
    out["excluded_by_window"] = []
    taken: set[int] = set()
    by_key: dict[int, dict] = {}                    # key event index -> the item that scored it (and its row)
    rows_of: dict[int, dict] = {}

    def hits(row) -> list[int]:
        names = _row_names(row, every)
        return [i for i in range(len(kes)) if names & (prog[i] | ind[i])]

    def same_period(row, i) -> bool:
        return period_of(row.get("date_text")) == period_of(kes[i]["date_text"])

    def best(row, idx: list[int]) -> int:
        """Prefer the key event whose indication the row names, then one whose program it names, then key order."""
        mine = _row_names(row, every)
        return max(idx, key=lambda i: (2 if mine & ind[i] else 1 if mine & prog[i] else 0, -i))

    def same_ticker(row, i) -> bool:
        return normalize(row.get("ticker")).upper() == normalize(kes[i].get("ticker")).upper()

    def type_state(row, i) -> str | None:
        """'same', 'tie' (found with a note) or None."""
        a, b = normalize(row.get("event_type")).lower(), normalize(kes[i].get("event_type")).lower()
        if a == b:
            return "same"
        return "tie" if _type_tie(a, b, row.get("quote")) else None

    rest = []
    for row in rows:
        c = [i for i in hits(row) if i not in taken and same_ticker(row, i) and same_period(row, i)
             and type_state(row, i)]
        if c and row.get("status") == MATCHED and row.get("id") not in excluded:
            i = best(row, c)
            taken.add(i)
            note = (TIE_NOTE.format(row=row.get("event_type"), key=kes[i]["event_type"])
                    if type_state(row, i) == "tie" else None)
            out["found"].append(_event_item(row, _expected(kes[i]), None, [], note))
            by_key[i], rows_of[i] = out["found"][-1], row
        else:
            rest.append(row)
    for row in rest:
        checks = row.get("checks") or []
        by_window = excluded.get(row.get("id")) == WINDOW_RULE
        invented = out["excluded_by_window"] if by_window else out["invented"]
        if row.get("status") == "unreadable":
            out["invented"].append(_event_item(row, None, T.UNREADABLE_OUTPUT, [T.UNREADABLE_OUTPUT]))
            continue
        named = hits(row)
        if not named and not complete:
            out["unscored"].append(_event_item(row, None, None, []))
            continue
        if not named:
            typ = T.type_for_exclusion(excluded.get(row.get("id"))) or T.primary_type(checks)
            invented.append(_event_item(row, None, typ))
            continue
        co = [i for i in named if same_ticker(row, i)]
        if not co:
            invented.append(_event_item(row, _expected(kes[named[0]]), T.WRONG_COMPANY))
            continue
        free = [i for i in co if i not in taken]
        on_date = [i for i in free if same_period(row, i)]
        again = [i for i in co if i in taken and same_period(row, i)]
        if not free or (again and not on_date):      # a second row for an event already matched, on its own date
            invented.append(_event_item(row, _expected(kes[(again or co)[0]]), T.DUPLICATE))
            continue
        i = best(row, on_date or free)
        taken.add(i)
        k, kind = kes[i], type_state(row, i)
        if on_date and kind:
            typ = T.type_for_exclusion(excluded.get(row.get("id"))) or T.primary_type(checks)
            bucket, item = "unsupported", _event_item(row, _expected(k), typ)
        elif kind:
            bucket, item = "wrong_date", _event_item(row, _expected(k), T.PERIOD_MISREAD)
        else:
            bucket, item = "wrong_type", _event_item(row, _expected(k), T.WRONG_EVENT_TYPE)
        out[bucket].append(item)
        by_key[i], rows_of[i] = item, row
    for i, k in enumerate(kes):
        if i not in taken:
            seen = next((c for c in surfaced if sentence_covered(k["quote"], [c.get("quote")])), None)
            out["missed"].append({"ticker": k["ticker"], "program": k["program"], "event_type": k["event_type"],
                                  "date_text": None, "expected": _expected(k),
                                  "type": T.MISSED_DATED_SENTENCE if seen else None,
                                  "types": [T.MISSED_DATED_SENTENCE] if seen else [], "status": None,
                                  "caught": bool(seen), "surfaced_as": seen["id"] if seen else None})
            by_key[i] = out["missed"][-1]
    # a key event outside the window, or one that cannot be placed, leaves the five buckets for its own
    for name in ("outside_window", "unplaced"):
        out[name] = {"count": 0, "proposed_excluded": 0, "proposed_not_excluded": 0, "never_proposed": 0, "items": []}
    flags = [in_window(k["date_text"], window) for k in kes]
    for i, flag in enumerate(flags):
        if flag is True:
            continue
        item = by_key[i]
        for b in KEY_EVENT_BUCKETS:
            out[b] = [x for x in out[b] if x is not item]
        if i not in rows_of:
            state = "never_proposed"
        elif excluded.get(rows_of[i].get("id")) == WINDOW_RULE:
            state = "proposed_excluded"
        else:
            state = "proposed_not_excluded"
        group = out["outside_window" if flag is False else "unplaced"]
        group[state] += 1
        group["count"] += 1
        group["items"].append({"expected": _expected(kes[i]), "in_window": flag, "state": state,
                               "id": item.get("id"), "status": item.get("status")})

    models = sorted({model_family(r.get("proposed_by")) for r in rows if r.get("proposed_by")})
    if model:
        out["model"], out["models"] = model_family(model), [model_family(model)]
    elif len(models) > 1:
        out["model"], out["models"] = BOTH_READERS, models
    else:
        out["model"] = models[0] if models else "unknown model"
        out["models"] = models
    out["complete"] = complete
    out["M"] = len(kes)
    out["M_in_window"] = flags.count(True)
    out["window"] = ({"start": window[0].isoformat(), "months": window[1],
                      "end": add_months(window[0], window[1]).isoformat()} if window else None)
    out["counts"] = {b: len(out[b]) for b in EVENT_BUCKETS}
    out["counts"]["invented_in_window"] = len(out["invented"])
    out["counts"]["excluded_by_window"] = len(out["excluded_by_window"])
    out["counts"]["outside_window"] = out["outside_window"]["count"]
    out["counts"]["unplaced"] = out["unplaced"]["count"]
    return out


# ---------------------------------------------------------------- planted wrong values


PLANT_KINDS = ("wrong_value", "wrong_place", "box_drift")


def _find_cell(reading: dict, row_label: str, column: str) -> dict | None:
    for row in (reading.get("table") or {}).get("rows") or []:
        if normalize(row.get("label")) == normalize(row_label):
            for c in row.get("cells") or []:
                if normalize(c.get("column")) == normalize(column):
                    return c
    return None


def score_planted(reading: dict, words_doc: dict, plants: list[dict], *,
                  visual_evidence: dict | None = None, derived_words_present: bool = False) -> dict:
    """Plant each wrong value into a copy of one real cell, run the real checks on it, and count how many the
    checks caught (status failed). A plant: {id, kind, row_label, column, set: {value?, column?, box?,
    row_label?}, expected_type, what}. `row_label` and `column` name the real cell it starts from; `set`
    holds what is changed (set.row_label: the row the planted value is saved under)."""
    columns = [str(c) for c in (reading.get("table") or {}).get("columns", [])] or None
    items = []
    for p in plants:
        base = _find_cell(reading, p["row_label"], p["column"])
        if base is None:
            raise ValueError(f"Plant {p.get('id')}: the reading has no value for {p['row_label']} / {p['column']}.")
        change = p.get("set") or {}
        cell = {k: v for k, v in base.items() if k in ("column", "value", "box", "label")}
        cell.update({k: v for k, v in change.items() if k != "row_label"})
        saved_row = change.get("row_label", p["row_label"])
        checked = check_cell(cell, words_doc, saved_row, columns, visual_evidence=visual_evidence,
                             derived_words_present=derived_words_present)
        typ = T.primary_type(checked["checks"])
        items.append({"id": p.get("id"), "kind": p.get("kind"), "what": p.get("what"), "row_label": saved_row,
                      "column": cell.get("column"), "value": cell.get("value"), "box": cell.get("box"),
                      "status": checked["status"], "caught": checked["status"] == FAILED, "type": typ,
                      "types": T.failed_types(checked["checks"]), "expected_type": p.get("expected_type"),
                      "type_as_planted": typ == p.get("expected_type"),
                      "failed_checks": [c["check"] for c in checked["checks"] if c["result"] == FAIL]})
    caught = sum(1 for i in items if i["caught"])
    return {"total": len(items), "caught": caught, "items": items,
            "by_kind": {k: {"total": sum(1 for i in items if i["kind"] == k),
                            "caught": sum(1 for i in items if i["kind"] == k and i["caught"])}
                        for k in PLANT_KINDS}}
