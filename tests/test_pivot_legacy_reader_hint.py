"""Legacy raw reader hints are distinct from code-derived saved row types."""
import copy
import json

import pytest

from runs.proposal import context_fields, reader_context_fields, reader_context_steps
from runs import build_entry, compare
from tests.test_pivot_proposal_flow import make_run, CASES, AT


def test_raw_legacy_hint_preserved_without_promoting_saved_classification():
    raw = {'event_type': 'manufacturing qualification', 'conditions': []}
    before = copy.deepcopy(raw)
    assert reader_context_fields(raw)['type_hint'] == 'manufacturing qualification'
    assert context_fields(raw)['type_hint'] is None
    assert raw == before
    step = reader_context_steps(raw, AT)[0]
    assert step['tag'] == 'Code' and step['step'] == 'Legacy reader type'
    assert repr(raw['event_type']) in step['text']


@pytest.mark.parametrize('hint', [None, '', 17, [], 'explicit reader interpretation'])
def test_explicit_hint_never_falls_back_to_legacy_field(hint):
    raw = {'event_type': 'launch', 'type_hint': hint}
    actual = reader_context_fields(raw)['type_hint']
    assert actual == ('explicit reader interpretation' if hint == 'explicit reader interpretation' else None)
    assert not any(t['step'] == 'Legacy reader type' for t in reader_context_steps(raw, AT))


@pytest.mark.parametrize('legacy', [None, '', '  ', 17, [], {}])
def test_malformed_legacy_value_stays_unknown(legacy):
    assert reader_context_fields({'event_type': legacy})['type_hint'] is None
    assert not reader_context_steps({'event_type': legacy}, AT)


def test_legacy_hint_preserved_at_primary_and_second_reader_boundaries(tmp_path):
    proposal, run = make_run(CASES[1], tmp_path / 'source')
    raw = {k: v for k, v in proposal.items() if k != 'type_hint'}
    raw['event_type'] = 'conditional clinical readout'
    words = json.loads((tmp_path / 'source/synthetic.words.json').read_text())
    primary = build_entry.build_row(2, raw, {'fake': False, 'prompt_text': 'Synthetic'},
                                    run['sources'][0], words, AT)
    secondary = compare._uncheckable_row(3, raw, None, {'prompt_text': 'Synthetic'},
                                         (None, None, 1), AT)
    for row in (primary, secondary):
        assert row['type_hint'] == raw['event_type']
        assert row['conditions'] == proposal['conditions']
        assert any(t['step'] == 'Legacy reader type' for t in row['trail'])
    assert 'type_hint' not in raw
    assert 'type hint conditional clinical readout' in compare._model_step(raw, (None, None, 1), AT)['text']


def test_timing_send_back_does_not_invent_a_legacy_reader_hint(tmp_path):
    from tests.test_r12_fixwave import _start, _reader, _q1_quote
    calls = []
    run, _ = _start(tmp_path, _reader(lambda i: {
        'id': i, 'quote': _q1_quote(), 'date_text': 'Q1 2027'}, calls))
    step = next(t for row in run['rows'] for t in row['trail'] if t['step'] == 'Sentence for the date')
    assert 'type hint none' in step['text']
    assert calls == ['read', 'send-back']
