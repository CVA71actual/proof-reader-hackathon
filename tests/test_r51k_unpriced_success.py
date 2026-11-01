"""Offline API probes. No network; only temporary run directories are written."""
import json
import pytest
from capture.reader import Reply
from tests.test_run_controls import FakeReader, make, start, run_json
from tests.test_cross_path_contracts import test_spend_is_the_priced_trail_plus_counted_unknowns as saved_invariant

class AnswerWithoutPrice(FakeReader):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
    def call(self, prompt, image=None):
        reply = super().call(prompt, image)
        if self.mode == 'missing_usage':
            return Reply(reply.raw, reply.model)
        return Reply(reply.raw, 'unknown-returned-model', prompt_tokens=100, completion_tokens=10)

@pytest.mark.parametrize('mode', ['missing_usage', 'unknown_returned_model'])
@pytest.mark.parametrize('limit', [None, 1.0])
def test_successful_unpriced_reply_stops_before_next_dispatch(tmp_path, mode, limit):
    client, reader = make(tmp_path, AnswerWithoutPrice(mode))
    rid = start(client, companies=['PTGX', 'IONS'], spending_limit_usd=limit)['id']
    meta = run_json(tmp_path, rid)['run']
    job = json.loads((tmp_path / rid / 'job.json').read_text())
    observed = {'status': meta['status'], 'calls': len(reader.prompts),
                'unpriced_calls': job['unpriced_calls'], 'spend_usd': meta['spend_usd']}
    print(mode, limit, observed)
    assert job['unpriced_calls'] == 1 and meta['status'] == 'needs_input', observed
    assert len(reader.prompts) == 1, observed

def test_priced_crossing_is_not_an_unknown_cost(tmp_path):
    client, reader = make(tmp_path, FakeReader(cost=0.3))
    rid = start(client, companies=['PTGX','IONS'], spending_limit_usd=0.5)['id']
    meta = run_json(tmp_path, rid)['run']
    job = json.loads((tmp_path / rid / 'job.json').read_text())
    assert meta['status'] == 'needs_input' and meta['spend_usd'] == pytest.approx(0.6)
    assert len(reader.prompts) == 2 and job['unpriced_calls'] == 0
    saved_invariant(tmp_path / rid)
