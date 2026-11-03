"""Offline PTGX demo path through production API, report download, and decision replay.

Only a temporary copy is changed. Saved model readings and captured pages are
used without model calls. The qualification mutation is an adversarial test,
not a statement in the real filing.
"""
import hashlib
import json
import shutil
from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader

from report.build import build, report_filenames
from server.app import create_app

REPO = Path(__file__).resolve().parents[1]
SAVED = REPO / 'runs/ptgx-r5'


class NoModelCalls:
    def __getattr__(self, method):
        raise AssertionError('The saved-source demo must not call a model: ' + method)


def file_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def text_of(response, suffix):
    assert response.status_code == 200, response.text[:400] if response.status_code != 200 else ''
    if suffix == '.pdf':
        return '\n'.join(page.extract_text() or '' for page in PdfReader(BytesIO(response.content)).pages)
    doc = Document(BytesIO(response.content))
    return '\n'.join([p.text for p in doc.paragraphs] +
                     [cell.text for table in doc.tables for row in table.rows for cell in row.cells])


def test_saved_ptgx_fix_keep_revision_undo_and_downloads_agree(tmp_path):
    original = file_hashes(SAVED)
    path = tmp_path/'ptgx-r5'
    shutil.copytree(SAVED, path)
    page_hashes = file_hashes(path/'pages')
    log = path/'decisions.jsonl'
    root = '/api/runs/ptgx-r5'
    app = create_app(runs_dir=tmp_path, reader=NoModelCalls(), second_reader=None,
                     wonder_reader=None, sam_reader=None)
    with TestClient(app) as client:
        initial = client.get(root)
        assert initial.status_code == 200, initial.text
        data = initial.json()
        row = next(r for r in data['rows'] if r.get('program') == 'PN-8047')
        target = row['id']
        quote = row['quote']
        assert 'Phase 1 initiation anticipated in Q1 2027' in quote
        source = next(s for s in data['sources'] if s['id'] == row['source_id'])
        page = next(p for p in source['pages'] if p['page_number'] == row['page_number'])
        image = client.get('/runs/ptgx-r5/pages/' + Path(page['image']).name)
        assert image.status_code == 200
        assert hashlib.sha256(image.content).hexdigest() == page['image_sha256']
        number = data['run']['number']
        names = report_filenames(number)
        build('ptgx-r5', run_dir=path, force=True, generated_at='2026-11-01T21:00:00Z')

        def inspect(accepted, *, stale_export=False):
            before = log.read_bytes()
            got = client.get(root)
            assert got.status_code == 200, got.text
            data = got.json(); state = data['state']; current = state['targets'][target]
            calendar = client.get(root+'/calendar').json()['entries']
            assert current['accepted'] == accepted
            # CALENDAR-RULE-001: review only with an issue; a clean unkept row is placed automatically
            assert current['needs_attention'] == (not accepted and bool(current['issues']))
            assert (target in {r['id'] for r in calendar}) == current['on_calendar']
            assert (current['inclusion'] == 'kept') == accepted
            assert current['values']['quote'] == quote
            assert state['counts']['review']['accepted'] == int(accepted)
            assert state['counts']['review']['proposed'] == len(data['rows']) - int(accepted)
            for name in names:
                response = client.get('/runs/ptgx-r5/report/'+name)
                if stale_export:
                    assert response.status_code == 404, 'Old accepted output must not remain downloadable'
                else:
                    text = text_of(response, Path(name).suffix)
                    flat = ' '.join(text.split())
                    inc = state['counts']['inclusion']
                    assert f'Kept by you: {int(accepted)}' in flat
                    assert f'Included automatically — not reviewed by you: {inc["included"]}' in flat
                    assert f'Needs your review: {inc["review"]}' in flat
                    assert 'PN-8047' in text and 'Q1 2027' in text
                    assert 'IND-enabling studies' in text
                    if accepted:
                        assert 'Kept by you' in text
            assert log.read_bytes() == before, 'Reads must not rewrite decision history'
            return current

        def decide(action, **extra):
            before = log.read_bytes()
            response = client.post(root+'/decisions', json={'target':target,'action':action,**extra})
            assert response.status_code == 200, response.text
            assert log.read_bytes().startswith(before), 'Decisions must append, not rewrite earlier lines'
            return response.json()

        inspect(False)
        decide('fix', fix={'field':'title','from':row['title'],'to':row['title']+' — PN-8047'})
        assert inspect(False)['decision_label'] == 'Fixed by you — not yet accepted'
        decide('keep')
        inspect(True)
        kept_bytes = log.read_bytes()
        assert json.loads(kept_bytes.splitlines()[-1])['review_basis']['sha256']
        inspect(True)  # reload without a state change
        changed = json.loads((path/'run.json').read_text())
        next(r for r in changed['rows'] if r['id']==target)['conditions'] = ['Planted test condition']
        (path/'run.json').write_text(json.dumps(changed))
        assert inspect(False, stale_export=True)['decision_label'] == 'Previous Keep — needs your review'
        assert log.read_bytes() == kept_bytes
        decide('keep')
        assert inspect(True)['review_history']  # history is not an active hold
        decide('undo')
        inspect(False)
        decide('remove')
        state = client.get(root).json()['state']['targets'][target]
        assert state['review_bucket'] == 'excluded' and not state['needs_attention']
        assert target not in {r['id'] for r in client.get(root+'/calendar').json()['entries']}
        for name in names:
            text = text_of(client.get('/runs/ptgx-r5/report/' + name), Path(name).suffix)
            assert 'Kept by you: 0' in text and 'Excluded: 1' in text
    assert file_hashes(path/'pages') == page_hashes
    assert file_hashes(SAVED) == original


def test_saved_vktx_earnings_call_date_fix_keep_and_undo_agree_with_past_report(tmp_path):
    """An actual saved earnings-call proposal, not a new reading or synthetic future date."""
    from report.data import build_view, load_run_dir

    saved = REPO/'runs/twelve-4'
    original = file_hashes(saved)
    path = tmp_path/'twelve-4'
    shutil.copytree(saved, path)
    shutil.rmtree(path/'report', ignore_errors=True)
    page_hashes = file_hashes(path/'pages')
    log = path/'decisions.jsonl'
    root = '/api/runs/twelve-4'
    app = create_app(runs_dir=tmp_path, reader=NoModelCalls(), second_reader=None,
                     wonder_reader=None, sam_reader=None)
    with TestClient(app) as client:
        initial = client.get(root)
        assert initial.status_code == 200, initial.text
        data = initial.json()
        row = next(r for r in data['rows'] if r['id'] == 'e81')
        quote = "Management will host a conference call to discuss Viking's third quarter 2026 financial results today at 4:30 pm Eastern."
        assert row['quote'] == quote and row['date'] is None
        source = next(s for s in data['sources'] if s['id'] == row['source_id'])
        assert source['published_date'] == '2026-10-28'
        assert source['url'].endswith('/000119312526436145/vktx-ex99_1.htm')
        page = next(p for p in source['pages'] if p['page_number'] == row['page_number'])
        response = client.get('/runs/twelve-4/pages/' + Path(page['image']).name)
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == page['image_sha256']
        assert not data['state']['targets']['e81']['accepted']

        def decide(action, **extra):
            before = log.read_bytes()
            response = client.post(root+'/decisions', json={'target': 'e81', 'action': action, **extra})
            assert response.status_code == 200, response.text
            assert log.read_bytes().startswith(before)
            return client.get(root).json()

        fixed = decide('fix', fix={'field': 'date', 'from': None, 'to': '2026-10-28'})
        st = fixed['state']['targets']['e81']
        assert st['values']['date'] == '2026-10-28'
        assert st['values']['quote'] == quote
        assert not st['accepted'] and st['inclusion'] != 'kept'
        assert st['needs_attention'] == bool(st['issues'])
        # The date check compares the stated date. Event meaning is explicitly
        # outside code checking; Keep must preserve both results.
        date_check = next(c for c in st['recheck']['checks'] if c['name'] == 'date_match')
        assert date_check['result'] == 'pass'
        checked_results = {c['name']: c['result'] for c in st['recheck']['checks']}
        assert checked_results['type_match'] == 'not_needed'
        assert st['recheck']['result'] == 'pass'
        # CALENDAR-RULE-001: unkept and clean with a past date, it is Context (past), never on the calendar
        assert st['inclusion'] == 'context' and st['review_label'] == 'Context (past)'
        assert 'e81' not in {r['id'] for r in fixed['calendar_partition']['calendar']}

        accepted = decide('keep')
        assert accepted['state']['targets']['e81']['accepted']
        assert {c['name']: c['result'] for c in accepted['state']['targets']['e81']['recheck']['checks']} == checked_results
        assert 'e81' in {r['id'] for r in accepted['calendar_partition']['past']}
        assert 'e81' not in {r['id'] for r in accepted['calendar_partition']['calendar']}
        loaded, decisions = load_run_dir(path)
        view = build_view(loaded, decisions, generated_at="2026-11-01T21:00:00Z")
        exported = next(r for r in view['past'] if r['id'] == 'e81')
        assert exported['quote'] == quote and exported['sort_key'][0] == '2026-10-28'
        assert exported['person_tag'] == 'Kept by you'
        assert {k: view['counts'][k] for k in ('accepted', 'proposed', 'excluded')} == accepted['state']['counts']['review']
        assert view['counts']['accepted'] == 1
        build('twelve-4', run_dir=path, force=True)
        for name in report_filenames(loaded['run']['number']):
            text = text_of(client.get('/runs/twelve-4/report/'+name), Path(name).suffix)
            assert "Viking's third quarter 2026 financial results" in text
            assert 'Kept by you: 1' in text
        history = log.read_bytes()
        assert client.get(root).json()['state']['targets']['e81']['accepted']
        assert log.read_bytes() == history
        undone = decide('undo')
        assert not undone['state']['targets']['e81']['accepted']
        assert undone['state']['targets']['e81']['values']['date'] == '2026-10-28'
        assert undone['state']['targets']['e81']['inclusion'] == 'context'     # CALENDAR-RULE-001
        assert 'e81' not in {r['id'] for r in undone['calendar_partition']['calendar']}
    assert file_hashes(path/'pages') == page_hashes
    assert file_hashes(saved) == original
