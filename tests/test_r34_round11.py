"""Job 34: Red Team round 11 correctness fixes (F1, F2a, F4, F9). No model call, no network, no key."""

from __future__ import annotations

from checks.common import COULD_NOT_RUN, NOT_NEEDED, PASS
from checks.event_checks import date_match, relative_period
from report.data import build_view
from runs.calendar import calendar_entries
from runs.state import current_state

VKTX_QUOTE = ("The company expects to complete the SAD study later this quarter and initiate a 4 week "
              "multiple ascending dose (MAD) study of VK3019 in 1Q27.")


def _check(name, result, found=None):
    return {"name": name, "result": result, "compared": {"looked_for": None, "found": found}}


def _row(rid="r1", **kw):
    row = {"id": rid, "ticker": "PTGX", "company": "Protagonist", "event_type": "milestone", "title": "Phase 2",
           "date_text": "early Q1 2027", "date": None, "date_end": None, "quote": "q", "program": "PN-881",
           "indication": None, "source_id": "s1", "page_number": 1, "status": "matched", "group": "kept",
           "reason": "Every check passed.", "checks": [], "disagreement": None}
    row.update(kw)
    return row


def _run(*rows):
    return {"run": {"id": "run-t", "number": 1, "choices": {}, "started_at": "2026-10-30T00:00:00+00:00"},
            "rows": list(rows), "tables": [], "sources": [], "mistakes": [],
            "exclusions": []}


ONE_SIDED = {"field": "indication", "first_reader": None, "second_reader": "Psoriatic Arthritis"}


def _undecided(checks):
    return _row(status="undecided", group="open", disagreement=ONE_SIDED, checks=checks)


# ---------------------------------------------------------------- F1

def test_f1_one_sided_disagreement_with_a_check_that_could_not_run_stays_open():
    run = _run(_undecided([_check("quote_match", "pass"), _check("date_match", "could_not_run")]))
    t = current_state(run, [])["targets"]["r1"]
    assert (t["status"], t["group"]) == ("undecided", "open")


def test_f1_one_sided_disagreement_with_every_check_passed_or_not_needed_is_matched():
    run = _run(_undecided([_check("quote_match", "pass"), _check("type_match", "not_needed")]))
    t = current_state(run, [])["targets"]["r1"]
    assert (t["status"], t["group"]) == ("matched", "kept")


# ---------------------------------------------------------------- F2a

def test_f2a_period_found_by_the_date_check_is_written_to_the_values():
    row = _row(checks=[_check("date_match", "pass", "2027-01-01 to 2027-01-31")])
    run = _run(row)
    state = current_state(run, [])
    v = state["targets"]["r1"]["values"]
    assert (v["date"], v["date_end"]) == ("2027-01-01", "2027-01-31")
    entry = calendar_entries(run, state)[0]
    assert (entry["date"], entry["date_end"]) == ("2027-01-01", "2027-01-31")


def test_f2a_a_stored_date_or_a_failed_check_is_left_alone():
    stored = _row(date="2027-01-05", date_end="2027-01-06",
                  checks=[_check("date_match", "pass", "2027-01-01 to 2027-01-31")])
    failed = _row("r2", checks=[_check("date_match", "fail", "2027-01-01 to 2027-01-31")])
    t = current_state(_run(stored, failed), [])["targets"]
    assert (t["r1"]["values"]["date"], t["r1"]["values"]["date_end"]) == ("2027-01-05", "2027-01-06")
    assert (t["r2"]["values"]["date"], t["r2"]["values"]["date_end"]) == (None, None)


# ---------------------------------------------------------------- F4

def test_f4_unparsed_date_text_beside_another_date_could_not_run():
    res = date_match("sometime soon", None, "The company expects to finish sometime soon and start in 1Q27.")
    assert res.result == COULD_NOT_RUN


def test_f4_unparsed_date_text_with_no_other_date_is_not_needed():
    assert date_match("sometime soon", None, "The company expects to finish sometime soon.").result == NOT_NEEDED


def test_f4_later_this_quarter_is_the_filing_quarter():
    from datetime import date
    assert relative_period("later this quarter", "2026-11-05") == (date(2026, 10, 1), date(2026, 12, 31))
    assert relative_period("this quarter", date(2026, 8, 5)) == (date(2026, 7, 1), date(2026, 9, 30))
    res = date_match("later this quarter", None, VKTX_QUOTE, filed="2026-11-05")
    assert res.result == PASS
    assert res.compared["found"] == "2026-10-01 to 2026-12-31"


def test_f4_later_this_quarter_without_a_filing_date_could_not_run():
    assert date_match("later this quarter", None, VKTX_QUOTE).result == COULD_NOT_RUN


# ---------------------------------------------------------------- F9

def _fixed_program_run():
    run = _run(_row(date="2027-01-01", date_end="2027-01-31"))
    decision = {"action": "fix", "target": "r1", "by": "person", "at": "2026-10-30T00:00:00Z",
                "fix": {"field": "program", "from": "PN-881", "to": "PN-8047"}}
    recheck = {"action": "recheck", "target": "r1", "by": "code", "at": "2026-10-30T00:00:01Z",
               "recheck": {"result": "pass", "reason": "Passed."}}
    return run, [decision, recheck]


def test_f9_a_fixed_program_appears_on_the_calendar():
    run, decisions = _fixed_program_run()
    state = current_state(run, decisions)
    assert state["targets"]["r1"]["values"]["program"] == "PN-8047"
    assert calendar_entries(run, state)[0]["program"] == "PN-8047"


def test_f9_a_fixed_program_appears_in_the_export():
    run, decisions = _fixed_program_run()
    view = build_view(run, decisions, generated_at="2026-10-30T00:00:00Z")
    assert [r["program"] for r in view["calendar"]] == ["PN-8047"]
