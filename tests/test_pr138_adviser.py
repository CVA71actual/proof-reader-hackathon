"""Offline check 18. The legacy accuracy.json is a committed fixture (see its PROVENANCE.md); no git is used."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from accuracy import export
from server.app import create_app
from tests.test_accuracy_export import scored, NOW, REPO

LEGACY = REPO / 'tests/fixtures/accuracy_legacy_128d299'


def stale_case(tmp_path):
    d = tmp_path / 'sample'
    shutil.copytree(REPO / 'runs/sample', d)
    rep = scored(d)
    run = json.loads((d / 'run.json').read_text())
    cell = run['tables'][0]['rows'][0]['cells'][0]
    cell['value'], cell['status'], cell['checks'][0]['result'] = '115,316', 'failed', 'fail'
    (d / 'run.json').write_text(json.dumps(run))
    return d, rep


def test_legacy_stale_score_must_not_be_served(tmp_path):
    d, rep = stale_case(tmp_path)
    shutil.copy(LEGACY / 'accuracy.json', d / 'accuracy.json')
    legacy = json.loads((d / 'accuracy.json').read_text())
    assert legacy['content_sha256'] == export.content_hash(legacy)
    assert 'words_sha256' not in legacy
    assert scored(d)['models']['Muse Spark']['values']['found'] == 68
    with TestClient(create_app(tmp_path)) as client:
        result = client.get('/api/runs/sample/accuracy')
    assert result.status_code == 409, (result.status_code, result.json()['models']['Muse Spark']['values'])


def test_new_writer_refuses_same_stale_score(tmp_path):
    d, rep = stale_case(tmp_path)
    with pytest.raises(export.StaleReport):
        export.write(d, rep, now=NOW, tip='551915b', dirty=False)


def test_fresh_score_is_served_and_matches_fresh_inputs(tmp_path):
    d, _ = stale_case(tmp_path)
    export.write(d, scored(d), now=NOW, tip='551915b', dirty=False)
    with TestClient(create_app(tmp_path)) as client:
        result = client.get('/api/runs/sample/accuracy')
    assert result.status_code == 200
    assert result.json()['models']['Muse Spark']['values']['found'] == 68
