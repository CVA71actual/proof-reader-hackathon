"""GROUP-AXIS-001: every acceptance consumer uses the same derived review view."""
import copy

from report.data import build_view
from runs.calendar import calendar_entries
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run, CASES, AT, decision, kept_decision


def assert_consumers(run, log, expected):
    state = current_state(run, log)
    target = state['targets']['e1']
    view = build_view(run, log, generated_at=AT)
    assert target['review_bucket'] == expected
    assert state['counts']['review'] == {k: int(k == expected) for k in ('accepted', 'proposed', 'excluded')}
    assert state['kept_rows'] == int(expected == 'accepted')
    assert state['unresolved'] == int(expected == 'proposed')
    assert target['accepted'] == target['on_calendar'] == (expected == 'accepted')
    assert target['needs_attention'] == (expected == 'proposed')
    assert view['counts'] == state['counts']['review']
    if expected != 'accepted':
        assert all(row.get('override_reason') is None for row in view['open_rows'] + view['edits'])
    assert len(calendar_entries(run, state)) == int(expected == 'accepted')
    assert len(view['open_rows']) == int(expected == 'proposed')
    projected = view['calendar'] + view['contingent'] + view['past'] + view['later'] + view['undated'] + view['timing_unresolved'] + view['open_rows']
    if expected != 'excluded':
        assert len(projected) == 1
        assert projected[0]['group_label'] == target['review_label']
        # Report renderers receive exactly the same acceptance and decision words as the API.
        assert projected[0]['review_label'] == target['review_label']
        assert projected[0]['decision_label'] == target['decision_label']
        assert projected[0]['person_tag'] == target['decision_label']
    assert view['no_accepted_rows_text'] == ('No rows accepted by you.' if expected != 'accepted' else None)
    assert state['cost_per_kept_row_usd'] == (1.0 if expected == 'accepted' else None)
    return target


def test_one_acceptance_projection_for_fix_keep_revision_undo_remove():
    _, run = make_run(CASES[1])
    run['run']['spend_usd'] = 1.0
    checks = copy.deepcopy(run['rows'][0]['checks'])
    target = assert_consumers(run, [], 'proposed')
    # Mechanical group remains diagnostic; it is not a publication decision.
    assert target['group'] == 'kept' and target['status'] == 'matched'
    assert target['review_label'] == 'Proposed — needs review'
    edit = {**decision('fix'), 'fix': {'field': 'title', 'from': run['rows'][0]['title'], 'to': 'Revised proposed title'}}
    target = assert_consumers(run, [edit], 'proposed')
    assert target['group'] == 'corrected'
    assert target['review_label'] == 'Corrected, awaiting your review'
    assert target['decision_label'] == 'Fixed by you — not yet accepted'
    keep = kept_decision(run, [edit])
    target = assert_consumers(run, [edit, keep], 'accepted')
    assert target['decision_label'] == 'Kept by you'
    assert_consumers(run, [edit, keep, decision('undo')], 'proposed')
    run['rows'][0]['conditions'] = []
    target = assert_consumers(run, [edit, keep], 'proposed')
    assert target['decision_label'] == 'Previous Keep — needs your review'
    assert target['review_label'] == 'Previous Keep — needs your review'
    fresh = kept_decision(run, [edit, keep])
    target = assert_consumers(run, [edit, keep, fresh], 'accepted')
    assert target['review_history'] and target['decision_label'] == 'Kept by you'
    assert 'history' not in target['review_label']
    assert_consumers(run, [edit, keep, fresh, decision('undo')], 'proposed')
    assert_consumers(run, [edit, keep, fresh, decision('remove')], 'excluded')
    assert run['rows'][0]['checks'] == checks


def test_rebound_ptgx_reason_does_not_present_historical_checks_as_current():
    from pathlib import Path
    from runs.check_load import load_bound_run
    import json

    path = Path(__file__).resolve().parents[1] / 'runs/ptgx-r5'
    saved = json.loads((path / 'run.json').read_text())
    run = load_bound_run(path)
    state = current_state(run, [])
    for row in run['rows']:
        target = state['targets'][row['id']]
        assert row['reason'] == next(r['reason'] for r in saved['rows'] if r['id'] == row['id'])
        assert row['check_history']  # original verdicts remain inspectable history
        assert 'Date read as written' not in target['reason']
        assert 'Event type matches the quote' not in target['reason']
        assert 'current mechanical checks' in target['reason']
        assert target['needs_attention'] and not target['accepted']
