"""Shared assertion for historical semantic-check cases under PIVOT-001.

Synthetic page words are labelled by make_run. No model, saved run, or human
acceptance is fabricated. These assertions test the publication boundary.
"""
from runs.calendar import calendar_entries
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run


def assert_proposal_held(quote, hint, *, date_text=None, start=None, end=None):
    _, run = make_run(('adversarial-meaning', quote, hint, date_text, start, []),
                      event_updates={'date_end': end})
    state = current_state(run, [])
    target = state['targets']['e1']
    assert target['needs_attention'] and not target['accepted']
    assert state['counts']['review'] == {'accepted': 0, 'proposed': 1, 'excluded': 0}
    assert target['values']['quote'] == quote
    assert target['values']['type_hint'] == hint
    assert calendar_entries(run, state) == []
    return run, state


def fresh_keep(run, decisions, target, *, note="Accepted for this disposable test"):
    """New explicit human decision against the current effective test proposal."""
    return {"action": "keep", "target": target, "by": "person",
            "at": "2026-11-01T22:30:00Z", "note": note,
            "review_basis": current_state(run, decisions)["targets"][target]["review_basis"]}
