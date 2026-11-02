"""A printed timing sentence cannot erase the event or establish its relationship."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from server.jobs import Job
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run, AT, kept_decision

EVENT = 'CB-100 initiation depends on successful validation.'
TIMING = 'Initiation is expected in Q1 2027.'
CASE = ('connected', EVENT + ' ' + TIMING, 'conditional initiation', 'Q1 2027', '2027-01-01', ['depends on successful validation'])


def apply(tmp_path, *, page=1, source_id='synthetic-source'):
    raw, run = make_run(CASE, tmp_path/'synthetic', event_updates={'quote': EVENT})
    words = json.loads((tmp_path/'synthetic/synthetic.words.json').read_text())
    source = copy.deepcopy(run['sources'][0]);source['id'] = source_id
    words['page_number'] = page
    row=run['rows'][0]; original=copy.deepcopy(row)
    before=kept_decision(run)
    answer={'id':'e1','quote':TIMING,'date_text':'Q1 2027'}
    raw_answer=copy.deepcopy(answer)
    job=Job.__new__(Job);job.run=run
    job.apply_send_back(row,answer,{'model':'Muse Spark','prompt_text':'Synthetic timing request','usage':{'prompt_tokens':10,'completion_tokens':10},'fake':True},
                        source,words,(10,10,1),'Synthetic timing request',AT)
    return run, row, original, answer, raw_answer, before


def test_different_printed_timing_passage_keeps_original_event_and_qualifications(tmp_path):
    run,row,original,answer,raw_answer,before=apply(tmp_path)
    assert row['quote']==EVENT and row['conditions']==['depends on successful validation']
    for key in ('title','type_hint','relevance','subject_company','relation','date_text','date','date_end','box','checks','status','group'):
        assert row[key]==original[key], key
    assert row['timing_proposal']['proposal']==raw_answer
    assert row['timing_proposal']['source_id']==row['source_id']
    assert row['timing_proposal']['page_number']==row['page_number']
    assert 'does not establish their relationship' in row['trail'][-1]['text']
    assert not run['mistakes']
    assert not current_state(run,[])['targets']['e1']['on_calendar']
    # Newly presented evidence invalidates acceptance, even while original fields survive.
    assert not current_state(run,[before])['targets']['e1']['on_calendar']
    answer['quote']='mutated outside the row'
    assert row['timing_proposal']['proposal']==raw_answer
    schema=json.loads((Path(__file__).parents[1]/'runs/schema/run.schema.json').read_text())
    assert not list(Draft202012Validator({'$ref':'#/$defs/row','$defs':schema['$defs']}).iter_errors(row))


@pytest.mark.parametrize('page,source_id',[(2,'synthetic-source'),(1,'other-source')])
def test_timing_answer_on_different_page_or_source_cannot_repair_row(tmp_path,page,source_id):
    run,row,original,_,_,_=apply(tmp_path,page=page,source_id=source_id)
    assert row['quote']==original['quote'] and row['checks']==original['checks']
    assert not run['mistakes']
    assert 'different source or page' in row['trail'][-1]['text']
