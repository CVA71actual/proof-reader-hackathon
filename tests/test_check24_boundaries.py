"""Check 24: offline API/load boundary probes; writes only pytest temporary copies."""
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from server.app import create_app
from runs.state import current_state
from runs.sources import SourcesError
from report.data import load_run_dir
from tests.conftest import REPO


def copy_run(tmp_path, name):
    d = tmp_path / name
    shutil.copytree(REPO / 'runs' / name, d)
    shutil.rmtree(d / 'report', ignore_errors=True)
    (d / 'decisions.jsonl').write_text('')
    return d


def test_period_correction_accepts_the_current_value_returned_by_get(tmp_path):
    d = copy_run(tmp_path, 'ptgx-r4a')
    run = json.loads((d / 'run.json').read_text())
    row = run['rows'][1]
    row['date'] = row['date_end'] = None  # a period proposal containing wording only
    for c in row['checks']:
        c.pop('assessment', None)
    (d / 'run.json').write_text(json.dumps(run))
    with TestClient(create_app(tmp_path, background=False)) as client:
        get = client.get('/api/runs/ptgx-r4a')
        assert get.status_code == 200
        state = get.json()['state']['targets'][row['id']]
        assert not state['checks_stale']
        assert (state['values']['date'], state['values']['date_end']) == ('2027-01-01', '2027-03-31')
        response = client.post('/api/runs/ptgx-r4a/decisions', json={
            'target': row['id'], 'action': 'fix',
            'fix': {'field': 'date', 'from': state['values']['date'], 'to': '2027-02-01'}})
        assert response.status_code == 200, response.json()
        assert response.json()['state']['values']['date'] == '2027-02-01'


def test_unchecked_field_edit_is_recorded_without_a_demonstrated_correction(tmp_path):
    copy_run(tmp_path, 'sample')
    with TestClient(create_app(tmp_path, background=False)) as client:
        state = client.get('/api/runs/sample').json()['state']['targets']['e1']
        assert not state['checks_stale']
        response = client.post('/api/runs/sample/decisions', json={
            'target': 'e1', 'action': 'fix',
            'fix': {'field': 'title', 'from': state['values']['title'],
                    'to': state['values']['title'] + ' (reviewed)'}})
        assert response.status_code == 200, response.json()
        after = response.json()['state']
        assert after['recheck']['result'] == 'could_not_run'
        assert not after['fixes'][-1]['mistake']


def test_failed_artifact_cannot_leave_matched_cells_on_offline_load(tmp_path):
    d = copy_run(tmp_path, 'sample')
    original = json.loads((d / 'run.json').read_text())
    table = original['tables'][0]
    source = next(s for s in original['sources'] if s['id'] == table['source_id'])
    page = next(p for p in source['pages'] if p['page_number'] == table['page_number'])
    artifact = d / page['words']
    artifact.write_bytes(artifact.read_bytes() + b' ')
    with TestClient(create_app(tmp_path, background=False)) as client:
        assert client.get('/api/runs/sample').status_code == 409
    try:
        run, decisions = load_run_dir(d)
    except SourcesError:
        return  # Refusing the same artifact on both paths is an acceptable outcome.
    state = current_state(run, decisions)
    cell = table['rows'][0]['cells'][0]
    target = state['targets'][cell['id']]
    assert target['status'] != 'matched', target
    assert target['effective_checks'] == []


def test_explicit_day_fix_remains_accepted_positive_control(tmp_path):
    copy_run(tmp_path, 'sample')
    with TestClient(create_app(tmp_path, background=False)) as client:
        state = client.get('/api/runs/sample').json()['state']['targets']['e1']
        response = client.post('/api/runs/sample/decisions', json={
            'target': 'e1', 'action': 'fix',
            'fix': {'field': 'date', 'from': state['values']['date'], 'to': '2027-03-13'}})
        assert response.status_code == 200, response.json()
        assert response.json()['state']['status'] == 'failed'


def test_unchanged_artifact_offline_load_positive_control(tmp_path):
    d = copy_run(tmp_path, 'sample')
    run, decisions = load_run_dir(d)
    cell = run['tables'][0]['rows'][0]['cells'][0]
    state = current_state(run, decisions)['targets'][cell['id']]
    assert state['status'] == 'matched'
    assert len(state['effective_checks']) == 3
