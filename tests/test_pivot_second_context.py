"""Two matching quotes must not conceal different reader interpretations."""
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from runs import compare
from runs.calendar import calendar_entries
from runs.proposal import CONTEXT_FIELDS
from runs.state import current_state
from server.app import create_app
from tests.test_pivot_proposal_flow import make_run, CASES, AT, decision, kept_decision


def compared(tmp_path, change):
    proposal, run = make_run(CASES[1], tmp_path / 'synthetic', reading_fake=False)
    answer = copy.deepcopy(proposal)
    answer.update(change)
    words = json.loads((tmp_path / 'synthetic/synthetic.words.json').read_text())
    rows, added = compare.apply_events(run['rows'], [answer], {'prompt_text': 'Synthetic second read'},
                                      run['sources'][0], words, AT, 2)
    assert not added
    assert rows is run['rows']
    return proposal, answer, run


@pytest.mark.parametrize('change', [
    {'conditions': []}, {'conditions': None}, {'conditions': ['if funding succeeds']},
    {'type_hint': 'FDA approval'}, {'relation': 'Unestablished partnership'},
    {'subject_company': 'Other company'}, {'relevance': 'Unconditional approval'},
])
def test_context_difference_survives_save_reload_keep_undo(tmp_path, change):
    first, answer, run = compared(tmp_path, change)
    row = run['rows'][0]
    assert row['second_reader_proposal']['proposal'] == answer
    assert {f: row[f] for f in CONTEXT_FIELDS} == {f: first[f] for f in CONTEXT_FIELDS}
    raw = json.dumps(run)
    run = json.loads(raw)
    schema = json.loads((Path(__file__).parents[1] / 'runs/schema/run.schema.json').read_text())
    validator = Draft202012Validator({'$ref': '#/$defs/row', '$defs': schema['$defs']})
    assert not list(validator.iter_errors(row))
    state = current_state(run, [])
    assert state['targets']['e1']['needs_attention']
    assert state['targets']['e1']['disagreement']['field'] in change
    assert not calendar_entries(run, state)
    assert not any(state['mistakes'].values())
    keep = kept_decision(run)
    kept = current_state(run, [keep])
    assert kept['targets']['e1']['decided_by_person']
    assert kept['targets']['e1']['status'] != 'matched'
    assert calendar_entries(run, kept)
    undone = current_state(run, [keep, decision('undo')])
    assert undone['targets']['e1']['needs_attention']
    assert not calendar_entries(run, undone)
    # Raw alternative must not alias the caller's later mutation.
    answer['conditions'] = ['changed after comparison']
    assert json.dumps(run) == raw


def test_api_recheck_of_unrelated_field_does_not_clear_lost_condition(tmp_path):
    first, answer, run = compared(tmp_path, {'conditions': []})
    path = tmp_path / 'synthetic/run.json'
    path.write_text(json.dumps(run))
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root = '/api/runs/synthetic'
        got = client.get(root)
        assert got.status_code == 200, got.text
        assert got.json()['state']['targets']['e1']['needs_attention']
        response = client.post(root + '/decisions', json={
            'target': 'e1', 'action': 'fix',
            'fix': {'field': 'title', 'from': first['title'], 'to': 'Title edited by person'}})
        assert response.status_code == 200, response.text
        assert response.json()['state']['needs_attention']
        assert not response.json()['state']['on_calendar']
        keep = client.post(root + '/decisions', json={'target': 'e1', 'action': 'keep'})
        assert keep.status_code == 200, keep.text
        assert keep.json()['state']['on_calendar']
        assert keep.json()['state']['status'] != 'matched'
        undo = client.post(root + '/decisions', json={'target': 'e1', 'action': 'undo'})
        assert undo.status_code == 200, undo.text
        assert undo.json()['state']['needs_attention']
        reloaded = client.get(root).json()
        assert reloaded['rows'][0]['second_reader_proposal']['proposal'] == answer
        assert reloaded['state']['targets']['e1']['needs_attention']
        assert not any(reloaded['state']['mistakes'].values())


def test_equal_context_is_not_a_difference_or_semantic_clearance(tmp_path):
    _, answer, run = compared(tmp_path, {})
    row = run['rows'][0]
    assert not row.get('disagreement')
    assert row['second_reader_proposal']['proposal'] == answer
    assert 'Agreement is not a check' in row['trail'][-1]['text']


def test_missing_context_and_empty_list_are_not_equivalent():
    a = {'quote': None, 'event_type': 'unknown', 'conditions': []}
    b = {'quote': None, 'conditions': None}
    assert compare.differences(a, b) == [
        {'field': 'conditions', 'first_reader': [], 'second_reader': None}]
    assert compare.differences({'quote': None, 'event_type': 'unknown'}, b) == []


def test_malformed_second_context_stays_raw_and_does_not_erase_first(tmp_path):
    first, answer, run = compared(tmp_path, {'conditions': 17})
    row = run['rows'][0]
    assert row['conditions'] == first['conditions']
    assert row['second_reader_proposal']['proposal']['conditions'] == 17
    assert row['disagreement']['second_reader'] is None
    assert any(t['step'] == 'Proposal shape' for t in row['trail'])
    assert current_state(run, [])['targets']['e1']['needs_attention']


def test_legacy_row_without_hint_does_not_invent_reader_disagreement():
    proposal, run = make_run(CASES[0])
    row = run['rows'][0]
    row.pop('type_hint')
    original = copy.deepcopy(row)
    assert not any(d['field'] == 'type_hint' for d in compare.differences(row, proposal))
    assert row == original
    assert current_state(run, [])['targets']['e1']['semantic_status'] == 'unresolved'
    # This does not excuse conflicting supplied meanings on newer rows.
    row['type_hint'] = 'FDA approval'
    assert any(d['field'] == 'type_hint' for d in compare.differences(row, proposal))
