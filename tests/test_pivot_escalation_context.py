"""Escalation must not turn a changed interpretation into a code-caught error."""
import copy
import json

import pytest

from runs import escalate
from runs.calendar import calendar_entries
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run, CASES, AT


@pytest.mark.parametrize("change", [
    {"conditions": [], "type_hint": "FDA approval"},
    {"conditions": []},
    {"type_hint": "FDA approval"},
    {"relevance": "This proves approval."},
    {"subject_company": "Other company"},
    {"relation": "Unestablished partnership"},
])
def test_reinterpretation_cannot_be_a_code_caught_correction(tmp_path, change):
    proposal, run = make_run(CASES[1], tmp_path / "source",
                            event_updates={"date": "2027-02-01"}, reading_fake=False)
    row = run["rows"][0]
    before = copy.deepcopy(row)
    answer = {**proposal, "date": "2027-01-01", **change}
    words = json.loads((tmp_path / "source/synthetic.words.json").read_text())
    outcome = escalate.apply(row, answer, {"fake": False, "prompt_text": "Synthetic"},
                             run["sources"][0], words, AT, escalate.trigger(row), run,
                             (None, None, 1), "Synthetic")
    assert outcome == "kept"
    assert run["mistakes"] == []
    for field in escalate.FIELDS + ("status", "group", "checks"):
        assert row[field] == before[field], field
    assert not calendar_entries(run, current_state(run, []))
    for field in change:
        assert field in row["trail"][-1]["text"]
        assert repr(change[field]) in row["trail"][-1]["text"]
    assert "a person must review" in row["trail"][-1]["text"]


def test_added_interpretation_is_not_mechanical_confirmation(tmp_path):
    proposal, run = make_run(CASES[1], tmp_path / "source", event_updates={
        "date": "2027-02-01", "relation": None}, reading_fake=False)
    row = run["rows"][0]
    words = json.loads((tmp_path / "source/synthetic.words.json").read_text())
    answer = {**proposal, "date": "2027-01-01", "relation": "New proposed partnership"}
    assert escalate.apply(row, answer, {"fake": False, "prompt_text": "Synthetic"},
                          run["sources"][0], words, AT, escalate.trigger(row), run,
                          (None, None, 1), "Synthetic") == "kept"
    assert row["relation"] is None
    assert not run["mistakes"]


def test_context_comparison_ignores_only_case_and_whitespace(tmp_path):
    proposal, run = make_run(CASES[1], tmp_path / 'source',
                            event_updates={'date': '2027-02-01'}, reading_fake=False)
    row = run['rows'][0]
    words = json.loads((tmp_path / 'source/synthetic.words.json').read_text())
    answer = {**proposal, 'date': '2027-01-01',
              'type_hint': '  CONDITIONAL   clinical READOUT ',
              'conditions': [' IF   VALIDATION succeeds '],
              'relevance': proposal['relevance'].upper()}
    outcome = escalate.apply(row, answer, {'fake': False, 'prompt_text': 'Synthetic'},
                             run['sources'][0], words, AT, escalate.trigger(row), run,
                             (None, None, 1), 'Synthetic')
    assert outcome == 'replaced'
    assert row['escalation_context_changes'] == []
    assert not any('type hint from' in m['what_changed'] or 'conditions from' in m['what_changed']
                   or 'relevance from' in m['what_changed'] for m in run['mistakes'])


def test_residual_context_difference_is_structured(tmp_path):
    proposal, run = make_run(CASES[1], tmp_path / 'source',
                            event_updates={'date': '2027-02-01'}, reading_fake=False)
    row = run['rows'][0]
    words = json.loads((tmp_path / 'source/synthetic.words.json').read_text())
    answer = {**proposal, 'date': '2027-01-01', 'conditions': []}
    escalate.apply(row, answer, {'fake': False, 'prompt_text': 'Synthetic'},
                   run['sources'][0], words, AT, escalate.trigger(row), run, (None, None, 1), 'Synthetic')
    assert row['escalation_context_changes'] == [
        {'field': 'conditions', 'before': ['if validation succeeds'], 'after': []}]
