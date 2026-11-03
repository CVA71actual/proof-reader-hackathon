"""Bounded mechanical repair controls. Synthetic fixtures; no model calls."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from runs import escalate
from runs.calendar import calendar_entries
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run, CASES, AT, kept_decision


def setup(tmp_path, updates=None):
    _, run = make_run(CASES[1], tmp_path / 'source',
                      event_updates=updates or {'date': '2027-02-01'}, reading_fake=False)
    row = run['rows'][0]
    words = json.loads((tmp_path / 'source/synthetic.words.json').read_text())
    answer = {key: row[key] for key in ('id', 'source_id', 'page_number', 'quote', 'date_text', 'date_end')}
    answer['date'] = '2027-01-01'
    return run, row, words, answer


def apply(run, row, words, answer, **kwargs):
    reading = {'raw_response': json.dumps({'events': [answer]}), 'events': [copy.deepcopy(answer)]}
    original_reading = copy.deepcopy(reading)
    outcome = escalate.apply(row, answer, reading, run['sources'][0], words, AT,
                             kwargs.get('trigger', escalate.trigger(row)), run, (None, None, 1), 'Synthetic')
    assert reading == original_reading
    return outcome


def test_narrow_patch_keeps_context_and_original_evidence_but_expires_keep(tmp_path):
    run, row, words, answer = setup(tmp_path)
    original = copy.deepcopy(row)
    keep = kept_decision(run)
    assert current_state(run, [keep])['targets']['e1']['on_calendar']
    assert apply(run, row, words, answer) == 'replaced'
    assert row['date'] == '2027-01-01'
    for field in escalate.FIELDS:
        if field not in ('date_text', 'date', 'date_end'):
            assert row[field] == original[field]
    assert len(run['mistakes']) == 1
    assert row['escalation_proposal']['proposal'] == answer
    assert 'conditions' not in row['escalation_proposal']['proposal']
    state = current_state(run, [keep])
    assert state['targets']['e1']['needs_attention']
    assert not calendar_entries(run, state)
    assert not calendar_entries(run, current_state(run, []))
    fresh = kept_decision(run, [keep])
    assert calendar_entries(run, current_state(run, [keep, fresh]))
    # A repeat cannot turn the same repaired failure into another mistake.
    assert apply(run, row, words, answer, trigger={'kind':'check','label':'Date period found in quote','why':'old'}) == 'kept'
    assert len(run['mistakes']) == 1
    schema = json.loads((Path(__file__).parents[1] / 'runs/schema/run.schema.json').read_text())
    Draft202012Validator({'$ref':'#/$defs/row', '$defs':schema['$defs']}).validate(row)


@pytest.mark.parametrize('change', [
    {'quote':'Timing is expected in Q1 2027.'},
    {'quote':'CB-100 results are expected in Q1 2027'},
    {'id':'another-row'}, {'source_id':'another-source'}, {'page_number':2},
    {'title':'Another event'}, {'program':'CB-200'}, {'conditions':[]},
    {'conditions':{'bad':'shape'}}, {'type_hint':'approval'},
    {'date':'2027-02-02'}, {'date_text':None},
])
def test_wrong_identity_alternative_meaning_or_failed_patch_never_replaces(tmp_path, change):
    run, row, words, answer = setup(tmp_path)
    original = copy.deepcopy(row)
    answer.update(change)
    assert apply(run, row, words, answer) == 'kept'
    for field in escalate.FIELDS + ('checks','status','group'):
        assert row[field] == original[field], field
    assert not run['mistakes']
    assert row['escalation_proposal']['proposal'] == answer
    assert current_state(run, [])['targets']['e1']['needs_attention']
    schema = json.loads((Path(__file__).parents[1] / 'runs/schema/run.schema.json').read_text())
    Draft202012Validator({'$ref':'#/$defs/row', '$defs':schema['$defs']}).validate(row)


@pytest.mark.parametrize('field', ['date_text','date','date_end'])
def test_missing_requested_field_is_an_incomplete_repair(tmp_path, field):
    run, row, words, answer = setup(tmp_path)
    del answer[field]
    assert apply(run, row, words, answer) == 'kept'
    assert row['date'] == '2027-02-01' and not run['mistakes']
    assert 'all requested date fields' in row['trail'][-1]['text']


def test_stale_failed_check_is_not_a_demonstrated_before_failure(tmp_path):
    run, row, words, answer = setup(tmp_path, {'date':'2027-01-01'})
    next(c for c in row['checks'] if c['name']=='date_match')['result'] = 'fail'
    # A synonymous printed period is not a correction of an already-valid date.
    answer['date_text'] = 'first quarter 2027'
    assert apply(run, row, words, answer) == 'kept'
    assert not run['mistakes']
    assert 'did not change from fail to pass' in row['trail'][-1]['text']


def test_ambiguous_patch_or_quote_containment_does_not_select_first_event():
    row = {'id':'e1', 'quote':'The full event if validation succeeds.'}
    wrong = {'quote':'The full event'}
    right = {'quote':row['quote']}
    assert escalate.pick(row, [wrong]) is None
    assert escalate.pick(row, [right,right]) is None
    assert escalate.pick(row, [right]) is right
    alternate = {'id':'e1','quote':'Timing elsewhere.'}
    assert escalate.pick(row, [alternate]) is alternate  # apply preserves it but refuses replacement


def test_prompt_preserves_target_and_date_wire_contract(tmp_path):
    run, row, words, answer = setup(tmp_path)
    text = escalate.prompt({'filing_date':'2026-11-01'}, escalate.trigger(row), (0,300), words, row=row)
    assert json.dumps({k:row[k] for k in ('id','source_id','page_number','quote','date_text','date','date_end')}) in text
    for phrase in ('YYYY-MM-DD','first day through its last day','left, top, right, bottom',
                   '0 to 1000','Do not return title','original passage','date fields'):
        assert phrase in text


@pytest.mark.parametrize('incomplete', [False, True])
def test_runner_requests_only_target_patch_and_records_one_attempt(tmp_path, incomplete):
    from tests.test_escalation import FakeReader, FakeWonder, wrong_date_events, start, run_of, job_of
    directory = tmp_path / 'runs'
    directory.mkdir()
    def answer(prompt, image, n):
        original = json.loads(prompt.split('Original proposal: ', 1)[1])
        assert original['source_id'] and original['page_number'] == 3
        assert 'Do not return title' in prompt
        original['date'] = '2027-01-01'
        if incomplete:
            original.pop('date_end')
        return [original]
    wonder = FakeWonder(answer=answer)
    first = FakeReader(answer=lambda p,n: json.dumps({'events':wrong_date_events()}))
    client, rid = start(directory, first, None, wonder)
    run = run_of(directory, rid)
    row = next(r for r in run['rows'] if r['date_text']=='Q1 2027')
    assert len(wonder.prompts) == 1
    assert row['date'] == ('2027-02-01' if incomplete else '2027-01-01')
    assert len(run['mistakes']) == (0 if incomplete else 1)
    assert 'type_hint' not in row['escalation_proposal']['proposal']
    assert row['conditions'] is None  # legacy reader, never invented by the patch
    escalated = current_state(run, [])['targets'][row['id']]
    assert not escalated['accepted'] and escalated['inclusion'] != 'kept'   # CALENDAR-RULE-001
    assert job_of(directory, rid)['escalated_rows'][row['id']] == ('kept' if incomplete else 'replaced')
    # Attempt terminality is persisted and the runner does not retry an incomplete answer.
    assert run['run']['status'] == 'done'
    assert client.get(f'/api/runs/{rid}').status_code == 200
    assert len(wonder.prompts) == 1
    schema = json.loads((Path(__file__).parents[1] / 'runs/schema/run.schema.json').read_text())
    Draft202012Validator(schema).validate(run)


def test_legacy_type_or_date_labels_do_not_authorize_paid_escalation():
    from runs import escalate
    for label in ("Event type matches the quote", "Date read as written"):
        row = {"id": "old", "status": "failed", "checks": [{"result": "fail", "label": label}]}
        assert escalate.budgeted([row]) == []
