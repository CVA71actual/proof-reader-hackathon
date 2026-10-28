"""The state rule: what each row (and each table value) looks like after a person's decisions.

current_state(run, decisions) is pure: it reads the run.json dict and the list of decision dicts
(one per decisions.jsonl line, oldest first) and returns a new dict. No files, no clock, no network.

One state per target, set by the latest decision still in effect:
    keep    -> group "kept"
    fix     -> group "corrected"; the field changes from -> to; the target needs its checks run again
               (needs_recheck true, recheck null) until code writes a re-check for that fix
    recheck -> written by code (by "code"), never by a person: the checks were run again on the fixed
               value. It belongs to the target's latest fix in effect that has no re-check yet.
               result "pass": the fix stands (group "corrected", status "matched").
               result "fail": the row goes back to group "open" with status "failed".
    remove  -> group "removed"; the person's note is the reason
    undo    -> takes back that target's most recent decision still in effect, restoring the state
               before it (undo twice takes back two decisions). Undo of a fix also takes back its
               re-check; a re-check is never undone on its own.
With no decisions in effect, a target is as the run left it.

Table values (cells) have no group in run.json; their starting group comes from their status:
matched -> kept, failed -> open, anything else -> unverified.

Decisions that cannot apply (unknown target, unknown action, nothing to undo, a fix whose "from"
is not the current value, ...) change nothing and are listed under "ignored" with a reason.

A person's choice is not a check result: a decision changes a target's group, never its status,
and the target is marked decided_by_person with last_action, so the screen can say "Kept by you"
and still show what the checks said.

Mistakes and exclusions are kept apart:
    mistakes    a value that was wrong and got changed. From run.json's mistakes[] (caught by code,
                the second reader or the person during the run) plus, for the person, every fix
                still in effect whose re-check passed. A fix not yet re-checked is counted in
                counts.fixes_pending instead; a fix whose re-check failed is not counted at all.
                A fix always changes the value (a fix with from == to is ignored).
    exclusions  a row left out under a rule, not because it was wrong: run.json's exclusions[]
                whose row is still removed and untouched by the person, plus every row whose latest
                decision in effect is the person's remove (rule "person_removed").
Undo takes back a fix or a remove, so it lowers whichever counter that decision raised.
"""

from __future__ import annotations

import copy

GROUPS = ("kept", "corrected", "removed", "unverified", "open")
ACTIONS = ("keep", "fix", "remove", "undo", "recheck")
PERSON_ACTIONS = ("keep", "fix", "remove", "undo")
RECHECK_RESULTS = ("pass", "fail")
CAUGHT_BY = ("code", "second_reader", "person")
EXCLUDED_BY = ("code", "person")
UNRESOLVED = ("open", "unverified")

ROW_FIELDS = ("ticker", "company", "event_type", "title", "date_text", "date", "date_end", "quote")
CELL_FIELDS = ("value",)

_CELL_GROUP = {"matched": "kept", "failed": "open"}


def _base_targets(run: dict) -> dict:
    targets = {}
    for row in run.get("rows", []):
        targets[row["id"]] = {
            "kind": "row",
            "group": row["group"],
            "status": row["status"],
            "reason": row.get("reason"),
            "values": {f: row.get(f) for f in ROW_FIELDS},
        }
    for table in run.get("tables", []):
        for trow in table.get("rows", []):
            for cell in trow.get("cells", []):
                targets[cell["id"]] = {
                    "kind": "cell",
                    "group": _CELL_GROUP.get(cell["status"], "unverified"),
                    "status": cell["status"],
                    "reason": None,
                    "values": {"value": cell.get("value")},
                }
    return targets


def _apply(base: dict, stack: list[dict]) -> dict:
    s = copy.deepcopy(base)
    s["fixes"] = []
    s["before"] = {}
    s["recheck"] = None
    for d in stack:
        action, note = d["action"], d.get("note")
        if action == "keep":
            s["group"] = "kept"
            s["reason"] = note or "Kept by you."
        elif action == "remove":
            s["group"] = "removed"
            s["reason"] = note or "Removed by you."
        elif action == "fix":
            fx = d["fix"]
            s["before"].setdefault(fx["field"], base["values"][fx["field"]])
            s["values"][fx["field"]] = fx["to"]
            rc = d.get("recheck")
            s["fixes"].append({"field": fx["field"], "from": fx["from"], "to": fx["to"], "at": d.get("at"),
                               "recheck": rc})
            s["group"] = "corrected"
            s["reason"] = note or f"Fixed by you: {fx['field']} changed from {fx['from']} to {fx['to']}."
            if rc is not None:
                s["recheck"] = rc
                if rc["result"] == "pass":
                    s["status"] = "matched"
                    s["reason"] = rc.get("reason") or "The checks were run again on the fixed value and passed."
                else:
                    s["group"] = "open"
                    s["status"] = "failed"
                    s["reason"] = rc.get("reason") or "The checks were run again on the fixed value and failed."
    last_fix = s["fixes"][-1] if s["fixes"] else None
    s["recheck"] = last_fix["recheck"] if last_fix else None
    s["needs_recheck"] = bool(last_fix) and last_fix["recheck"] is None
    s["decisions_in_effect"] = len(stack)
    s["decided_by_person"] = bool(stack)
    s["last_action"] = stack[-1]["action"] if stack else None
    return s


def _why_not(d, targets: dict, stacks: dict) -> str | None:
    """None if the decision can apply now, else a plain reason why it is ignored."""
    if not isinstance(d, dict):
        return "The line is not a decision."
    action, target = d.get("action"), d.get("target")
    if action not in ACTIONS:
        return f"Unknown action {action!r}."
    if action == "recheck":
        if d.get("by") != "code":
            return "Only code writes a re-check."
    elif d.get("by") != "person":
        return "Only a person's decisions are counted."
    if target not in targets:
        return f"No row or table value has the id {target!r}."
    if action == "recheck":
        rc = d.get("recheck")
        if not isinstance(rc, dict) or rc.get("result") not in RECHECK_RESULTS:
            return "A re-check needs a result of pass or fail."
        if _waiting_fix(stacks[target]) is None:
            return "There is no fix waiting for a re-check on this target."
        return None
    if action == "undo":
        return None if stacks[target] else "There is nothing to undo for this target."
    if action == "fix":
        fx = d.get("fix")
        if not isinstance(fx, dict) or not {"field", "from", "to"} <= fx.keys():
            return "A fix needs a field, a from value and a to value."
        allowed = ROW_FIELDS if targets[target]["kind"] == "row" else CELL_FIELDS
        if fx["field"] not in allowed:
            return f"The field {fx['field']!r} cannot be fixed here."
        current = _apply(targets[target], stacks[target])["values"][fx["field"]]
        if fx["from"] != current:
            return f"The fix starts from {fx['from']!r} but the current value is {current!r}."
        if fx["from"] == fx["to"]:
            return "The fix does not change anything."
    return None


def _waiting_fix(stack: list[dict]) -> int | None:
    """Index of the latest fix in effect, if it has no re-check yet; else None."""
    for i in range(len(stack) - 1, -1, -1):
        if stack[i]["action"] == "fix":
            return i if stack[i].get("recheck") is None else None
    return None


def _exclusions(run: dict, states: dict) -> list[dict]:
    out = []
    for x in run.get("exclusions", []):
        st = states.get(x.get("target"))
        if st and st["group"] == "removed" and not st["decided_by_person"]:
            out.append({"target": x["target"], "rule": x["rule"], "reason": x["reason"], "by": x["by"]})
    for t, st in states.items():
        if st["last_action"] == "remove":
            out.append({"target": t, "rule": "person_removed", "reason": st["reason"], "by": "person"})
    return out


def current_state(run: dict, decisions: list) -> dict:
    """Return {"targets", "counts", "mistakes", "excluded", "exclusions", "unresolved", "ignored"}.

    counts      rows and table values per group, plus excluded_by_person and fixes_pending (fixes in
                effect not yet re-checked)
    mistakes    {code, second_reader, person}: who caught each value that was wrong and got changed
    excluded    {code, person}: rows left out under a rule, by who left them out
    exclusions  one entry per excluded row: target, rule, reason, by
    unresolved  rows still "open" or "unverified" (the one "Needs attention" number)
    """
    targets = _base_targets(run)
    stacks: dict[str, list[dict]] = {t: [] for t in targets}
    ignored = []
    for i, d in enumerate(decisions):
        why = _why_not(d, targets, stacks)
        if why is not None:
            ignored.append({"index": i, "target": d.get("target") if isinstance(d, dict) else None,
                            "reason": why})
            continue
        if d["action"] == "undo":
            stacks[d["target"]].pop()
        elif d["action"] == "recheck":
            st = stacks[d["target"]]
            i = _waiting_fix(st)
            rc = d["recheck"]
            st[i] = {**st[i], "recheck": {"result": rc["result"], "reason": rc.get("reason"),
                                          "checks": rc.get("checks", []), "at": d.get("at")}}
        else:
            stacks[d["target"]].append(d)

    states = {t: _apply(base, stacks[t]) for t, base in targets.items()}

    counts = {"rows": {g: 0 for g in GROUPS}, "cells": {g: 0 for g in GROUPS}}
    for s in states.values():
        counts["rows" if s["kind"] == "row" else "cells"][s["group"]] += 1

    mistakes = {k: 0 for k in CAUGHT_BY}
    for m in run.get("mistakes", []):
        if m.get("caught_by") in mistakes:
            mistakes[m["caught_by"]] += 1
    fixes = [d for st in stacks.values() for d in st if d["action"] == "fix"]
    mistakes["person"] += sum(1 for d in fixes if (d.get("recheck") or {}).get("result") == "pass")
    counts["fixes_pending"] = sum(1 for d in fixes if d.get("recheck") is None)

    exclusions = _exclusions(run, states)
    excluded = {k: sum(1 for x in exclusions if x["by"] == k) for k in EXCLUDED_BY}
    counts["excluded_by_person"] = excluded["person"]
    unresolved = sum(1 for s in states.values() if s["kind"] == "row" and s["group"] in UNRESOLVED)

    return {"targets": states, "counts": counts, "mistakes": mistakes, "excluded": excluded,
            "exclusions": exclusions, "unresolved": unresolved, "ignored": ignored}
