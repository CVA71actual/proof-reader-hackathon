"""Date checks compare date text and bounds, not event meaning or publication."""
from datetime import date
from types import SimpleNamespace

import pytest

from checks.event_checks import date_match
from runs.calendar import run_interval_partition
from runs.recheck_dates import exclude_again
from runs.state import current_state
from server.jobs import Job, past_reason
from tests.test_pivot_proposal_flow import make_run, kept_decision, AT


@pytest.mark.parametrize('quote,expected', [
    ('In Q1 2027, Cedar Bio plans to initiate CB-100.', 'pass'),
    ('Cedar Bio plans to initiate CB-100, and this is expected in Q1 2027.', 'pass'),
    ('Cedar Bio plans to initiate CB-100. Its unrelated office lease ends in Q1 2027.', 'pass'),
    ('CB-100 initiation in Q1 2027 is no longer expected.', 'pass'),
    ('CB-100 initiation is expected in Q1 2027 if validation succeeds.', 'pass'),
    ('CB-100 initiation moves from Q1 2027 to Q3 2027.', 'could_not_run'),
])
def test_printed_date_can_match_without_accepting_the_claim(quote, expected):
    _, run = make_run(('date-boundary', quote, 'study initiation', 'Q1 2027', '2027-01-01', []))
    check = next(c for c in run['rows'][0]['checks'] if c['name'] == 'date_match')
    # DATE-LITERAL-001/H2: joined range endpoints are not standalone periods.
    assert check['result'] == expected
    if expected == 'pass':
        assert 'date bounds only' in check['reason']
    else:
        assert 'range' in check['reason']
    target = current_state(run, [])['targets']['e1']
    assert not target['accepted'] and target['inclusion'] != 'kept'
    if expected != 'pass':      # CALENDAR-RULE-001: a date the check cannot support never lands on the calendar
        assert target['needs_attention'] and not target['on_calendar']
    assert target['values']['quote'] == quote


@pytest.mark.parametrize('wording,start,end,quote', [
    ('Q1 2027', '2027-04-01', '2027-06-30', 'Results in Q1 2027.'),
    ('2027', '2027-01-01', '2027-12-31', 'Results in Q1 2027.'),
    ('Q1 2027', '2027-01-01', '2027-03-31', 'Results in Q3 2027.'),
    ('November 12, 2026', '2026-11-13', None, 'Results on November 12, 2026.'),
])
def test_wrong_or_absent_literal_dates_still_fail(wording, start, end, quote):
    assert date_match(wording, start, quote, date_end=end).result == 'fail'


def test_unaccepted_historical_date_is_not_excluded_and_keep_places_it_in_past():
    quote = 'CB-100 initiation is planned. Its unrelated office lease ended on October 12, 2026.'
    _, run = make_run(('past-boundary', quote, 'study initiation', 'October 12, 2026', '2026-10-12', []))
    row = run['rows'][0]
    assert past_reason(row, date(2026, 11, 1), run['sources'][0]) is None
    # Invoke the live runner path without a model or a background job.
    context = SimpleNamespace(meta=run['run'], run=run)
    Job.apply_window(context, [row], AT, source=run['sources'][0])
    assert row['group'] != 'removed' and run['exclusions'] == []
    assert 'relationship needs your review' in row['past_exclusion_proposal']
    assert exclude_again(run, set(), AT) == {'out': []}
    state = current_state(run, [])
    # CALENDAR-RULE-001: unaccepted and clean, it is Context (past), never on the calendar and never kept
    assert state['targets']['e1']['inclusion'] == 'context' and not state['targets']['e1']['accepted']
    assert not run_interval_partition(run, state)[0]['calendar']
    accepted = current_state(run, [kept_decision(run)])
    assert [r['id'] for r in run_interval_partition(run, accepted)[0]['past']] == ['e1']
