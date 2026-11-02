"""An old Keep cannot accept a materially different proposal or source."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pathlib import Path

from report.data import build_view
from runs.calendar import calendar_entries
from runs.state import current_state
from server.app import create_app, RunStore, _decide
from tests.test_pivot_proposal_flow import make_run, CASES, AT, decision, kept_decision


@pytest.mark.parametrize('field,value', [
    ('conditions', []), ('type_hint', 'FDA approval'), ('title', 'Different action'),
    ('program', 'CB-200'), ('indication', 'Another indication'),
    ('subject_company', 'Other company'), ('relation', 'Different relationship'),
    ('relevance', 'Different proposed consequence'), ('quote', 'A different quote.'),
    ('date_text', 'Q2 2027'), ('date', '2027-02-01'), ('date_end', '2027-02-28'),
    ('box', [1, 2, 3, 4]), ('page_number', 2),
])
def test_material_revision_invalidates_keep_without_rewriting_history(field, value):
    _, run = make_run(CASES[1])
    keep = kept_decision(run)
    log = [keep]
    original_log = copy.deepcopy(log)
    before = current_state(run, log)
    assert before['targets']['e1']['on_calendar']
    run['rows'][0][field] = value
    state = current_state(run, log)
    target = state['targets']['e1']
    assert target['semantic_status'] == 'unresolved'
    assert target['needs_attention'] and not target['on_calendar']
    assert target['review_history'][0]['review_basis'] == keep['review_basis']
    assert not calendar_entries(run, state)
    assert len(build_view(run, log, generated_at=AT)['open_rows']) == 1
    assert log == original_log
    # A new explicit acceptance is scoped to the new revision; Undo restores
    # the expired Keep as history, never the old acceptance.
    fresh = kept_decision(run, log)
    assert current_state(run, log + [fresh])['targets']['e1']['on_calendar']
    assert not current_state(run, log + [fresh, decision('undo')])['targets']['e1']['on_calendar']


@pytest.mark.parametrize('field,value', [
    ('words_sha256', '1' * 64), ('image_sha256', '2' * 64),
    ('derived_words', {'file': 'new.words.json', 'sha256': '3' * 64}),
    ('visual_evidence', {'origin': 'approximate', 'overlays_supported': False, 'reason': 'Derived'}),
])
def test_evidence_revision_invalidates_keep(field, value):
    _, run = make_run(CASES[1])
    keep = kept_decision(run)
    run['sources'][0]['pages'][0][field] = value
    target = current_state(run, [keep])['targets']['e1']
    assert target['semantic_status'] == 'unresolved' and target['needs_attention']
    assert not target['on_calendar'] and target['review_history']


def test_accounting_and_trail_changes_do_not_expire_acceptance():
    _, run = make_run(CASES[1])
    keep = kept_decision(run)
    run['run']['spend_usd'] = 123
    run['rows'][0]['trail'].append({'step': 'Diagnostic', 'tag': 'Code', 'text': 'Non-material note'})
    run['rows'][0]['reason'] = 'New presentation wording'
    target = current_state(run, [keep])['targets']['e1']
    assert target['semantic_status'] == 'human_accepted' and target['on_calendar']


def test_unbound_or_malformed_keep_is_history():
    _, run = make_run(CASES[1])
    valid = kept_decision(run)
    for basis in (None, {}, {**valid['review_basis'], 'version': True}, {'version': 1, 'sha256': '0' * 64}):
        keep = {**decision('keep'), **({'review_basis': basis} if basis is not None else {})}
        target = current_state(run, [keep])['targets']['e1']
        assert target['semantic_status'] == 'unresolved'
        assert not target['on_calendar'] and target['review_history']


def test_api_stamps_basis_then_reload_expires_changed_condition(tmp_path):
    _, run = make_run(CASES[1], tmp_path / 'synthetic')
    run_path = tmp_path / 'synthetic/run.json'
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root = '/api/runs/synthetic'
        kept = client.post(root + '/decisions', json={'target': 'e1', 'action': 'keep'})
        assert kept.status_code == 200, kept.text
        assert kept.json()['state']['on_calendar']
        log_path = tmp_path / 'synthetic/decisions.jsonl'
        raw_log = log_path.read_bytes()
        saved = json.loads(raw_log)
        assert saved['review_basis'] == kept.json()['state']['review_basis']
        schema = json.loads((Path(__file__).parents[1] / 'runs/schema/decision.schema.json').read_text())
        assert not list(Draft202012Validator(schema).iter_errors(saved))
        changed = json.loads(run_path.read_text())
        changed['rows'][0]['conditions'] = []
        run_path.write_text(json.dumps(changed))
        got = client.get(root)
        assert got.status_code == 200, got.text
        target = got.json()['state']['targets']['e1']
        assert target['semantic_status'] == 'unresolved' and target['needs_attention']
        assert not target['on_calendar'] and target['review_history']
        assert log_path.read_bytes() == raw_log


def test_decide_overwrites_supplied_basis_and_http_rejects_unrecognised_field(tmp_path):
    _, run = make_run(CASES[1], tmp_path / 'synthetic')
    forged = {'version': 1, 'sha256': '0' * 64}
    store = RunStore(tmp_path)
    # At the recording boundary the server replaces even a supplied value.
    result = _decide(store, 'synthetic', store.run('synthetic'), {**decision('keep'), 'review_basis': forged})
    assert result['state']['on_calendar']
    saved = json.loads((tmp_path / 'synthetic/decisions.jsonl').read_text())
    assert saved['review_basis'] != forged
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        response = client.post('/api/runs/synthetic/decisions', json={
            'target': 'e1', 'action': 'keep', 'review_basis': forged})
        # Existing HTTP unknown-field contract remains unchanged; no client hash
        # is accepted as authority. _decide is the only released app function.
        assert response.status_code == 422


def _with_source_digests(run, *, rendered=False):
    source = run['sources'][0]
    source['sha256'] = 'a' * 64
    if rendered:
        source['rendered_from'] = {'format': 'html', 'sha256': 'b' * 64}
    return run


@pytest.mark.parametrize('path,value', [
    (('sha256',), 'c' * 64),
    (('rendered_from', 'sha256'), 'd' * 64),
])
def test_source_document_digest_revision_invalidates_keep(path, value):
    # PIVOT RED 1: the cited pages were cut from a document identified by
    # source.sha256 (and, for rendered HTML, rendered_from.sha256). A changed
    # document digest is a changed source, so a Keep bound to the old one is
    # history, not current acceptance.
    _, run = make_run(CASES[1])
    _with_source_digests(run, rendered=True)
    keep = kept_decision(run)
    log = [keep]
    original_log = copy.deepcopy(log)
    assert current_state(run, log)['targets']['e1']['on_calendar']
    holder = run['sources'][0]
    for key in path[:-1]:
        holder = holder[key]
    holder[path[-1]] = value
    state = current_state(run, log)
    target = state['targets']['e1']
    assert target['semantic_status'] == 'unresolved' and target['needs_attention']
    assert not target['on_calendar'] and target['review_history']
    assert target['review_label'] == 'Previous Keep — needs your review'
    assert not calendar_entries(run, state)
    assert log == original_log


def test_api_reload_expires_keep_when_only_source_digest_changes(tmp_path):
    _, run = make_run(CASES[1])
    _with_source_digests(run)
    directory = tmp_path / 'synthetic'
    make_run(CASES[1], directory)
    run_path = directory / 'run.json'
    saved_run = json.loads(run_path.read_text())
    _with_source_digests(saved_run)
    run_path.write_text(json.dumps(saved_run))
    from report.build import build
    from report.data import report_filenames
    from server.app import REPORT_STALE_REASON
    build('synthetic', run_dir=directory, force=True, generated_at=AT)
    names = report_filenames(1)
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root = '/api/runs/synthetic'
        kept = client.post(root + '/decisions', json={'target': 'e1', 'action': 'keep'})
        assert kept.status_code == 200, kept.text
        assert kept.json()['state']['on_calendar']
        assert not client.get(root).json()['report_stale']
        for name in names:
            assert client.get('/runs/synthetic/report/' + name).status_code == 200
        log_path = directory / 'decisions.jsonl'
        raw_log = log_path.read_bytes()
        changed = json.loads(run_path.read_text())
        changed['sources'][0]['sha256'] = 'e' * 64
        run_path.write_text(json.dumps(changed))
        got = client.get(root)
        assert got.status_code == 200, got.text
        target = got.json()['state']['targets']['e1']
        assert target['semantic_status'] == 'unresolved' and target['needs_attention']
        assert not target['on_calendar']
        assert target['review_label'] == 'Previous Keep — needs your review'
        entries = client.get(root + '/calendar').json()['entries']
        assert 'e1' not in {e['id'] for e in entries}
        # The saved export bound to the old source is refused, never current.
        assert got.json()['report_stale']
        assert got.json()['report_stale_reason'] == REPORT_STALE_REASON
        for name in names:
            refused = client.get('/runs/synthetic/report/' + name)
            assert refused.status_code == 404
            assert refused.headers['X-Report-Reason'] == REPORT_STALE_REASON
        assert log_path.read_bytes() == raw_log


# Recorded with the pre-RED-1 basis formula (no source lineage) for the
# CASES[1] synthetic run carrying source.sha256='a'*64 and an HTML
# rendered_from. It stands in for any Keep saved before lineage was bound.
PRE_LINEAGE_V1_BASIS = {'version': 1,
                        'sha256': '8385cfb6c9c0b6bfdad51ca170dd7df22e23beab4b530f2e9afced5120d302eb'}


def test_pre_lineage_v1_keep_expires_fail_closed_and_new_keep_holds():
    _, run = make_run(CASES[1])
    _with_source_digests(run, rendered=True)
    old = {**decision('keep'), 'review_basis': PRE_LINEAGE_V1_BASIS}
    log = [old]
    original_log = copy.deepcopy(log)
    state = current_state(run, log)
    target = state['targets']['e1']
    assert target['semantic_status'] == 'unresolved' and target['needs_attention']
    assert not target['on_calendar'] and target['review_history']
    assert target['review_bucket'] == 'proposed'
    assert not calendar_entries(run, state)
    assert log == original_log
    fresh = kept_decision(run)
    assert fresh['review_basis']['version'] == 1
    assert fresh['review_basis'] != PRE_LINEAGE_V1_BASIS
    assert current_state(run, [fresh])['targets']['e1']['on_calendar']


@pytest.mark.parametrize('change', ['format', 'added', 'removed'])
def test_rendered_lineage_change_invalidates_keep(change):
    _, run = make_run(CASES[1])
    _with_source_digests(run, rendered=change != 'added')
    keep = kept_decision(run)
    source = run['sources'][0]
    if change == 'format':
        source['rendered_from']['format'] = 'pdf'
    elif change == 'added':
        source['rendered_from'] = {'format': 'html', 'sha256': 'b' * 64, 'renderer': 'r'}
    else:
        del source['rendered_from']
    target = current_state(run, [keep])['targets']['e1']
    assert not target['on_calendar'] and target['review_history']


def test_renderer_name_alone_does_not_expire_keep():
    _, run = make_run(CASES[1])
    _with_source_digests(run, rendered=True)
    run['sources'][0]['rendered_from']['renderer'] = 'first'
    keep = kept_decision(run)
    run['sources'][0]['rendered_from']['renderer'] = 'second'
    assert current_state(run, [keep])['targets']['e1']['on_calendar']
