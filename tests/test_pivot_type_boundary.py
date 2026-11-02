"""Interpretation is a reader proposal, never a keyword-derived code verdict."""
import copy

import pytest

from checks import event_checks
from runs.compare import _uncheckable_row, differences
from runs.proposal import reader_event_type
from runs.state import current_state
from tests.test_pivot_proposal_flow import AT, make_run, kept_decision, decision


CASES = [
    ('earnings', 'Cedar Bio will release financial results on November 12, 2026.', 'earnings'),
    ('conference', "Management will host a conference call to discuss financial results on November 12, 2026.", 'conference'),
    ('manufacturing qualification', 'Qualification of the new manufacturing line on November 12, 2026 would remove the supply constraint for its commercial launch.', 'unknown'),
    ('planned submission', 'The board will discuss the submission budget on November 12, 2026.', 'unknown'),
]


@pytest.mark.parametrize('hint,quote,category', CASES)
def test_reader_hint_survives_without_keyword_self_certification(hint, quote, category, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Keyword classification must not judge a reader proposal')
    monkeypatch.setattr(event_checks, 'event_type_for', forbidden)
    case = ('type-boundary', quote, hint, 'November 12, 2026', '2026-11-12', [])
    proposal, run = make_run(case)
    row = run['rows'][0]
    assert row['type_hint'] == hint and row['event_type'] == category
    check = next(c for c in row['checks'] if c['name'] == 'type_match')
    assert check['result'] == 'not_needed'
    assert 'review' in check['reason'].lower()
    state = current_state(run, [])['targets']['e1']
    assert not state['accepted'] and state['needs_attention']
    assert current_state(run, [kept_decision(run)])['targets']['e1']['accepted']
    assert not current_state(run, [kept_decision(run), decision('undo')])['targets']['e1']['accepted']
    missing_page = _uncheckable_row(1, proposal, None, {'prompt_text': 'Synthetic'}, (None, None, 1), AT)
    assert missing_page['event_type'] == category and missing_page['type_hint'] == hint


def test_second_read_compares_reader_hints_never_classifier_outputs(monkeypatch):
    proposal, run = make_run(('type-boundary', CASES[0][1], 'earnings', 'November 12, 2026', '2026-11-12', []))
    second = copy.deepcopy(proposal)
    second['type_hint'] = 'trial_results'
    monkeypatch.setattr(event_checks, 'event_type_for', lambda *a, **k: pytest.fail('classifier was called'))
    found = differences(run['rows'][0], second)
    assert found == [{'field': 'type_hint', 'first_reader': 'earnings', 'second_reader': 'trial_results'}]


def test_raw_legacy_category_is_preserved_but_explicit_unknown_is_not_backfilled():
    assert reader_event_type({'event_type': 'conference'}) == 'conference'
    assert reader_event_type({'type_hint': None, 'event_type': 'conference'}) == 'unknown'
    assert reader_event_type({'type_hint': '  Trial Results  '}) == 'trial_results'
    assert reader_event_type({'type_hint': 'Phase 3 start', 'quote': 'financial results'}) == 'unknown'


def test_keyword_type_tool_is_retired_and_cannot_feed_a_pass_back_to_reader(monkeypatch):
    from capture.reader_tools import TOOLS, ToolBox
    assert {t['function']['name'] for t in TOOLS} == {'quote_on_page', 'date_as_written'}
    monkeypatch.setattr(event_checks, 'event_type_for', lambda *a, **k: pytest.fail('classifier was called'))
    answer = ToolBox([], '2026-11-01').run_tool('event_type_for_words', {'quote': CASES[0][1]})
    assert answer['result'] == 'could_not_run' and answer['found'] is None
