"""Pages without rows must have the same identity in offline and API loads."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

from report.data import load_run_dir
from report.manifest import report_manifest
from runs.sources import SourcesError
from server.app import RunStore

SAVED = Path(__file__).resolve().parents[1]/'runs/twelve-4'


def test_offline_and_api_run_identity_includes_pages_without_rows():
    before = (SAVED/'run.json').read_bytes()
    run, decisions = load_run_dir(SAVED)
    store = RunStore(SAVED.parent)
    served = store.run(SAVED.name)
    referenced = {(r['source_id'], r['page_number']) for r in run['rows']}
    unused = [p for s in run['sources'] for p in s['pages']
              if (s['id'], p['page_number']) not in referenced]
    assert unused, 'This reproduction must include pages that produced no rows'
    assert all(p.get('visual_evidence') for p in unused)
    assert run == served
    assert report_manifest(run, decisions) == report_manifest(served, store.decisions(SAVED.name))
    assert (SAVED/'run.json').read_bytes() == before


def test_unused_page_tamper_refused_by_both_loaders(tmp_path):
    path = tmp_path/'twelve-4'
    shutil.copytree(SAVED, path)
    run = json.loads((path/'run.json').read_text())
    referenced = {(r['source_id'], r['page_number']) for r in run['rows']}
    page = next(p for s in run['sources'] for p in s['pages']
                if (s['id'], p['page_number']) not in referenced and p.get('words'))
    f = path/page['words']
    f.write_bytes(f.read_bytes()+b' ')
    with pytest.raises(SourcesError):
        load_run_dir(path)
    with pytest.raises(HTTPException) as exc:
        RunStore(tmp_path).run(path.name)
    assert exc.value.status_code == 409


@pytest.mark.parametrize("changed", ["runs/review.py", "runs/proposal.py", "runs/check_load.py", "runs/sources.py"])
def test_changed_review_code_expires_saved_report_manifest(tmp_path, monkeypatch, changed):
    from report import manifest
    for name in manifest.VERSION_FILES:
        target = tmp_path/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest.ROOT/name, target)
    monkeypatch.setattr(manifest, 'ROOT', tmp_path)
    out = tmp_path/'output'
    out.mkdir()
    run = {'run': {'id': 'test', 'number': 1}, 'rows': []}
    # RED 4: a current report is a bound bundle, so the fixture needs both artifacts.
    from report.data import report_filenames
    for name in report_filenames(1):
        (out/name).write_bytes(b'artifact ' + name.encode())
    manifest.write_report_manifest(out, run, [])
    assert manifest.report_is_current(out, run, [])
    (tmp_path/changed).write_text('# changed consumer rules')
    assert not manifest.report_is_current(out, run, [])
