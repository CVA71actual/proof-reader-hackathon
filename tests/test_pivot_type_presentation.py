"""A stored code category must never masquerade as the reader's interpretation."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from report.data import build_view, load_run_dir
from runs.calendar import calendar_entries
from runs.state import current_state
from server.app import create_app
from tests.test_pivot_proposal_flow import AT, CASES, kept_decision, make_run


def projected(run, decisions):
    state = current_state(run, decisions)
    view = build_view(run, decisions, generated_at=AT)
    rows = sum((view[k] for k in ('open_rows','calendar','past','later','undated','timing_unresolved','contingent')), [])
    assert len(rows) == 1
    target = state['targets'][run['rows'][0]['id']]
    assert rows[0]['type_presentation'] == target['type_presentation']
    assert rows[0]['action'] == target['type_presentation']['summary']
    cal = calendar_entries(run, state)
    if target['on_calendar']:      # kept, or (CALENDAR-RULE-001) placed automatically
        assert cal[0]['type_presentation'] == target['type_presentation']
        assert (cal[0]['inclusion'] == 'kept') == target['accepted']
    else:
        assert not cal
    return target


@pytest.mark.parametrize('legacy', [True, False])
def test_code_category_and_reader_words_are_separate_before_and_after_keep(legacy):
    _, run = make_run(CASES[0])
    row = run['rows'][0]
    if legacy:
        row.pop('type_hint')
        row['event_type'] = 'trial_results'  # historical keyword misclassification, not today's inference
    original = copy.deepcopy(run)
    target = projected(run, [])
    p = target['type_presentation']
    assert not target['accepted']
    assert p['category'] == row['event_type']
    if legacy:
        assert p['category_origin'] == 'legacy_keyword'
        assert p['category_label'] == 'Category (from old keyword rule)'
        assert p['type_hint'] is None and p['type_hint_origin'] == 'not_supplied'
        assert 'from old keyword rule' in p['summary']
        assert 'trial_results' in p['summary']
    else:
        assert p['category_origin'] == 'unknown'
        assert p['type_hint'] == CASES[0][2]
        assert p['type_hint_origin'] == 'reader'
        assert CASES[0][2] in p['summary']  # freeform reader wording, not just "unknown"
    accepted = projected(run, [kept_decision(run)])
    assert accepted['accepted'] and accepted['type_presentation'] == p
    assert run == original


@pytest.mark.parametrize('hint', [None, ''])
def test_explicitly_unknown_new_hint_is_not_called_an_old_keyword_category(hint):
    _, run = make_run(CASES[0])
    run['rows'][0]['type_hint'] = hint
    target = projected(run, [])
    p = target['type_presentation']
    assert p['category_origin'] == 'unknown'
    assert p['type_hint'] is None and p['type_hint_origin'] == 'not_supplied'
    assert 'old keyword' not in p['summary']
    assert not target['accepted']


@pytest.mark.parametrize('field,new_value', [('event_type','milestone'),('type_hint','Manufacturing qualification'),('type_hint',None)])
def test_api_edits_and_undo_do_not_misattribute_a_persons_type_to_the_reader(tmp_path,field,new_value):
    directory = tmp_path/'synthetic'
    _, run = make_run(CASES[0],directory)
    row = run['rows'][0]
    if field == 'event_type':
        row.pop('type_hint')
        row['event_type'] = 'trial_results'
        (directory/'run.json').write_text(json.dumps(run))
    raw_before = (directory/'run.json').read_bytes()
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root='/api/runs/synthetic'
        initial=client.get(root).json()['state']['targets']['e1']
        assert client.post(root+'/decisions',json={'target':'e1','action':'keep'}).status_code==200
        result=client.post(root+'/decisions',json={'target':'e1','action':'fix','fix':{
            'field':field,'from':row.get(field),'to':new_value}})
        assert result.status_code==200,result.text
        target=result.json()['state']
        assert not target['accepted'] and target['inclusion'] != 'kept'     # CALENDAR-RULE-001
        p=target['type_presentation']
        if field=='event_type':
            assert p['category_origin']=='person' and p['category_label']=='Category edited by you'
        else:
            assert p['type_hint_origin']=='person' and p['type_hint_label']=='Type edited by you'
            assert p['type_hint']==new_value
        loaded,decisions=load_run_dir(directory)
        assert projected(loaded,decisions)['type_presentation']==p
        assert client.post(root+'/decisions',json={'target':'e1','action':'undo'}).status_code==200
        restored=client.get(root).json()['state']['targets']['e1']
        assert restored['accepted']
        assert restored['type_presentation']==initial['type_presentation']
    assert (directory/'run.json').read_bytes()==raw_before


def test_real_saved_keyword_category_is_disclosed_without_rewriting_source():
    directory=Path(__file__).resolve().parents[1]/'runs/twelve-4'
    before=hashlib.sha256((directory/'run.json').read_bytes()).hexdigest()
    run, _=load_run_dir(directory)
    row=next(r for r in run['rows'] if r['id']=='e81')
    assert 'type_hint' not in row
    run['rows']=[row]
    run['tables']=[]
    target=projected(run,[])
    p=target['type_presentation']
    assert p['category_origin']=='legacy_keyword' and p['type_hint'] is None
    assert p['category']==row['event_type']
    assert 'from old keyword rule' in p['summary']
    assert hashlib.sha256((directory/'run.json').read_bytes()).hexdigest()==before


def test_a_saved_hint_does_not_establish_that_the_category_came_from_the_reader():
    _, run = make_run(CASES[0])
    row = run['rows'][0]
    row['type_hint'] = 'earnings release'
    row['event_type'] = 'trial_results'
    target = projected(run, [])
    p = target['type_presentation']
    assert p['category_origin'] == 'stored' and p['category_label'] == 'Stored category'
    assert p['type_hint'] == 'earnings release' and p['type_hint_origin'] == 'reader'
    assert 'Stored category: trial_results' in p['summary']
    assert "Reader's proposed type: earnings release" in p['summary']
    assert not target['accepted'] and row['event_type'] == 'trial_results'


@pytest.mark.parametrize('legacy', [True, False])
def test_pdf_and_word_expose_type_provenance_for_accepted_proposal(legacy):
    from io import BytesIO
    from docx import Document
    from pypdf import PdfReader
    from report.pdf_report import build_pdf
    from report.docx_report import build_docx

    _, run = make_run(CASES[0])
    if legacy:
        run['rows'][0].pop('type_hint')
        run['rows'][0]['event_type'] = 'trial_results'
    view = build_view(run, [kept_decision(run)], generated_at=AT)
    pdf = PdfReader(BytesIO(build_pdf(view)))
    doc = Document(BytesIO(build_docx(view)))
    texts = [' '.join(page.extract_text() or '' for page in pdf.pages),
             ' '.join([p.text for p in doc.paragraphs] +
                      [c.text for t in doc.tables for r in t.rows for c in r.cells])]
    for text in texts:
        text = ' '.join(text.split())
        if legacy:
            assert 'Category (from old keyword rule): trial_results' in text
            assert "Reader's proposed type: Not supplied" in text
        else:
            assert "Reader's proposed type: earnings release" in text
        assert 'Kept by you' in text
