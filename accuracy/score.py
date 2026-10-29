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

A key says whether it is complete (key["complete"]). On a partial key (complete false) a proposal outside the key
is "unscored" (tables: a row and column the key does not hold; events: a program the key does not hold): it is not
a mistake and carries no type. On a complete key those are extra / invented, as before. A column is the same
column however the year is printed ("2025", "FY2025", "December 31, 2025"), and a key event's `aliases` are other
names for its program; the scorer matches any.

Every item that is not found carries a type code (accuracy.types) where one applies, and `caught`: whether the
checks flagged it themselves (a check failed). A wrong item the checks passed is listed with caught false.
"""

from __future__ import annotations

import re

from accuracy import types as T
from accuracy.key import cell_id, event_id, program_names
from checks.common import FAIL, normalize
from checks.coverage import sentence_covered
from checks.event_checks import parse_exact_date, parse_period, vague_period_window
from checks.status import FAILED, MATCHED, check_cell

TABLE_BUCKETS = ("found", "wrong_value", "wrong_place", "missed", "extra", "unscored")
EVENT_BUCKETS = ("found", "wrong_date", "wrong_type", "unsupported", "missed", "invented", "unscored")


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


def _strip(text: str | None) -> str:
    return re.sub(r"^(?:in |during |by )?(?:the )?", "", normalize(text).rstrip(".").lower())


def _program_of(row: dict, names: dict[str, str]) -> str | None:
    """The key program a row is about: its program or its indication when either is the key's program or an
    alias of it, else the one key program named in its title or quote. A program the key does not hold is
    returned as printed."""
    for field in ("program", "indication"):
        said = normalize(row.get(field)).lower()
        if said in names:
            return names[said]
    if row.get("program"):
        return normalize(row["program"]).lower()
    hay = " ".join(normalize(row.get(f)) for f in ("title", "quote")).lower()
    hits = {names[n] for n in sorted(names, key=len, reverse=True) if n and n in hay}
    return hits.pop() if len(hits) == 1 else None


def _event_item(row: dict, expected: dict | None, typ, types=None) -> dict:
    checks = row.get("checks") or []
    return {"id": row.get("id"), "ticker": row.get("ticker"), "program": row.get("program"),
            "event_type": row.get("event_type"), "date_text": row.get("date_text"),
            "page_number": row.get("page_number"), "expected": expected, "type": typ,
            "types": types if types is not None else T.failed_types(checks), "status": row.get("status"),
            "caught": _caught(checks) or row.get("status") in ("unreadable", FAILED),
            "stopped_by": check_reason(checks)}


def _expected(k: dict) -> dict:
    return {f: k[f] for f in ("ticker", "program", "event_type", "date_text")}


def score_events(rows: list[dict], key: dict, exclusions: list[dict] | None = None,
                 model: str | None = None) -> dict:
    key_events = {event_id(e): e for e in key.get("events") or []}
    # Rows code found (a dated sentence no reader row quotes) are not proposals: they are never scored as
    # events, and they name the key events the reader missed that the coverage check surfaced.
    surfaced = [r for r in rows if r.get("found_by") == "coverage"]
    rows = [r for r in rows if r.get("found_by") != "coverage"]
    names = program_names(key)
    complete = is_complete(key)
    excluded = {x.get("target"): x.get("rule") for x in exclusions or []}
    out = {b: [] for b in EVENT_BUCKETS}
    taken: set = set()

    def ident(row):
        return (normalize(row.get("ticker")).upper(), _program_of(row, names),
                normalize(row.get("event_type")).lower())

    rest = []
    for row in rows:
        k = key_events.get(ident(row))
        if k and ident(row) not in taken and row.get("status") == MATCHED and row.get("id") not in excluded \
                and period_of(row.get("date_text")) == period_of(k["date_text"]):
            taken.add(ident(row))
            out["found"].append(_event_item(row, _expected(k), None, []))
        else:
            rest.append(row)
    for row in rest:
        tk, prog, et = ident(row)
        checks = row.get("checks") or []
        if row.get("status") == "unreadable":
            out["invented"].append(_event_item(row, None, T.UNREADABLE_OUTPUT, [T.UNREADABLE_OUTPUT]))
            continue
        same_prog = [k for k in key_events if k[1] == prog]
        if not same_prog and not complete:
            out["unscored"].append(_event_item(row, None, None, []))
            continue
        if not same_prog:
            typ = T.type_for_exclusion(excluded.get(row.get("id"))) or T.primary_type(checks)
            out["invented"].append(_event_item(row, None, typ))
            continue
        same_co = [k for k in same_prog if k[0] == tk]
        if not same_co:
            k = key_events[same_prog[0]]
            out["invented"].append(_event_item(row, _expected(k), T.WRONG_COMPANY))
            continue
        if (tk, prog, et) in key_events:
            kid = (tk, prog, et)
            k = key_events[kid]
            if kid in taken:
                out["invented"].append(_event_item(row, _expected(k), T.DUPLICATE))
                continue
            taken.add(kid)
            if period_of(row.get("date_text")) != period_of(k["date_text"]):
                out["wrong_date"].append(_event_item(row, _expected(k), T.PERIOD_MISREAD))
            else:
                typ = T.type_for_exclusion(excluded.get(row.get("id"))) or T.primary_type(checks)
                out["unsupported"].append(_event_item(row, _expected(k), typ))
            continue
        free = [kid for kid in same_co if kid not in taken]
        if not free:
            out["invented"].append(_event_item(row, _expected(key_events[same_co[0]]), T.DUPLICATE))
            continue
        taken.add(free[0])
        out["wrong_type"].append(_event_item(row, _expected(key_events[free[0]]), T.WRONG_EVENT_TYPE))
    for kid, k in key_events.items():
        if kid not in taken:
            seen = next((c for c in surfaced if sentence_covered(k["quote"], [c.get("quote")])), None)
            out["missed"].append({"ticker": k["ticker"], "program": k["program"], "event_type": k["event_type"],
                                  "date_text": None, "expected": _expected(k),
                                  "type": T.MISSED_DATED_SENTENCE if seen else None,
                                  "types": [T.MISSED_DATED_SENTENCE] if seen else [], "status": None,
                                  "caught": bool(seen), "surfaced_as": seen["id"] if seen else None})
    models = sorted({model_family(r.get("proposed_by")) for r in rows if r.get("proposed_by")})
    out["model"] = model_family(model) if model else (", ".join(models) if models else "unknown model")
    out["complete"] = complete
    out["M"] = len(key_events)
    out["counts"] = {b: len(out[b]) for b in EVENT_BUCKETS}
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


def score_planted(reading: dict, words_doc: dict, plants: list[dict]) -> dict:
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
        checked = check_cell(cell, words_doc, saved_row, columns)
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
