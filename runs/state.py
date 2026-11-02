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
from checks import event_checks
from runs.prices import cost_per_kept_row_usd
from runs.ids import require_unique_target_ids
from runs.claim_assessment import checks_current as bound_checks_current, page_evidence, row_source, visible_passage_assessment
from runs.page_outcomes import read_view as page_outcomes_view
from runs.proposal import CONTEXT_FIELDS, type_presentation
from datetime import date


def run_reference_date(run: dict) -> date | None:
    """THE one reference date of a run: `as_of` in run.json, else the day it started. None when neither reads as a
    date. Everything that needs "the run date" reads it here; started_at itself is the real timestamp and is never
    changed."""
    meta = run.get("run") or {}
    for key in ("as_of", "started_at"):
        try:
            return date.fromisoformat(str(meta.get(key) or "")[:10])
        except ValueError:
            continue
    return None


UNRESOLVED_REASON = "This run has no usable reference date (as_of or started_at)."


def as_of_note(run: dict) -> str | None:
    """"as of 13 Oct 2026 · run on 31 Oct 2026" when as_of is set and is not the start day; otherwise None."""
    meta = run.get("run") or {}
    ref, started = run_reference_date(run), str(meta.get("started_at") or "")[:10]
    if ref is None:
        return UNRESOLVED_REASON
    try:
        began = date.fromisoformat(started)
    except ValueError:
        return None
    if not meta.get("as_of") or ref is None or ref == began:
        return None
    f = lambda d: f"{d.day} {d.strftime('%b %Y')}"
    return f"as of {f(ref)} · run on {f(began)}"


def _plain_field(field) -> str:
    """A field name in words for a reason: event_type -> 'event type'."""
    return str(field).replace("_", " ")


def _plain_value(field, value):
    return value.replace("_", " ") if field == "event_type" and isinstance(value, str) else value

GROUPS = ("kept", "corrected", "removed", "unverified", "open")
ACTIONS = ("keep", "fix", "remove", "undo", "recheck")
PERSON_ACTIONS = ("keep", "fix", "remove", "undo")
RECHECK_RESULTS = ("pass", "fail", "could_not_run", "unresolved")
COULD_NOT_RUN_REASON = "The re-check could not run: a check had nothing to compare."
NO_ERROR_REASON = "The value already passed its checks before the edit."
NO_BEFORE_REASON = "The re-check did not record the checks on the value before the edit."
BEFORE_UNDEMONSTRATED_REASON = "The old value could not be assessed; no mistake was demonstrated."
CAUGHT_BY = ("code", "second_reader", "person")
EXCLUDED_BY = ("code", "person")
UNRESOLVED = ("open", "unverified")

ROW_FIELDS = ("ticker", "company", "event_type", "title", "date_text", "date", "date_end", "quote", "program",
              "indication") + CONTEXT_FIELDS
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
    with two values becomes the main disagreement and the rest stay in "also".
    Proposal context is an exception: a missing qualification versus a supplied
    one, or [] versus a condition list, must remain a review hold. Neither
    missing context nor an empty list is semantic confirmation."""
    if not d:
        return None
    entries = [{k: v for k, v in x.items() if k != "also"} for x in [d, *(d.get("also") or [])]]
    real = [x for x in entries
            if (x.get("field") in CONTEXT_FIELDS and x.get("first_reader") != x.get("second_reader"))
            or (x.get("field") not in CONTEXT_FIELDS
                and not _empty(x.get("first_reader")) and not _empty(x.get("second_reader")))]
    if not real:
        return None
    return {**real[0], **({"also": real[1:]} if real[1:] else {})}


_REVIEW_FOUND_BY = ("coverage", "second_reader")


def derive_status(row: dict, checks: list | None = None, disagreement=None) -> tuple[str, str, bool]:
    """The one place a saved row's (status, group) is worked out from its CURRENT effective checks (pass `checks`
    when they differ from row["checks"]) and the readers' disagreement. Returns (status, group, derived).
    Repair tools only replace a check result; they never write status or group, and current_state calls this.

    - a code exclusion (group "removed") is left exactly as it is;
    - a row only the second reader or the coverage scan found needs review: undecided (failed if a check failed),
      open, whatever its checks say;
    - a real two-sided disagreement is held whatever status was saved (escalation or a repair tool never settles it;
      only a person's fix of the disputed field, a keep or a remove does, in _apply): failed if a check failed, else
      undecided; open;
    - otherwise the checks decide (event_checks.row_status): matched -> kept, failed -> open, anything else
      -> unverified. A row left undecided only for a one-sided "disagreement" is matched when the checks say matched
      and otherwise stays as the run left it. A row with no checks, or a status the checks do not own (unreadable),
      stays as saved.
    Human decisions (keep, fix, remove) are applied afterwards by _apply and override this."""
    saved = (row["status"], row["group"], False)
    if row["group"] == "removed":
        return saved
    checks = row.get("checks") or [] if checks is None else checks
    disagreement = _real_disagreement(row.get("disagreement")) if disagreement is None else disagreement
    if row.get("found_by") in _REVIEW_FOUND_BY or disagreement:
        status = event_checks.row_status(checks, second_reader_agrees=False)
        return ("failed" if status == event_checks.FAILED else "undecided", "open", True)
    if row["status"] in ("unreadable", "unchecked") or not checks:
        return saved
    status = event_checks.row_status(checks)
    if row["status"] == "undecided" and status != event_checks.MATCHED:
        return saved
    if status == row["status"]:
        return saved
    group = "kept" if status == event_checks.MATCHED else "open" if status == event_checks.FAILED else "unverified"
    return (status, group, True)


def _derived_reason(row: dict, status: str, group: str, checks: list) -> str | None:
    """Do not present a saved verdict summary as current after checks were refreshed."""
    if status == "matched" and row.get("check_history"):
        return "The current mechanical checks have no failures; event meaning needs your review."
    if (status, group) == (row["status"], row["group"]):
        return None
    if status == "failed":
        bad = next((c for c in checks if c.get("result") == "fail"), None)
        return (bad or {}).get("reason") or "A check failed."
    if status == "matched":
        return "The complete event-check set has no failures, with at least one check passed."
    return row.get("reason")


def _period_bounds(row: dict) -> tuple[str, str] | None:
    """Use the structured date facts emitted by date_match; never parse its reason text."""
    if row.get("date") or row.get("date_end"):
        return None
    for c in row.get("checks") or []:
        if c.get("name") == "date_match" and c.get("result") == "pass":
            timing = (c.get("compared") or {}).get("timing") or {}
            if timing.get("precision") == "period" and timing.get("start") and timing.get("end"):
                return timing["start"], timing["end"]
    return None


def _base_targets(run: dict) -> dict:
    targets = {}
    sources = {source.get("id"): source for source in run.get("sources", [])}
    for row in run.get("rows", []):
        candidate_values = {f: row.get(f) for f in ROW_FIELDS}
        values = dict(candidate_values)
        source = sources.get(row.get("source_id"), {})
        checks = row.get("checks") or []
        if bound_checks_current(checks, row, source):
            bounds = _period_bounds({**row, "checks": checks})
            if bounds:
                values["date"], values["date_end"] = bounds
        dis = _real_disagreement(row.get("disagreement"))
        status, group, _ = derive_status(row, disagreement=dis)
        targets[row["id"]] = {
            "kind": "row",
            "group": group,
            "status": status,
            "check_status": event_checks.row_status(row.get("checks") or []),
            "reason": _derived_reason(row, status, group, row.get("checks") or []) or row.get("reason"),
            "saved_checks": copy.deepcopy(row.get("checks") or []),
            "load_check_history": copy.deepcopy(row.get("check_history") or []),
            "values": values,
            "candidate_values": candidate_values,
            "candidate": copy.deepcopy(row),
            "disagreement": dis,
        }
    for table in run.get("tables", []):
        for trow in table.get("rows", []):
            for cell in trow.get("cells", []):
                targets[cell["id"]] = {
                    "kind": "cell",
                    "group": _CELL_GROUP.get(cell["status"], "unverified"),
                    "status": cell["status"],
                    "reason": None,
                    "saved_checks": copy.deepcopy(cell.get("checks") or []),
                    "values": {"value": cell.get("value")},
                    "disagreement": _real_disagreement(cell.get("disagreement")),
                }
    return targets


def checked_fields(kind: str) -> tuple[str, ...]:
    return ROW_CHECKED_FIELDS if kind == "row" else CELL_CHECKED_FIELDS


def _apply(base: dict, stack: list[dict]) -> dict:
    s = copy.deepcopy(base)
    if base["kind"] == "row":
        s["candidate_values"] = copy.deepcopy(base.get("candidate_values", base["values"]))
        # Rebuild the editable projection from raw candidate values before
        # replaying decisions. `base["values"]` may contain period bounds
        # derived from the original checks; those bounds are not explicit
        # candidate values and must not survive a later failed re-check.
        s["values"] = copy.deepcopy(s["candidate_values"])
    s["fixes"] = []
    s["before"] = {}
    s["recheck"] = None
    dis0 = base.get("disagreement") or {}
    disputed = [x.get("field") for x in [dis0, *dis0.get("also", [])] if x and x.get("field")]
    # The readers' disagreement stays open until a fix of EVERY disputed field (the main one and each "also")
    # has passed its re-check, or a person keeps or removes the row.
    remaining = list(dict.fromkeys(disputed)) if base.get("disagreement") is not None else []
    unsettled = base.get("disagreement") is not None
    fixed: set[str] = set()
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
            fixed.add(fx["field"])
            s["values"][fx["field"]] = fx["to"]
            if base["kind"] == "row":
                s["candidate_values"][fx["field"]] = fx["to"]
            rc = d.get("recheck")
            covered = fx["field"] in checked_fields(s["kind"])
            settles = unsettled and fx["field"] in remaining
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
                        # this re-check ran on the row as it now reads, so it also covers an earlier fix of any
                        # disputed field (checked or judged, e.g. program) whose own re-check failed only because
                        # another field was still wrong. This records the person's choice, not code validation.
                        done = {fx["field"], *fixed}
                        remaining = [f for f in remaining if f not in done]
                        unsettled = bool(remaining)
                    s["status"] = "undecided" if unsettled else "matched"
                    s["reason"] = rc.get("reason") or "The checks were run again on the fixed value and passed."
                    if not outcome["mistake"] and outcome["not_counted_reason"]:
                        s["reason"] = f"{s['reason']} {outcome['not_counted_reason']}"
                elif rc["result"] in ("could_not_run", "unresolved"):
                    if unsettled and not settles:
                        s["status"] = "undecided"
                    elif covered or settles:
                        s["status"] = "proposed"
                    else:
                        # An unchecked field edit is not a correction; preserve the bound
                        # check result while its group/placement remain governed separately.
                        s["status"] = base.get("check_status", s["status"])
                    s["reason"] = rc.get("reason") or COULD_NOT_RUN_REASON
                else:
                    s["group"] = "open"
                    s["status"] = "failed"
                    s["reason"] = rc.get("reason") or "The checks were run again on the fixed value and failed."
            if unsettled:
                s["reason"] = f"{s['reason']} Still disputed: {', '.join(remaining)}."
    last_fix = s["fixes"][-1] if s["fixes"] else None
    s["recheck"] = last_fix["recheck"] if last_fix else None
    s["needs_recheck"] = bool(last_fix) and last_fix["recheck"] is None
    # Saved checks describe the original value. Once a fix exists, only the
    # latest re-check's after-set describes the current value; pending or
    # unavailable checks stay empty rather than borrowing old failures.
    if last_fix:
        rc = last_fix["recheck"] or {}
        after = rc.get("after") or {}
        s["effective_checks"] = copy.deepcopy(after.get("checks") or rc.get("checks") or [])
        s["check_history"] = (copy.deepcopy(base.get("load_check_history") or [])
                               + copy.deepcopy(base.get("saved_checks") or []))
    else:
        s["effective_checks"] = copy.deepcopy(base.get("saved_checks") or [])
        s["check_history"] = copy.deepcopy(base.get("load_check_history") or [])
    # Keep the editable view in step with the latest accepted assessment at
    # every replay prefix. Candidate values remain raw for binding; period
    # bounds are a projection exposed to GET and used by the next edit's
    # from-value validation.
    if base["kind"] == "row":
        candidate = {**base.get("candidate", {}), **s["candidate_values"]}
        bounds = _period_bounds({**candidate, "checks": s.get("effective_checks") or []})
        if bounds:
            s["values"]["date"], s["values"]["date_end"] = bounds
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
            return "A re-check needs a result of pass, fail, could_not_run or unresolved."
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
        if fx["field"] in CONTEXT_FIELDS:
            value = fx["to"]
            if fx["field"] == "conditions":
                if value is not None and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                    return "Conditions must be a list of source excerpts or null."
            elif value is not None and not isinstance(value, str):
                return "A proposal description must be text or null."
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
    unresolved  event rows whose derived review_bucket is proposed (counts.review.proposed).
                Passing checks or a field fix do not accept a proposal. A current bound Keep or
                an exclusion takes it out; an expired Keep returns it to review.
    cost_per_kept_row_usd  run.spend_usd divided by event rows explicitly accepted for the calendar; null when the spend
                was not recorded or no row is kept
    """
    require_unique_target_ids(run)
    # Delayed import breaks the state/calendar cycle: calendar imports the
    # shared reference-date contract from this module.
    from runs.calendar import date_text_cleared, effective_date_display, explicit_dependency, timing_state

    # Historical check outcomes must not establish today's status. Validate the
    # saved candidate before any check can derive status or timing; load paths
    # verify the exact artifact bytes before this pure state projection runs.
    run_view = {**run, "rows": []}
    by_source = {source.get("id"): source for source in run.get("sources", [])}
    for original in run.get("rows", []):
        row = copy.deepcopy(original)
        source = by_source.get(row.get("source_id"), {})
        checks = row.get("checks") or []
        fresh = bound_checks_current(checks, row, source)
        if not fresh and row.get("group") != "removed":
            row["check_history"] = [*(row.get("check_history") or []), *checks]
            row["checks"] = []
            # Preserve an existing review hold and unreadable outcome even
            # though stale checks cannot establish a new status.
            if row.get("status") not in {"undecided", "unreadable"}:
                row["status"] = "proposed"
                row["group"] = "unverified"
                row["reason"] = "Saved checks are history; current checks are unavailable."
        run_view["rows"].append(row)
    targets = _base_targets(run_view)
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
            fix_index = _waiting_fix(st)
            rc = d["recheck"]
            base = targets[d["target"]]
            if base["kind"] == "row":
                pending = _apply(base, st)
                candidate = {**base["candidate"], **pending["candidate_values"]}
                source = row_source(run_view, base["candidate"])
                checks = (rc.get("after") or {}).get("checks") or rc.get("checks") or []
                from runs.recheck import recheck_result
                fix_field = st[fix_index]["fix"]["field"]
                no_evidence_outcome = (not checks and rc.get("result") in {"could_not_run", "unresolved"})
                expected_result = (rc.get("result") if no_evidence_outcome else recheck_result(
                    checks, base["kind"], fix_field, pending["fixes"][-1]["settles"]))
                if (expected_result != rc.get("result")
                        or (not no_evidence_outcome and not bound_checks_current(checks, candidate, source))):
                    reason = ("The saved re-check has no check results; it remains history."
                              if not checks else
                              "The saved re-check is unbound or stale; it remains history.")
                    ignored.append({"index": i, "target": d.get("target"), "reason": reason})
                    continue
            st[fix_index] = {**st[fix_index], "recheck": {"result": rc["result"], "reason": rc.get("reason"),
                                          "checks": rc.get("checks", []), "at": d.get("at"),
                                          "before": rc.get("before"), "after": rc.get("after")}}
        else:
            stacks[d["target"]].append(d)

    states = {t: _apply(base, stacks[t]) for t, base in targets.items()}

    # Text-layer and box checks do not read the words rendered in page ink.
    # Keep checks current only when their exact bindings still match; otherwise
    # expose them as history and never inherit a pass.
    for target, state in states.items():
        if state["kind"] != "row":
            continue
        base = targets[target]
        source = row_source(run, base["candidate"])
        state["page_evidence"] = page_evidence(source, base["candidate"].get("page_number"))
        # Assess the candidate as saved/fixed before adding display dates derived
        # from the check itself. Otherwise a valid period check invalidates itself.
        current = {**base["candidate"], **state["candidate_values"]}
        checks = state.get("effective_checks") or []
        checks_current = bound_checks_current(checks, current, source)
        state["checks_stale"] = not checks_current
        state["claim_assessments"] = {"visible_passage": visible_passage_assessment(current, source)}
        state["assessment_status"] = "not_checked" if checks_current else "unresolved"
        if not checks_current:
            previous = state.get("effective_checks") or []
            state["check_history"] = copy.deepcopy(state.get("check_history") or previous or base["saved_checks"])
            state["effective_checks"] = []
        else:
            bounds = _period_bounds({**current, "checks": checks})
            if bounds:
                state["values"]["date"], state["values"]["date_end"] = bounds
        display_current = {**current, "date": state["values"].get("date"),
                           "date_end": state["values"].get("date_end")}
        state["timing_state"] = timing_state(display_current)
        # One effective date display from the complete effective tuple, shared
        # by the API row, calendar entries and the report (never recomputed there).
        state["date_display"] = effective_date_display(state["values"], state["timing_state"])
        state["date_text_cleared"] = date_text_cleared(state["values"], state.get("fixes"))
        state["contingent"] = explicit_dependency(display_current)
        state.pop("candidate", None)
        state.pop("candidate_values", None)
        state.pop("check_status", None)
        # A mechanical assessment and a person's acceptance answer different
        # questions. Derive review state from the existing decision log, not
        # from a pass, model agreement, or a code-corrected value. A field edit
        # still needs an explicit Keep of the resulting proposal.
        from runs.review import review_basis, matches_review_basis, review_projection
        basis = review_basis(run, base["candidate"], state["values"])
        state["review_basis"] = basis
        active = stacks[target]
        latest = active[-1] if active else {}
        action = state.get("last_action")
        accepted = action == "keep" and matches_review_basis(latest.get("review_basis"), basis)
        state["review_history"] = [
            {**copy.deepcopy(d), "history_reason": (
                "This Keep has no recorded proposal/evidence basis; review is required."
                if d.get("review_basis") is None else
                "The proposal or evidence changed after this Keep; review is required.")}
            for d in active if d["action"] == "keep" and not matches_review_basis(d.get("review_basis"), basis)]
        state["semantic_status"] = ("human_accepted" if accepted else
                                    "human_removed" if action == "remove" else "unresolved")
        if action == "keep" and not accepted:
            state["reason"] = state["review_history"][-1]["history_reason"]
        if state["semantic_status"] == "unresolved" and state["group"] != "removed":
            state["reason"] = ((state.get("reason") or "Checks recorded.")
                               + " The event remains a proposal; a person must decide whether to keep it.")
        state.update(review_projection(state))
        state["type_presentation"] = type_presentation(base["candidate"], state["values"], state["fixes"])

    counts = {"rows": {g: 0 for g in GROUPS}, "cells": {g: 0 for g in GROUPS}}
    for s in states.values():
        counts["rows" if s["kind"] == "row" else "cells"][s["group"]] += 1

    counts["review"] = {key: sum(1 for st in states.values()
                                      if st["kind"] == "row" and st["review_bucket"] == key)
                        for key in ("accepted", "proposed", "excluded")}

    mistakes = {k: 0 for k in CAUGHT_BY}
    for m in run.get("mistakes", []):
        if m.get("caught_by") in mistakes:
            mistakes[m["caught_by"]] += 1
    fixes = [(f, s) for s in states.values() for f in s["fixes"]]
    mistakes["person"] += sum(1 for f, _ in fixes if f["mistake"] and not f["second_reader_value"])
    mistakes["second_reader"] += sum(1 for f, _ in fixes if f["mistake"] and f["second_reader_value"])
    counts["edits_without_error"] = sum(1 for f, _ in fixes if f["edit_without_error"])
    counts["fixes_pending"] = sum(1 for f, s in fixes if s["group"] != "removed"
                                  and (f["recheck"] is None or f["recheck"]["result"] in ("could_not_run", "unresolved")))

    mistake_list = _mistake_list(run, states)

    exclusions = _exclusions(run, states)
    excluded = {k: sum(1 for x in exclusions if x["by"] == k) for k in EXCLUDED_BY}
    counts["excluded_by_person"] = excluded["person"]
    unresolved = counts["review"]["proposed"]

    spend = (run.get("run") or {}).get("spend_usd")
    kept_rows = counts["review"]["accepted"]

    return {"view_version": 1, "targets": states, "page_outcomes": page_outcomes_view((run.get("run") or {}).get("page_outcomes")),
            "counts": counts, "mistakes": mistakes, "mistake_list": mistake_list,
            "excluded": excluded, "exclusions": exclusions, "unresolved": unresolved, "ignored": ignored,
            "kept_rows": kept_rows, "cost_per_kept_row_usd": cost_per_kept_row_usd(spend, kept_rows)}


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
