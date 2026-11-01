"""Cross-path contracts. All filesystem changes use pytest tmp_path. No live models."""
import json
from pathlib import Path
import pytest
from capture.reader import Reply
from server import jobs
from tests.test_run_controls import FakeReader,PTGX_ONLY

@pytest.mark.parametrize('may_be_billed',[False,True])
def test_fallback_uses_the_same_unknown_cost_rule_as_normal_gate(tmp_path,may_be_billed):
    choices=jobs.validate_choices({**PTGX_ONLY,'spending_limit_usd':1.0})
    run=jobs.create_run(tmp_path,choices,run_id='probe')
    job=jobs.Job(tmp_path/'probe',FakeReader())
    reply=Reply(None,job.reader.model,error='no answer',timed_out=True,extra={'may_have_been_billed':may_be_billed})
    allowed=job._before_fallback(reply)
    jobs.release_budget(job.dir.name,job._held);job._held=None
    ordinary_stops=job._checks_before_a_call(job.reader)
    if may_be_billed:
        assert job.job['unpriced_calls']==1
        assert ordinary_stops is True
        assert allowed is False,'fallback admitted after unknown billed cost while ordinary gate refuses'
    else:
        assert job.job['unpriced_calls']==0
        assert allowed is True and ordinary_stops is False


RUNS=Path(__file__).resolve().parent.parent/'runs'

def _runs():
    return sorted(d for d in RUNS.iterdir() if (d/'run.json').exists())

@pytest.mark.parametrize('run_dir',_runs(),ids=lambda d:d.name)
def test_spend_is_the_priced_trail_plus_counted_unknowns(run_dir):
    """(a) run.json spend_usd equals the last priced snapshot in job.json's log (the priced trail).
    (b) a run over its limit either stopped right after ONE priced call that crossed the limit alone (the gate's
    alone_may_cross rule: the last priced step goes from below the limit to above it, status needs_input with the limit
    reason), or carries a counted unknown (unpriced_calls / retry_worst_case_usd).
    runs/sample has no job.json, so no trail is kept there and only the number's shape is checked (reason: no trail)."""
    run=json.loads((run_dir/'run.json').read_text())['run']
    spend=run.get('spend_usd')
    assert spend is None or (isinstance(spend,(int,float)) and spend>=0)
    jf=run_dir/'job.json'
    if not jf.exists():
        return
    job=json.loads(jf.read_text())
    trail=[e['spend_usd'] for e in job.get('log',[]) if e.get('spend_usd') is not None]
    if trail:
        assert round(spend or 0,6)==round(trail[-1],6),(spend,trail[-1])
    limit=(run.get('choices') or {}).get('spending_limit_usd')
    if limit is None or (spend or 0)<=limit:
        return
    if job.get('unpriced_calls') or job.get('retry_worst_case_usd'):
        return
    steps=[]            # the distinct priced totals, in order
    for t in trail:
        if not steps or round(t,6)!=round(steps[-1],6):
            steps.append(t)
    assert len(steps)>=2 and steps[-2]<=limit<steps[-1],'over the limit, not an alone-crossing priced call, and no counted unknown'
    assert run.get('status')=='needs_input' and (run.get('status_reason') or '').startswith('Stopped at the spending limit'),'the run did not stop right after the crossing'
