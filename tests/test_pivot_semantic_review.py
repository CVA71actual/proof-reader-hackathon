"""Human acceptance is independent of mechanical check results."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from report.data import build_view
from runs.calendar import calendar_entries
from runs.state import current_state
from server.app import create_app
from tests.test_pivot_proposal_flow import make_run, CASES, AT, decision, kept_decision


@pytest.mark.parametrize('case', CASES, ids=[x[0] for x in CASES])
def test_hold_keep_undo_share_one_calendar_and_report_state(case):
    _, run = make_run(case)
    checks = copy.deepcopy(run['rows'][0]['checks'])
    before = current_state(run, [])['targets']['e1']
    assert before['semantic_status'] == 'unresolved'
    assert before['needs_attention'] and not before['on_calendar']
    assert not build_view(run, [], generated_at=AT)['calendar']
    assert len(build_view(run, [], generated_at=AT)['open_rows']) == 1
    keep = kept_decision(run)
    kept = current_state(run, [keep])
    target = kept['targets']['e1']
    assert target['semantic_status'] == 'human_accepted'
    assert target['on_calendar'] and not target['needs_attention']
    assert target['status'] == before['status']
    assert run['rows'][0]['checks'] == checks
    assert calendar_entries(run, kept)[0]['semantic_status'] == 'human_accepted'
    undone = current_state(run, [keep, decision('undo')])
    assert undone['targets']['e1']['semantic_status'] == 'unresolved'
    assert not calendar_entries(run, undone)
    assert len(build_view(run, [keep, decision('undo')], generated_at=AT)['open_rows']) == 1
    removed = current_state(run, [decision('remove')])
    assert removed['targets']['e1']['semantic_status'] == 'human_removed'
    assert not calendar_entries(run, removed)


def test_successful_mechanical_fix_still_requires_explicit_keep(tmp_path):
    _, run = make_run(CASES[1], tmp_path / 'synthetic', event_updates={'date': '2027-02-01'})
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root = '/api/runs/synthetic'
        fixed = client.post(root + '/decisions', json={'target': 'e1', 'action': 'fix', 'fix': {
            'field': 'date', 'from': '2027-02-01', 'to': '2027-01-01'}})
        assert fixed.status_code == 200, fixed.text
        s = fixed.json()['state']
        assert s['recheck']['result'] == 'pass'
        assert s['semantic_status'] == 'unresolved'
        assert s['needs_attention'] and not s['on_calendar']
        kept = client.post(root + '/decisions', json={'target': 'e1', 'action': 'keep'})
        assert kept.status_code == 200, kept.text
        assert kept.json()['state']['semantic_status'] == 'human_accepted'
        assert kept.json()['state']['on_calendar']
        undone = client.post(root + '/decisions', json={'target': 'e1', 'action': 'undo'})
        assert undone.status_code == 200, undone.text
        assert undone.json()['state']['values']['date'] == '2027-01-01'
        assert undone.json()['state']['semantic_status'] == 'unresolved'
        assert not undone.json()['state']['on_calendar']
        assert client.get(root).json()['state']['targets']['e1']['semantic_status'] == 'unresolved'


def test_automatic_code_correction_does_not_accept_meaning():
    _, run = make_run(CASES[0])
    run['rows'][0]['group'] = 'corrected'
    run['rows'][0]['reason'] = 'Code repaired a mechanical value.'
    state = current_state(run, [])['targets']['e1']
    assert state['semantic_status'] == 'unresolved'
    assert not state['on_calendar'] and state['needs_attention']
