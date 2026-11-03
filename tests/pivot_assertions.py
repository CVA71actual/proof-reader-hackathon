"""Shared assertion for historical semantic-check cases under PIVOT-001.

Synthetic page words are labelled by make_run. No model, saved run, or human
acceptance is fabricated. These assertions test the publication boundary.
"""
from runs.calendar import calendar_entries
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run


def assert_proposal_held(quote, hint, *, date_text=None, start=None, end=None):
    """No diagnostic or check outcome grants a person's acceptance. CALENDAR-RULE-001 (CVA 2026-11-02): a clean
    proposal is no longer held for a mandatory Keep — it may be placed automatically (calendar, context, later or
    undated) — but it is never accepted, never labelled or counted as kept, and a row with an issue is held for review."""
    _, run = make_run(('adversarial-meaning', quote, hint, date_text, start, []),
                      event_updates={'date_end': end})
    state = current_state(run, [])
    target = state['targets']['e1']
    assert not target['accepted'] and target['semantic_status'] == 'unresolved'
    assert target['decision_label'] is None and not target['review_label'].startswith(('Accepted', 'Kept'))
    assert state['counts']['review'] == {'accepted': 0, 'proposed': 1, 'excluded': 0}
    assert state['counts']['inclusion']['kept'] == 0
    assert target['values']['quote'] == quote
    assert target['values']['type_hint'] == hint
    entries = calendar_entries(run, state)
    if target['issues']:
        assert target['needs_attention'] and entries == []
    else:
        assert not target['needs_attention'] and target['inclusion'] in ('included', 'context', 'later', 'undated')
        assert [e['inclusion'] for e in entries] == [target['inclusion']]
    return run, state


def assert_never_accepted(run, state, target_id="e1"):
    """CALENDAR-RULE-001: an undecided proposal is never accepted or labelled kept. With an issue it waits for
    review, off the calendar; clean, it is placed automatically and its calendar entry says so."""
    target = state["targets"][target_id]
    assert not target["accepted"] and target["inclusion"] != "kept"
    assert target["decision_label"] != "Kept by you" and not target["review_label"].startswith(("Accepted", "Kept"))
    entries = [e for e in calendar_entries(run, state) if e["id"] == target_id]
    if target["issues"]:
        assert target["needs_attention"] and not target["on_calendar"] and entries == []
    else:
        assert not target["needs_attention"] and target["on_calendar"]
        assert [e["inclusion"] for e in entries] == [target["inclusion"]]
    return target


def assert_no_fabricated_keep(run, state):
    """CALENDAR-RULE-001: every calendar entry of a row a person did not keep says so — never 'Kept by you'."""
    for e in calendar_entries(run, state):
        t = state["targets"][e["id"]]
        if not t["accepted"]:
            assert e["inclusion"] != "kept" and e["decision_label"] != "Kept by you"
            assert not t["needs_attention"] and not t["issues"]


def fresh_keep(run, decisions, target, *, note="Accepted for this disposable test"):
    """New explicit human decision against the current effective test proposal."""
    return {"action": "keep", "target": target, "by": "person",
            "at": "2026-11-01T22:30:00Z", "note": note,
            "review_basis": current_state(run, decisions)["targets"][target]["review_basis"]}
