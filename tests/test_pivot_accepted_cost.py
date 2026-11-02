from tests.test_pivot_proposal_flow import make_run, CASES, kept_decision, decision
from runs.state import current_state

def test_cost_per_kept_row_excludes_fix_awaiting_keep():
    _, run = make_run(CASES[1])
    run['run']['spend_usd'] = 1.0
    edit = {**decision('fix'), 'fix': {'field': 'title', 'from': run['rows'][0]['title'], 'to': 'Edited title'}}
    state = current_state(run, [edit])
    assert state['targets']['e1']['semantic_status'] == 'unresolved'
    assert state['cost_per_kept_row_usd'] is None
    keep = kept_decision(run, [edit])
    assert current_state(run, [edit, keep])['cost_per_kept_row_usd'] == 1.0
    assert current_state(run, [edit, keep, decision('undo')])['cost_per_kept_row_usd'] is None

def test_accepted_cost_returns_unknown_when_basis_expires():
    _, run = make_run(CASES[1])
    run['run']['spend_usd'] = 1.0
    keep = kept_decision(run)
    assert current_state(run, [keep])['cost_per_kept_row_usd'] == 1.0
    run['rows'][0]['conditions'] = []
    assert current_state(run, [keep])['cost_per_kept_row_usd'] is None
