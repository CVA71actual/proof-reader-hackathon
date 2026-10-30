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
               result "could_not_run": not a failure. The row stays group "corrected" with status
               "proposed" (a field no check compares: the status stays as it was); the fix stays in
               counts.fixes_pending (not a mistake) and the row is counted in unresolved, so it
               shows under Needs attention.
               An undecided row (the readers disagreed) stays "undecided" until a fix of its
               disagreement.field passes its re-check; a fix of another field does not settle it.
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
                the second reader or the person during the run) plus, for the person, every fix still in
                effect that is a demonstrated mistake. The re-check runs the same checks twice: on the value
                before the fix ("before") and on the new value ("after"). A fix of a field a check compares
                (ROW_CHECKED_FIELDS, CELL_CHECKED_FIELDS), or one that settled the disagreement, counts only when
                after is "pass" AND before is "fail" (the old value was wrong). Before "could_not_run" with
                after "pass" does not count, and is marked before_undemonstrated: the old value could not be
                assessed. Before "pass" with after "pass" is an edit without an error: the
                fix is recorded and the row stays corrected/matched, but it is not counted; it is counted in
                counts.edits_without_error with the reason "The value already passed its checks before the edit."
                A re-check that recorded no before-run is not counted either.
                A fix not yet re-checked, or whose re-check could not run, is counted in counts.fixes_pending
                instead; a fix whose re-check failed is not counted at all.
                A fix always changes the value (a fix with from == to is ignored).
    exclusions  a row left out under a rule, not because it was wrong: run.json's exclusions[]
                whose row is still removed and untouched by the person, plus every row whose latest
                decision in effect is the person's remove (rule "person_removed").
Undo takes back a fix or a remove, so it lowers whichever counter that decision raised.
"""

from __future__ import annotations

import copy
import re

from accuracy.types import primary_type, type_for_field
from runs.prices import cost_per_kept_row_usd


def _plain_field(field) -> str:
    """A field name in words for a reason: event_type -> 'event type'."""
    return str(field).replace("_", " ")


def _plain_value(field, value):
    return value.replace("_", " ") if field == "event_type" and isinstance(value, str) else value

GROUPS = ("kept", "corrected", "removed", "unverified", "open")
ACTIONS = ("keep", "fix", "remove", "undo", "recheck")
PERSON_ACTIONS = ("keep", "fix", "remove", "undo")
RECHECK_RESULTS = ("pass", "fail", "could_not_run")
COULD_NOT_RUN_REASON = "The re-check could not run: a check had nothing to compare."
NO_ERROR_REASON = "The value already passed its checks before the edit."
NO_BEFORE_REASON = "The re-check did not record the checks on the value before the edit."
BEFORE_UNDEMONSTRATED_REASON = "The old value could not be assessed; no mistake was demonstrated."
CAUGHT_BY = ("code", "second_reader", "person")
EXCLUDED_BY = ("code", "person")
UNRESOLVED = ("open", "unverified")

ROW_FIELDS = ("ticker", "company", "event_type", "title", "date_text", "date", "date_end", "quote", "program",
              "indication")
CELL_FIELDS = ("value",)
# The fields the checks compare. A fix to any other field cannot be shown right or wrong by a re-check.
ROW_CHECKED_FIELDS = ("quote", "date_text", "date", "date_end", "event_type")
CELL_CHECKED_FIELDS = ("value",)

_CELL_GROUP = {"matched": "kept", "failed": "open"}


def _empty(v) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and not (v.strip() if isinstance(v, str) else v))


def _real_disagreement(d) -> dict | None:
    """A disagreement exists only when both readers gave a value for a field; a reader that read nothing (None or
    empty) is not a disagreement. Each field (the main one and every "also") is judged on its own: the first one
    with two values becomes the main disagreement and the rest stay in "also"."""
    if not d:
        return None
    entries = [{k: v for k, v in x.items() if k != "also"} for x in [d, *(d.get("also") or [])]]
    real = [x for x in entries if not _empty(x.get("first_reader")) and not _empty(x.get("second_reader"))]
    if not real:
        return None
    return {**real[0], **({"also": real[1:]} if real[1:] else {})}


def _drop_empty_undecided(t: dict, raw, checks=None) -> None:
    """A target held as undecided only for a one-sided "disagreement" (one reader read nothing) has nothing to
    decide: it is matched, provided every check passed or was not needed. A check that could not run keeps the
    target as the run left it (open, not matched). An undecided target that never had a disagreement is left
    as it is."""
    if any(c.get("result") == "could_not_run" for c in checks or []):
        return
    if raw and t["status"] == "undecided" and t["disagreement"] is None:
        t["status"] = "matched"
        if t["group"] == "open":
            t["group"] = "kept"


_PERIOD_FOUND = re.compile(r"^(\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})$")


def _period_bounds(row: dict) -> tuple[str, str] | None:
    """The first and last day the passed date check found for the row's period ("2027-01-01 to 2027-01-31"),
    or None when the row already stores a date or the check named no period."""
    if row.get("date") or row.get("date_end"):
        return None
    for c in row.get("checks") or []:
        if c.get("name") == "date_match" and c.get("result") == "pass":
            m = _PERIOD_FOUND.match(str((c.get("compared") or {}).get("found") or ""))
            if m:
                return m[1], m[2]
    return None


def _base_targets(run: dict) -> dict:
    targets = {}
    for row in run.get("rows", []):
        values = {f: row.get(f) for f in ROW_FIELDS}
        bounds = _period_bounds(row)
        if bounds:
            values["date"], values["date_end"] = bounds
        targets[row["id"]] = {
            "kind": "row",
            "group": row["group"],
            "status": row["status"],
            "reason": row.get("reason"),
            "values": values,
            "disagreement": _real_disagreement(row.get("disagreement")),
        }
        _drop_empty_undecided(targets[row["id"]], row.get("disagreement"), row.get("checks"))
    for table in run.get("tables", []):
        for trow in table.get("rows", []):
            for cell in trow.get("cells", []):
                targets[cell["id"]] = {
                    "kind": "cell",
                    "group": _CELL_GROUP.get(cell["status"], "unverified"),
                    "status": cell["status"],
                    "reason": None,
                    "values": {"value": cell.get("value")},
                    "disagreement": _real_disagreement(cell.get("disagreement")),
                }
                _drop_empty_undecided(targets[cell["id"]], cell.get("disagreement"), cell.get("checks"))
    return targets


def checked_fields(kind: str) -> tuple[str, ...]:
    return ROW_CHECKED_FIELDS if kind == "row" else CELL_CHECKED_FIELDS


def _apply(base: dict, stack: list[dict]) -> dict:
    s = copy.deepcopy(base)
    s["fixes"] = []
    s["before"] = {}
    s["recheck"] = None
    disputed = (base.get("disagreement") or {}).get("field")
    # The readers' disagreement stays open until a fix of the disputed field passes its re-check.
    unsettled = base["status"] == "undecided"
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
            covered = fx["field"] in checked_fields(s["kind"])
            settles = unsettled and disputed is not None and fx["field"] == disputed
            outcome = _fix_outcome(rc, covered or settles)
            # a fix that puts in what the second reader read, on the field the readers disagreed on, is the
            # second reader's catch, not the person's
            dis = base.get("disagreement") or {}
            takes_second = any(fx["field"] == d.get("field") and fx["to"] == d.get("second_reader")
                               for d in [dis, *dis.get("also", [])] if d)
            s["fixes"].append({"field": fx["field"], "from": fx["from"], "to": fx["to"], "at": d.get("at"),
                               "recheck": rc, "settles": settles, "checked": covered or settles,
                               "second_reader_value": takes_second, **outcome})
            s["group"] = "corrected"
            s["reason"] = note or f"Fixed by you: {_plain_field(fx['field'])} changed from {_plain_value(fx['field'], fx['from'])} to {_plain_value(fx['field'], fx['to'])}."
            if rc is not None:
                s["recheck"] = rc
                if rc["result"] == "pass":
                    if settles:
                        unsettled = False
                    s["status"] = "undecided" if unsettled else "matched"
                    s["reason"] = rc.get("reason") or "The checks were run again on the fixed value and passed."
                    if not outcome["mistake"] and outcome["not_counted_reason"]:
                        s["reason"] = f"{s['reason']} {outcome['not_counted_reason']}"
                elif rc["result"] == "could_not_run":
                    if unsettled and not settles:
                        s["status"] = "undecided"
                    elif covered or settles:
                        s["status"] = "proposed"
                    # a field no check compares: the status stays what the checks last said
                    s["reason"] = rc.get("reason") or COULD_NOT_RUN_REASON
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


def _fix_outcome(rc: dict | None, checked: bool) -> dict:
    """Whether a fix is a demonstrated mistake: its re-check of the new value passed (after "pass") AND the same
    checks failed on the old value (before "fail"). Before "could_not_run" with after "pass" does not count
    and is marked before_undemonstrated because the old value could not be assessed. Before
    "pass" with after "pass" is an edit without an error: recorded, not counted."""
    out = {"mistake": False, "edit_without_error": False, "before_undemonstrated": False,
           "not_counted_reason": None}
    if rc is None or rc.get("result") != "pass" or not checked:
        return out
    before = (rc.get("before") or {}).get("result")
    if before == "fail":
        out["mistake"] = True
    elif before == "could_not_run":
        out["before_undemonstrated"] = True
        out["not_counted_reason"] = BEFORE_UNDEMONSTRATED_REASON
    elif before == "pass":
        out["edit_without_error"] = True
        out["not_counted_reason"] = NO_ERROR_REASON
    else:
        out["not_counted_reason"] = NO_BEFORE_REASON
    return out


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
            return "A re-check needs a result of pass, fail or could_not_run."
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


def _needs_attention(s: dict) -> bool:
    """Open or unverified; or corrected by a fix whose re-check could not run, or that left the readers'
    disagreement open. A later keep or remove is the person's answer, so it no longer needs attention."""
    if s["group"] in UNRESOLVED:
        return True
    if s["group"] != "corrected":
        return False
    return (s["recheck"] or {}).get("result") == "could_not_run" or s["status"] == "undecided"


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
    """Return {"targets", "counts", "mistakes", "excluded", "exclusions", "unresolved", "ignored",
    "cost_per_kept_row_usd"}.

    counts      rows and table values per group, plus excluded_by_person, fixes_pending (fixes in
                effect not yet re-checked, or whose re-check could not run, on rows not now removed) and
                edits_without_error (fixes in effect whose value passed its checks both before and after)
    mistakes    {code, second_reader, person}: who caught each value that was wrong and got changed
    mistake_list  one entry per counted mistake: target, type (accuracy.types), caught_by, model, what_changed,
                recorded_in ("run" or "decisions")
    excluded    {code, person}: rows left out under a rule, by who left them out
    exclusions  one entry per excluded row: target, rule, reason, by
    unresolved  rows now "open" or "unverified", plus rows now "corrected" whose fix's re-check could not
                run or that are still "undecided" (the one "Needs attention" number); a later keep or
                remove takes a row out
    cost_per_kept_row_usd  run.spend_usd divided by the rows now in "kept" or "corrected"; null when the spend
                was not recorded or no row is kept
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
                                          "checks": rc.get("checks", []), "at": d.get("at"),
                                          "before": rc.get("before"), "after": rc.get("after")}}
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
    fixes = [(f, s) for s in states.values() for f in s["fixes"]]
    mistakes["person"] += sum(1 for f, _ in fixes if f["mistake"] and not f["second_reader_value"])
    mistakes["second_reader"] += sum(1 for f, _ in fixes if f["mistake"] and f["second_reader_value"])
    counts["edits_without_error"] = sum(1 for f, _ in fixes if f["edit_without_error"])
    counts["fixes_pending"] = sum(1 for f, s in fixes if s["group"] != "removed"
                                  and (f["recheck"] is None or f["recheck"]["result"] == "could_not_run"))

    mistake_list = _mistake_list(run, states)

    exclusions = _exclusions(run, states)
    excluded = {k: sum(1 for x in exclusions if x["by"] == k) for k in EXCLUDED_BY}
    counts["excluded_by_person"] = excluded["person"]
    unresolved = sum(1 for s in states.values() if s["kind"] == "row" and _needs_attention(s))

    spend = (run.get("run") or {}).get("spend_usd")
    kept_rows = counts["rows"]["kept"] + counts["rows"]["corrected"]

    return {"targets": states, "counts": counts, "mistakes": mistakes, "mistake_list": mistake_list,
            "excluded": excluded, "exclusions": exclusions, "unresolved": unresolved, "ignored": ignored,
            "cost_per_kept_row_usd": cost_per_kept_row_usd(spend, kept_rows)}


def _target_models(run: dict) -> dict:
    """target id -> the model that proposed it (a row's proposed_by, a table value's table model)."""
    out = {r["id"]: r.get("proposed_by") for r in run.get("rows", [])}
    for table in run.get("tables", []):
        name = (table.get("model") or {}).get("name")
        for trow in table.get("rows", []):
            for cell in trow.get("cells", []):
                out[cell["id"]] = name
    return out


def _mistake_list(run: dict, states: dict) -> list[dict]:
    """One entry per counted mistake, with its type code: run.json's mistakes[] (their own `type`) and every
    fix in effect that is a demonstrated mistake (caught by the person). A fix's type is named by the check that
    failed on the old value, else by the field it changed."""
    models = _target_models(run)
    out = []
    for m in run.get("mistakes", []):
        if m.get("caught_by") not in CAUGHT_BY:
            continue
        out.append({"id": m.get("id"), "target": m.get("target"), "type": m.get("type"),
                    "caught_by": m["caught_by"], "model": models.get(m.get("target")),
                    "what_changed": m.get("what_changed"), "recorded_in": "run"})
    for t, s in states.items():
        for f in s["fixes"]:
            if not f["mistake"]:
                continue
            before = ((f["recheck"] or {}).get("before") or {}).get("checks") or []
            out.append({"id": None, "target": t, "type": primary_type(before) or type_for_field(f["field"]),
                        "caught_by": "second_reader" if f["second_reader_value"] else "person",
                        "model": models.get(t),
                        "what_changed": f"{_plain_field(f['field'])} changed from {_plain_value(f['field'], f['from'])} to {_plain_value(f['field'], f['to'])}.",
                        "recorded_in": "decisions"})
    return out
