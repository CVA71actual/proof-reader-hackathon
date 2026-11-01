"""Offline check-32 additions: only temporary saved-run copies are used."""
import copy
import pytest
from tests.test_failed_period_projection import setup_run
from report.data import load_run_dir
from runs.state import current_state


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('stale_recheck', [False, True])
@pytest.mark.parametrize('keep', [False, True])
def test_pending_or_stale_recheck_cannot_borrow_derived_period(tmp_path, explicit, stale_recheck, keep):
    target = setup_run(tmp_path, explicit=explicit)
    run, _ = load_run_dir(tmp_path / 'ptgx-r4a')
    original = current_state(run, [])['targets'][target]
    assert original['values']['date'] == '2027-01-01'
    row = next(r for r in run['rows'] if r['id'] == target)
    decisions = [{'target': target, 'action': 'fix', 'by': 'person',
                  'fix': {'field': 'date_text', 'from': 'Q1 2027', 'to': 'Q2 2027'}}]
    if stale_recheck:
        # Reuse the original Q1 checks as if they belonged to the new Q2 value.
        checks = copy.deepcopy(row['checks'])
        decisions.append({'target': target, 'action': 'recheck', 'by': 'code',
                          'recheck': {'result': 'pass', 'checks': checks,
                                      'after': {'checks': checks}}})
    if keep:
        decisions.append({'target': target, 'action': 'keep', 'by': 'person'})
    view = current_state(run, decisions)
    current = view['targets'][target]
    assert current['values']['date_text'] == 'Q2 2027'
    assert current['values']['date'] == ('2027-01-01' if explicit else None)
    assert current['values']['date_end'] == ('2027-03-31' if explicit else None)
    assert current['needs_recheck'] is True
    assert current['effective_checks'] == []
    assert len(view['ignored']) == int(stale_recheck)
    if stale_recheck:
        assert 'stale' in view['ignored'][0]['reason']
