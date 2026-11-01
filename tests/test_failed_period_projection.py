"""Check 31: offline API reproduction, only temporary run copies are changed."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from tests.conftest import REPO


def setup_run(tmp_path, explicit=False):
    d = tmp_path / 'ptgx-r4a'
    shutil.copytree(REPO / 'runs/ptgx-r4a', d)
    (d / 'decisions.jsonl').write_text('')
    run = json.loads((d / 'run.json').read_text())
    row = run['rows'][1]
    row['date_text'] = 'Q1 2027'
    row['date'] = '2027-01-01' if explicit else None
    row['date_end'] = '2027-03-31' if explicit else None
    for check in row['checks']:
        check.pop('assessment', None)
    (d / 'run.json').write_text(json.dumps(run))
    return row['id']


def state(client, target):
    return client.get('/api/runs/ptgx-r4a').json()['state']['targets'][target]


def change_period(client, target):
    assert state(client, target)['values']['date'] == '2027-01-01'
    response = client.post('/api/runs/ptgx-r4a/decisions', json={
        'target': target, 'action': 'fix',
        'fix': {'field': 'date_text', 'from': 'Q1 2027', 'to': 'Q2 2027'}})
    assert response.status_code == 200, response.json()
    current = state(client, target)
    assert current['status'] == 'failed', current
    assert current['values']['date_text'] == 'Q2 2027'
    return current


@pytest.mark.parametrize('keep', [False, True])
def test_failed_period_edit_drops_old_derived_bounds(tmp_path, keep):
    target = setup_run(tmp_path)
    with TestClient(create_app(tmp_path, background=False)) as client:
        current = change_period(client, target)
        if keep:
            response = client.post('/api/runs/ptgx-r4a/decisions', json={
                'target': target, 'action': 'keep'})
            assert response.status_code == 200, response.json()
            current = state(client, target)
            assert current['on_calendar'] is True
        print({'keep': keep, 'values': current['values'],
               'on_calendar': current['on_calendar'], 'status': current['status']})
        # Raw candidate has no explicit bounds. A failed new assessment cannot
        # carry forward the old Q1 assessment's derived dates as current values.
        assert current['values']['date'] is None, current['values']
        assert current['values']['date_end'] is None, current['values']


def test_explicit_candidate_bounds_are_not_indiscriminately_cleared(tmp_path):
    target = setup_run(tmp_path, explicit=True)
    with TestClient(create_app(tmp_path, background=False)) as client:
        current = change_period(client, target)
        assert current['values']['date'] == '2027-01-01'
        assert current['values']['date_end'] == '2027-03-31'


def test_undo_restores_supported_period(tmp_path):
    target = setup_run(tmp_path)
    with TestClient(create_app(tmp_path, background=False)) as client:
        change_period(client, target)
        response = client.post('/api/runs/ptgx-r4a/decisions', json={
            'target': target, 'action': 'undo'})
        assert response.status_code == 200, response.json()
        current = state(client, target)
        assert current['values']['date_text'] == 'Q1 2027'
        assert current['values']['date'] == '2027-01-01'
        assert current['values']['date_end'] == '2027-03-31'
