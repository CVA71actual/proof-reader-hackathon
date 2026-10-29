"""The accuracy report: plain text and JSON.

    build_report(key, key_path, table=None, events=None, planted=None, run_log=None) -> dict
    render_text(report) -> str

Per model: values n/N, events m/M, planted wrong values caught k/k, and the mistake log by type; every miss is
listed with its page and what the key expected. "Right value, check failed" is listed apart: the reader had the
value (or event) right and a check stopped it, with the check's own reason text. What a reader proposed outside a
partial key is counted as unscored and is never a miss or a mistake.
"""

from __future__ import annotations

from collections import Counter

from accuracy import types as T
from accuracy.mistake_log import NO_TYPE, log_from_score

REPORT_VERSION = 1


def _fraction(a, b) -> str:
    return f"{a}/{b}"


def _window_group(g: dict) -> dict:
    return {k: g[k] for k in ("count", "proposed_excluded", "proposed_not_excluded", "never_proposed")}


def _miss_lines(table: dict | None, events: dict | None, page: int) -> list[dict]:
    out = []
    if table:
        for bucket in ("wrong_value", "wrong_place", "missed", "extra"):
            for it in table[bucket]:
                out.append({"page": page, "what": "value", "bucket": bucket, "model": table["model"],
                            "row_label": it["row_label"], "column": it["column"], "expected": it["expected"],
                            "got": it["value"], "type": it.get("type"), "caught": it.get("caught")})
    if events:
        for bucket in ("wrong_date", "wrong_type", "unsupported", "missed", "invented"):
            for it in events[bucket]:
                got = None if bucket == "missed" else {f: it.get(f) for f in ("ticker", "program", "event_type",
                                                                              "date_text")}
                out.append({"page": it.get("page_number") or page, "what": "event", "bucket": bucket,
                            "model": events["model"], "expected": it.get("expected"), "got": got,
                            "type": it.get("type"), "caught": it.get("caught")})
    return out


def _right_but_stopped(table: dict | None, events: dict | None, page: int) -> list[dict]:
    """The reader had it right (the key's value in the key's row and column, or the key's event on its date) and a
    check failed or could not run: wrong_place for values, unsupported for events. The reason is the check's own."""
    out = []
    for it in (table or {}).get("wrong_place", []):
        out.append({"page": page, "what": "value", "model": table["model"], "row_label": it["row_label"],
                    "column": it["column"], "value": it["value"], "type": it.get("type"), "status": it.get("status"),
                    "stopped_by": it.get("stopped_by")})
    for it in (events or {}).get("unsupported", []):
        out.append({"page": it.get("page_number") or page, "what": "event", "model": events["model"],
                    "event": {f: it.get(f) for f in ("ticker", "program", "event_type", "date_text")},
                    "type": it.get("type"), "status": it.get("status"), "stopped_by": it.get("stopped_by")})
    return out


def build_report(key: dict, key_path: str | None = None, *, table: dict | None = None, events: dict | None = None,
                 planted: dict | None = None, run_log: dict | None = None, run_id: str | None = None,
                 planted_model: str | None = None) -> dict:
    models: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return models.setdefault(name, {"values": None, "events": None, "planted": None, "mistakes_by_type": {}})

    if table:
        slot(table["model"])["values"] = {"found": table["counts"]["found"], "N": table["N"], **table["counts"]}
    if events:
        # found / M is the in-window number: M is the in-window key events, M_total all of them; the buckets
        # outside_window and unplaced carry the rest (found + wrong + missed + outside + unplaced = M_total)
        slot(events["model"])["events"] = {"found": events["counts"]["found"], "M": events["M_in_window"],
                                           "M_total": events["M"], **events["counts"],
                                           "outside_window": _window_group(events["outside_window"]),
                                           "unplaced": _window_group(events["unplaced"]),
                                           "window": events.get("window")}
    if planted:
        name = planted_model or (table or {}).get("model") or "unknown model"
        slot(name)["planted"] = {"caught": planted["caught"], "total": planted["total"],
                                 "by_kind": planted["by_kind"]}
    scored = log_from_score(table, events)
    for name, m in models.items():
        m["mistakes_by_type"] = dict(Counter(e["type"] for e in scored if e["model"] == name))
    page = key.get("page_number")
    return {
        "report_version": REPORT_VERSION,
        "key": {"path": key_path, "sealed": bool(key.get("sealed")), "source_sha256": key.get("source_sha256"),
                "page_number": page, "cells": len(key.get("cells") or []), "events": len(key.get("events") or []),
                "complete": key.get("complete") is not False, "key_check": "passed"},
        "run": run_id,
        "models": models,
        "table": table, "events": events, "planted": planted,
        "mistake_log": {"from_scoring": [{"type": e["type"], "model": e["model"], "caught_by": e["caught_by"],
                                          "bucket": e["bucket"]} for e in scored],
                        "from_run": run_log},
        "misses": _miss_lines(table, events, page),
        "right_but_stopped": _right_but_stopped(table, events, page),
    }


def _window_lines(e: dict) -> list[str]:
    """The key events the window sets aside, and the rows it removed (never silently dropped)."""
    out = []
    if e.get("window"):
        w = e["window"]
        out.append(f"  time window: {w['start']} to {w['end']} ({w['months']} months); {e['M']} of {e['M_total']} "
                   f"key events are in it")
    for name, label in (("outside_window", "outside the window"), ("unplaced", "unplaced (vague date)")):
        g = e[name]
        if g["count"]:
            out.append(f"  {label}: {g['count']} (proposed and left out by the window {g['proposed_excluded']}, "
                       f"proposed and kept {g['proposed_not_excluded']}, never proposed {g['never_proposed']})")
    if e.get("excluded_by_window"):
        out.append(f"  rows left out by the window, not held against the model: {e['excluded_by_window']}")
    return out


def header_line(name: str, m: dict) -> str:
    """'Muse Spark 69/69 values, 2/2 events; planted errors caught 12/12' (a missing part reads 'not scored')."""
    v = _fraction(m["values"]["found"], m["values"]["N"]) + " values" if m["values"] else "values not scored"
    e = _fraction(m["events"]["found"], m["events"]["M"]) + " events" if m["events"] else "events not scored"
    p = (f"planted errors caught {_fraction(m['planted']['caught'], m['planted']['total'])}" if m["planted"]
         else "planted errors not run")
    return f"{name} {v}, {e}; {p}"


def render_text(rep: dict) -> str:
    k = rep["key"]
    sealed = "sealed" if k["sealed"] else "unsealed, for the tooling only"
    lines = [f"Accuracy report: page {k['page_number']} of source {str(k['source_sha256'])[:12]}... ({sealed})",
             f"Answer key: {k['path'] or 'given in code'}; {k['cells']} values, {k['events']} events; "
             f"every key entry is printed on the page; the key is "
             f"{'complete' if k.get('complete', True) else 'partial (what a reader proposes outside it is unscored, not a mistake)'}.",
             ""]
    if rep.get("run"):
        lines.insert(1, f"Run: {rep['run']}")
    for name, m in rep["models"].items():
        lines.append(header_line(name, m))
        if m["values"]:
            v = m["values"]
            lines.append(f"  values: found {v['found']}, wrong value {v['wrong_value']}, wrong place "
                         f"{v['wrong_place']}, missed {v['missed']}, extra {v['extra']}"
                         + (f", unscored {v['unscored']}" if v.get("unscored") else ""))
        if m["events"]:
            e = m["events"]
            lines.append(f"  events (in the run's window): found {e['found']}, wrong date {e['wrong_date']}, wrong type {e['wrong_type']}, "
                         f"not backed by the checks {e['unsupported']}, missed {e['missed']}, "
                         f"invented {e['invented']}" + (f", unscored {e['unscored']}" if e.get("unscored") else ""))
            lines.extend(_window_lines(e))
        if m["planted"]:
            parts = ", ".join(f"{kind.replace('_', ' ')} {d['caught']}/{d['total']}"
                              for kind, d in m["planted"]["by_kind"].items() if d["total"])
            lines.append(f"  planted wrong values caught by the checks: {m['planted']['caught']}/"
                         f"{m['planted']['total']} ({parts})")
        if m["mistakes_by_type"]:
            lines.append("  mistakes by type: " + ", ".join(f"{t} {n}" for t, n in m["mistakes_by_type"].items()))
        else:
            lines.append("  mistakes by type: none")
    lines.append("")
    planted = rep.get("planted")
    if planted:
        lines.append("Planted wrong values:")
        for it in planted["items"]:
            mark = "caught" if it["caught"] else "NOT CAUGHT"
            lines.append(f"  {it['id']} {mark} as {it['type'] or 'no type'} ({T.label(it['type'])}): {it['what']}")
        lines.append("")
    log = (rep.get("mistake_log") or {}).get("from_run")
    if log is not None:
        lines.append(f"Mistake log of the run (type, model, caught by): {log['total']}")
        for c in log["counts"]:
            lines.append(f"  {c['type']} ({c['label']}), {c['model']}, caught by {c['caught_by']}: {c['n']}")
        lines.append("")
    stopped = rep.get("right_but_stopped") or []
    if stopped:
        lines.append(f"Right value, check failed ({len(stopped)}): the reader had it right and a check stopped it.")
        for s in stopped:
            why = s["stopped_by"] or {}
            what = (f"row '{s['row_label']}', column {s['column']}: {s['value']}" if s["what"] == "value"
                    else f"event {s['event']}")
            lines.append(f"  page {s['page']}, {what} [{s['status']}]: {why.get('check') or 'a check'} "
                         f"{(why.get('result') or '').replace('_', ' ')}: {why.get('reason')}")
        lines.append("")
    # a right value a check stopped is listed above, not twice; it stays in rep["misses"] for the JSON
    misses = [m for m in rep["misses"] if m["bucket"] not in ("wrong_place", "unsupported")]
    if not misses:
        lines.append("Every miss: none. Every key entry was found.")
    else:
        lines.append(f"Every miss ({len(misses)}):")
        for m in misses:
            t = m["type"] if m["type"] and m["type"] != NO_TYPE else "no type"
            flag = "flagged by the checks" if m["caught"] else "not flagged by the checks"
            if m["what"] == "value":
                lines.append(f"  page {m['page']}, {m['bucket'].replace('_', ' ')}: row '{m['row_label']}', column "
                             f"{m['column']}: expected {m['expected']}, got {m['got']} [{t}; {flag}]")
            else:
                lines.append(f"  page {m['page']}, {m['bucket'].replace('_', ' ')}: expected {m['expected']}, "
                             f"got {m['got']} [{t}; {flag}]")
    return "\n".join(lines) + "\n"
