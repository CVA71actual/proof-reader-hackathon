"""PIVOT RED 4: a served report is the exact bundle its manifest names.

The manifest binds run, decisions and code (inputs) and, since RED 4, the
SHA-256 of each produced PDF/DOCX. A replaced, deleted or unbound artifact is
not current: the route refuses with the ordinary stale reason and the run API
reports the same reason. Reads never rebuild.
"""
import json

import pytest

from report import manifest as manifest_mod
from server.app import REPORT_STALE_REASON
from tests.test_server import _report_client

ROOT = 'runs/sample'


def _paths(tmp_path, pdf, docx):
    report = tmp_path / 'runs/sample/report'
    return report, report / pdf, report / docx


def _refused(c, name):
    r = c.get(f'/runs/sample/report/{name}')
    assert r.status_code == 404, name
    assert r.json()['detail'] == REPORT_STALE_REASON
    assert r.headers['X-Report-Reason'] == REPORT_STALE_REASON
    h = c.head(f'/runs/sample/report/{name}')
    assert h.status_code == 404 and h.headers['X-Report-Reason'] == REPORT_STALE_REASON
    detail = c.get('/api/runs/sample').json()
    assert detail['report_stale'] is True
    assert detail['report_stale_reason'] == REPORT_STALE_REASON


def _unchanged_inputs(tmp_path):
    run_dir = tmp_path / 'runs/sample'
    return {p.name: p.read_bytes() for p in (run_dir / 'run.json', run_dir / 'decisions.jsonl')}


def test_manifest_binds_each_artifact_digest(tmp_path):
    import hashlib
    c, pdf, docx = _report_client(tmp_path)
    report, pdf_path, docx_path = _paths(tmp_path, pdf, docx)
    saved = json.loads((report / 'manifest.json').read_text())
    assert saved['artifacts'] == {pdf: hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                                  docx: hashlib.sha256(docx_path.read_bytes()).hexdigest()}
    for name, path in ((pdf, pdf_path), (docx, docx_path)):
        r = c.get(f'/runs/sample/report/{name}')
        assert r.status_code == 200 and r.content == path.read_bytes()
    assert c.get('/api/runs/sample').json()['report_stale'] is False


@pytest.mark.parametrize('case', ['A_pdf_replaced', 'B_docx_replaced', 'C_pdf_deleted',
                                  'C_docx_deleted', 'D_legacy_manifest', 'D_one_digest_missing'])
def test_unbound_artifact_makes_whole_bundle_not_current(tmp_path, case):
    c, pdf, docx = _report_client(tmp_path)
    report, pdf_path, docx_path = _paths(tmp_path, pdf, docx)
    before = _unchanged_inputs(tmp_path)
    if case == 'A_pdf_replaced':
        pdf_path.write_bytes(b'%PDF-1.4 an earlier or forged report')
    elif case == 'B_docx_replaced':
        docx_path.write_bytes(b'PK an earlier or forged report')
    elif case == 'C_pdf_deleted':
        pdf_path.unlink()
    elif case == 'C_docx_deleted':
        docx_path.unlink()
    else:
        saved = json.loads((report / 'manifest.json').read_text())
        if case == 'D_legacy_manifest':
            saved.pop('artifacts')
        else:
            saved['artifacts'].pop(docx)
        (report / 'manifest.json').write_text(json.dumps(saved))
    # Both names refuse: an apparently valid sibling is not served as current.
    for name in (pdf, docx):
        _refused(c, name)
    assert _unchanged_inputs(tmp_path) == before, 'Reads must never rebuild or rewrite'
    if case.startswith('C_'):
        assert not (pdf_path.exists() and docx_path.exists())


def test_route_serves_exactly_the_bytes_it_verified(tmp_path, monkeypatch):
    # Case E: the file changes after it was read and hashed. The route must
    # return the verified in-memory bytes and never re-open the file.
    c, pdf, docx = _report_client(tmp_path)
    _, pdf_path, _ = _paths(tmp_path, pdf, docx)
    verified = pdf_path.read_bytes()
    reads = []
    real = manifest_mod._read_artifact

    def read_then_swap(path):
        data = real(path)
        if path.name == pdf:
            reads.append(path.name)
            path.write_bytes(b'%PDF-1.4 swapped after verification')
        return data

    monkeypatch.setattr(manifest_mod, '_read_artifact', read_then_swap)
    r = c.get(f'/runs/sample/report/{pdf}')
    assert r.status_code == 200 and r.content == verified
    assert reads == [pdf]
    monkeypatch.setattr(manifest_mod, '_read_artifact', real)
    _refused(c, pdf)


def test_build_publishes_a_complete_bound_bundle_and_rebuild_is_current(tmp_path):
    import shutil
    from pathlib import Path
    from fastapi.testclient import TestClient
    from report.build import build
    from report.data import report_filenames
    from server.app import create_app
    repo = Path(__file__).resolve().parents[1]
    runs = tmp_path / 'runs'
    shutil.copytree(repo / 'runs/sample', runs / 'sample')
    shutil.rmtree(runs / 'sample/report', ignore_errors=True)
    build('sample', run_dir=runs / 'sample', force=True, generated_at='2026-11-01T18:00:00Z')
    number = json.loads((runs / 'sample/run.json').read_text())['run']['number']
    pdf, docx = report_filenames(number)
    report = runs / 'sample/report'
    assert sorted(p.name for p in report.iterdir()) == sorted([pdf, docx, 'manifest.json'])
    c = TestClient(create_app(runs_dir=runs, ui_dir=tmp_path / 'none', reader=object()))
    assert c.get(f'/runs/sample/report/{pdf}').status_code == 200
    (report / pdf).write_bytes(b'%PDF-1.4 replaced')
    r = c.get(f'/runs/sample/report/{pdf}')
    assert r.status_code == 404 and r.json()['detail'] == REPORT_STALE_REASON
    build('sample', run_dir=runs / 'sample', force=True, generated_at='2026-11-01T18:00:00Z')
    for name in (pdf, docx):
        r = c.get(f'/runs/sample/report/{name}')
        assert r.status_code == 200 and r.content == (report / name).read_bytes()


def test_successful_rebuild_after_a_failed_automatic_rebuild_is_current(tmp_path, monkeypatch):
    # PIVOT RED 4b: a failed automatic rebuild sets report_stale; a later
    # successful explicit build of the saved report must make it current again.
    import hashlib
    import shutil
    from pathlib import Path
    from fastapi.testclient import TestClient
    from report import build as build_mod
    from report.data import report_filenames
    from server.app import create_app
    repo = Path(__file__).resolve().parents[1]
    runs = tmp_path / 'runs'
    shutil.copytree(repo / 'runs/sample', runs / 'sample')
    shutil.rmtree(runs / 'sample/report', ignore_errors=True)
    run_dir = runs / 'sample'
    build_mod.build('sample', run_dir=run_dir, force=True, generated_at='2026-11-01T18:00:00Z')
    number = json.loads((run_dir / 'run.json').read_text())['run']['number']
    names = report_filenames(number)
    c = TestClient(create_app(runs_dir=runs, ui_dir=tmp_path / 'none', reader=object()))
    assert all(c.get(f'/runs/sample/report/{n}').status_code == 200 for n in names)
    log = run_dir / 'decisions.jsonl'

    def broken(view):
        raise RuntimeError('builder failed')
    monkeypatch.setattr(build_mod, 'build_pdf', broken)
    state = c.get('/api/runs/sample').json()['state']['targets']
    target = next(t for t, s in state.items() if s['kind'] == 'row')
    r = c.post('/api/runs/sample/decisions', json={'target': target, 'action': 'remove'})
    assert r.status_code == 200, r.text
    assert 'could not rebuild' in r.json()['report_refresh']
    assert json.loads((run_dir / 'run.json').read_text())['report_stale'] is True
    for name in names:
        _refused(c, name)
    monkeypatch.undo()
    log_before = log.read_bytes()
    build_mod.build('sample', force=True, run_dir=run_dir, generated_at='2026-11-01T18:00:00Z')
    saved_run = json.loads((run_dir / 'run.json').read_text())
    assert 'report_stale' not in saved_run and 'report_stale_reason' not in saved_run
    assert log.read_bytes() == log_before
    detail = c.get('/api/runs/sample').json()
    assert detail['report_stale'] is False and detail['report_stale_reason'] is None
    digests = json.loads((run_dir / 'report/manifest.json').read_text())['artifacts']
    for name in names:
        r = c.get(f'/runs/sample/report/{name}')
        assert r.status_code == 200
        assert hashlib.sha256(r.content).hexdigest() == digests[name]


def test_test_export_with_out_does_not_clear_the_saved_report_flag(tmp_path):
    import shutil
    from pathlib import Path
    from report.build import build
    repo = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / 'runs/sample'
    shutil.copytree(repo / 'runs/sample', run_dir)
    raw = json.loads((run_dir / 'run.json').read_text())
    raw['report_stale'], raw['report_stale_reason'] = True, 'A rebuild failed.'
    (run_dir / 'run.json').write_text(json.dumps(raw))
    build('sample', run_dir=run_dir, out=tmp_path / 'export', generated_at='2026-11-01T18:00:00Z')
    assert json.loads((run_dir / 'run.json').read_text())['report_stale'] is True


def test_sticky_flag_without_a_valid_bundle_still_refuses(tmp_path):
    c, pdf, docx = _report_client(tmp_path)
    run_path = tmp_path / 'runs/sample/run.json'
    raw = json.loads(run_path.read_text())
    raw['report_stale'] = True
    run_path.write_text(json.dumps(raw))
    (tmp_path / 'runs/sample/report' / pdf).write_bytes(b'%PDF-1.4 torn')
    for name in (pdf, docx):
        _refused(c, name)
