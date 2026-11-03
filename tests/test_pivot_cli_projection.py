"""CLI acceptance counts follow the same bound review state as the API and reports."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runs import demo, recheck_dates, sources, twelve
from runs.calendar import calendar_entries
from runs.state import current_state
from server.app import create_app
from tests.test_pivot_proposal_flow import CASES, decision, kept_decision, make_run
from tests.test_pivot_saved_demo import NoModelCalls


def test_cli_never_calls_passing_checks_or_fix_only_accepted():
    _, run = make_run(CASES[1])
    run['run'].update(label='Synthetic CLI check', spend_usd=1.0)
    edit = {**decision('fix'), 'fix': {'field': 'title', 'from': run['rows'][0]['title'], 'to': 'Edited title'}}
    keep = kept_decision(run, [edit])
    for log, accepted in (([], 0), ([edit], 0), ([edit, keep], 1), ([edit, keep, decision('undo')], 0)):
        state = current_state(run, log)
        text = '\n'.join(twelve.summary(run, state, calendar_entries(run, state)))
        assert 'check results: matched 1' in text  # Mechanical evidence remains independent.
        # CALENDAR-RULE-001: a clean in-window row is on the calendar either kept by you or included automatically
        # (an edit with no re-check yet is an issue: neither kept nor included until it is checked or kept)
        auto = int(log == [])     # every other log here carries the un-re-checked edit
        assert f'calendar: {accepted} kept by you, {auto} included automatically' in text
        assert f'rows: Kept by you {accepted}, Included automatically (not reviewed by you) {auto}' in text
        assert f'accepted rows: {accepted}; cost per accepted row: ' + ('$1.0000' if accepted else 'not computed') in text
        assert 'rows: kept' not in text and 'kept or corrected' not in text
        assert recheck_dates._counts(run, log) == {
            'Excluded': 0, 'Proposed — needs review': 1-accepted, 'Accepted by you': accepted}
    run['rows'][0]['conditions'] = ['Planted changed condition']
    assert recheck_dates._counts(run, [edit, keep])['Accepted by you'] == 0


@pytest.fixture
def saved_run(tmp_path):
    path = tmp_path/'ptgx-r5'
    shutil.copytree(Path(__file__).resolve().parents[1]/'runs/ptgx-r5', path)
    shutil.rmtree(path/'report', ignore_errors=True)
    app = create_app(runs_dir=tmp_path, reader=NoModelCalls(), second_reader=None,
                     wonder_reader=None, sam_reader=None)
    with TestClient(app) as client:
        data = client.get('/api/runs/ptgx-r5').json()
        target = data['rows'][0]['id']
        response = client.post('/api/runs/ptgx-r5/decisions', json={'target': target, 'action': 'keep'})
        assert response.status_code == 200, response.text
        expected = client.get('/api/runs/ptgx-r5').json()['state']['counts']['review']
        assert expected['accepted'] == 1
    return path, expected


def test_recheck_cli_and_demo_load_same_bound_keep_as_api_without_writing(saved_run, capsys):
    path, expected = saved_run
    before = {p: p.read_bytes() for p in path.rglob('*') if p.is_file()}
    assert recheck_dates.main(['ptgx-r5', '--runs-dir', str(path.parent)]) == 0
    text = capsys.readouterr().out
    counts = (f'Excluded {expected["excluded"]}, Proposed — needs review {expected["proposed"]}, '
              f'Accepted by you {expected["accepted"]}')
    assert f'before {counts} | after {counts}' in text
    assert 'Checked and kept' not in text
    listed = '\n'.join(demo.list_lines(path.parent))
    assert f'Accepted by you 1, Proposed — needs review {expected["proposed"]}, Excluded {expected["excluded"]}' in listed
    assert {p: p.read_bytes() for p in path.rglob('*') if p.is_file()} == before


def test_recheck_cli_refuses_changed_page_before_printing_acceptance(saved_run, capsys):
    path, _ = saved_run
    run = json.loads((path/'run.json').read_text())
    page = run['sources'][0]['pages'][0]
    words = path/page['words']
    words.write_bytes(words.read_bytes()+b' ')
    log = (path/'decisions.jsonl').read_bytes()
    with pytest.raises(sources.SourcesError):
        recheck_dates.main(['ptgx-r5', '--runs-dir', str(path.parent), '--write'])
    assert 'Accepted by you' not in capsys.readouterr().out
    assert (path/'decisions.jsonl').read_bytes() == log
