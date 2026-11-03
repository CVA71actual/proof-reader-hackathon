"""An explicit human clear must not resurrect the original model proposal in an export."""
import pytest

from report.data import build_view
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run, CASES, AT, decision


@pytest.mark.parametrize('field,report_key', [
    ('quote','quote'), ('date_text','date_text'), ('program','program'),
    ('indication','indication'), ('ticker','ticker'), ('company','company'), ('title','event'),
])
@pytest.mark.parametrize('cleared', [None, ''])
def test_open_report_preserves_explicit_clear_in_effective_proposal(field, report_key, cleared):
    _, run = make_run(CASES[1], event_updates={'indication':'Synthetic indication'})
    row = run['rows'][0]
    log = [{**decision('fix'), 'fix':{'field':field,'from':row[field],'to':cleared}}]
    target = current_state(run, log)['targets']['e1']
    assert target['values'][field] == cleared
    assert target['needs_attention'] and not target['accepted']
    view = build_view(run, log, generated_at=AT)
    shown = view['open_rows'][0]
    assert shown[report_key] == '', 'Report must not silently restore the original model value'
    assert shown['decision_label'] == 'Fixed by you — not yet accepted'
    assert not view['calendar']
    assert row[field], 'The original raw proposal remains available as history'


def test_api_clear_keep_and_undo_preserve_the_same_effective_quote(tmp_path):
    from fastapi.testclient import TestClient
    from report.data import load_run_dir
    from server.app import create_app
    directory=tmp_path/'synthetic'
    _, run=make_run(CASES[1], directory)
    original=run['rows'][0]['quote']
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root='/api/runs/synthetic'
        response=client.post(root+'/decisions',json={'target':'e1','action':'fix','fix':{
            'field':'quote','from':original,'to':None}})
        assert response.status_code==200, response.text
        target=response.json()['state']
        assert target['values']['quote'] is None
        loaded, decisions=load_run_dir(directory)
        view=build_view(loaded,decisions,generated_at=AT)
        assert view['open_rows'][0]['quote']==''
        assert view['open_rows'][0]['decision_label']==target['decision_label']
        kept=client.post(root+'/decisions',json={'target':'e1','action':'keep'})
        assert kept.status_code==200,kept.text
        loaded,decisions=load_run_dir(directory)
        view=build_view(loaded,decisions,generated_at=AT)
        accepted=sum((view[k] for k in ('calendar','past','later','contingent','undated','timing_unresolved')),[])
        assert accepted[0]['quote']==''
        assert accepted[0]['decision_label']=='Kept by you'
        # Undo the Keep then the edit: only the explicit Undo restores the original quote.
        assert client.post(root+'/decisions',json={'target':'e1','action':'undo'}).status_code==200
        loaded,decisions=load_run_dir(directory)
        assert build_view(loaded,decisions,generated_at=AT)['open_rows'][0]['quote']==''
        assert client.post(root+'/decisions',json={'target':'e1','action':'undo'}).status_code==200
        loaded,decisions=load_run_dir(directory)
        # CALENDAR-RULE-001: back to the untouched proposal, it is wherever the rule places it (review or placed)
        restored=build_view(loaded,decisions,generated_at=AT)
        rows=sum((restored[k] for k in ('open_rows','calendar','past','later','contingent','undated','timing_unresolved')),[])
        assert [r['quote'] for r in rows]==[original]
