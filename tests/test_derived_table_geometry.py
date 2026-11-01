import copy
import json
from pathlib import Path
from fastapi.testclient import TestClient
from server.app import create_app
from tests.test_r27_picture import picture_index, run, FakeSam, BOXES, Transcriber, RUN_SCHEMA
from runs import sources

def test_derived_table_does_not_claim_word_geometry(tmp_path):
    doc, run_dir, _ = run(tmp_path, picture_index.__wrapped__(tmp_path), FakeSam(BOXES), Transcriber())
    source = doc['sources'][0]
    page = source['pages'][0]
    words, consumed = sources.load_page_evidence(run_dir, page)
    assert consumed['kind'] == 'derived'
    w = next(w for w in words['words'] if w['text'] == '2b')
    template = json.loads((Path(__file__).resolve().parents[0] / 'table_template.json').read_text())
    table = copy.deepcopy(template)
    table.update(id='t-derived', source_id=source['id'], page_number=page['page_number'])
    table['rows'] = [table['rows'][0]]
    table['rows'][0]['cells'] = [table['rows'][0]['cells'][0]]
    cell = table['rows'][0]['cells'][0]
    cell.update(id='c-derived',value='2b',box=[w[k] for k in ('x0','y0','x1','y1')],checks=[])
    doc['tables']=[table]
    RUN_SCHEMA.validate(doc)
    (run_dir/'run.json').write_text(json.dumps(doc))
    with TestClient(create_app(runs_dir=run_dir.parent,background=False)) as client:
        response=client.get('/api/runs/run-1')
    assert response.status_code == 200,response.text
    cell=response.json()['tables'][0]['rows'][0]['cells'][0]
    selector=next(c for c in cell['checks'] if c['name']=='selector_match')
    assert selector['result'] != 'pass',selector
