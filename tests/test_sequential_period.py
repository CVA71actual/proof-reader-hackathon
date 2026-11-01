"""Offline sequential edit reproduction; only writes temporary run copies."""
import json
import shutil
from fastapi.testclient import TestClient
from server.app import create_app
from tests.conftest import REPO

def test_correct_period_then_edit_derived_day(tmp_path):
    d = tmp_path / 'ptgx-r4a'
    shutil.copytree(REPO / 'runs/ptgx-r4a', d)
    (d / 'decisions.jsonl').write_text('')
    run = json.loads((d / 'run.json').read_text())
    row = run['rows'][1]
    row['date'] = row['date_end'] = None
    row['date_text'] = 'Q2 2027'  # the actual stored quote supports Q1
    for check in row['checks']:
        check.pop('assessment', None)
    (d / 'run.json').write_text(json.dumps(run))
    with TestClient(create_app(tmp_path, background=False)) as client:
        state = client.get('/api/runs/ptgx-r4a').json()['state']['targets'][row['id']]
        first = client.post('/api/runs/ptgx-r4a/decisions', json={
            'target': row['id'], 'action': 'fix',
            'fix': {'field': 'date_text', 'from': state['values']['date_text'], 'to': 'Q1 2027'}})
        assert first.status_code == 200, first.json()
        state = client.get('/api/runs/ptgx-r4a').json()['state']['targets'][row['id']]
        assert state['values']['date'] == '2027-01-01', state
        second = client.post('/api/runs/ptgx-r4a/decisions', json={
            'target': row['id'], 'action': 'fix',
            'fix': {'field': 'date', 'from': state['values']['date'], 'to': '2027-02-01'}})
        assert second.status_code == 200, second.json()
