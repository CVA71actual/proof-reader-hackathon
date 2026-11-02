"""A failed primary attempt cannot bypass the job's one admission gate."""
import json
import pytest
from capture import read_table
from capture.reader import Reply
from capture.second_reader import SecondReader, BASETEN_MODEL, OPENROUTER_MODEL
from tests.test_r38_timeouts import start, run_of, job_of
from tests.test_run_controls import FakeReader

@pytest.fixture
def keys(monkeypatch, tmp_path):
    monkeypatch.setattr(read_table, 'ENV_FILE', tmp_path / 'absent')
    monkeypatch.setenv('BASETEN_API_KEY', 'synthetic-primary')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-fallback')

class ErrorThenAnswer(SecondReader):
    def __init__(self, known_unbilled=False):
        super().__init__()
        self.routes=[]
        self.known_unbilled=known_unbilled
    def _ask(self, route, prompt, image=None):
        self.routes.append(route)
        if route=='baseten':
            extra={'route':route}
            if self.known_unbilled:
                extra['may_have_been_billed']=False
            return Reply(None, BASETEN_MODEL, error='Primary request failed (status 503).', extra=extra)
        return Reply(json.dumps({'events':[]}), OPENROUTER_MODEL,
                     prompt_tokens=100, completion_tokens=50, extra={'route':route})

@pytest.mark.parametrize('known_unbilled',[False,True])
def test_non_timeout_error_obeys_refusing_callback(keys,known_unbilled):
    reader=ErrorThenAnswer(known_unbilled);seen=[]
    def gate(reply):
        seen.append(reply)
        return False
    reader.before_fallback=gate
    reply=reader.call('synthetic')
    assert reader.routes==['baseten']
    assert len(seen)==1 and reply.error and not reply.timed_out
    assert reply.extra['already_counted'] and reply.extra['fallback_skipped']

@pytest.mark.parametrize('limit',[None,50.0])
@pytest.mark.parametrize('known_unbilled',[False,True])
def test_actual_job_gate_accounts_for_primary_before_fallback(tmp_path,keys,limit,known_unbilled):
    runs=tmp_path/'runs';runs.mkdir()
    second=ErrorThenAnswer(known_unbilled)
    rid=start(runs,FakeReader(),second,companies=['PTGX'],spending_limit_usd=limit)
    run,job=run_of(runs,rid),job_of(runs,rid)
    if known_unbilled:
        assert second.routes==['baseten','openrouter']
        assert job['unpriced_calls']==0
        assert run['run']['turns_total']==3
    else:
        assert second.routes==['baseten']
        assert job['unpriced_calls']==1
        assert run['run']['turns_total']==2
        assert run['run']['status']=='needs_input'


from tests.test_r27_picture import picture_index


def test_picture_page_refusal_keeps_pause_without_page_failure(tmp_path, keys, picture_index):
    from tests.test_r27_picture import run, FakeSam, BOXES
    second = ErrorThenAnswer()
    result, directory, first = run(tmp_path, picture_index, FakeSam(BOXES), second)
    job = json.loads((directory / 'job.json').read_text())
    assert second.routes == ['baseten']
    assert result['run']['status'] == 'needs_input'
    assert 'cost' in result['run']['status_reason'].lower()
    assert job['unpriced_calls'] == 1
    assert not job.get('page_failures') and not job.get('skipped')
    assert not result['run'].get('skipped_pages')
    assert first.prompts == []


@pytest.mark.parametrize('status', [401, 403, 400, 404, 413, 422, 429, 503, None])
def test_http_exception_passes_through_real_ask_into_gate(keys, monkeypatch, status):
    import httpx
    from openai import APIStatusError
    from capture import http
    from tests.test_r38_timeouts import FakeClient
    exc = (APIStatusError('synthetic', response=httpx.Response(status, request=httpx.Request(
        'POST', 'https://example.invalid')), body=None) if status else ValueError('synthetic'))
    client = FakeClient(exc)
    monkeypatch.setattr(http, 'openai_client', lambda *a: client)
    seen = []
    reader = SecondReader()
    reader.before_fallback = lambda reply: seen.append(reply) or False
    reply = reader.call('synthetic')
    assert len(seen) == 1 and reply.extra['fallback_skipped']
    # PIVOT RED 3 (Operator ruling item 32): no automatic 400/422 retry; one request per attempt.
    assert client.calls == 1
    assert reply.extra['may_have_been_billed'] is (status not in (401, 403))
    assert reply.extra['billing_classification_reason']


def test_auth_failure_after_possibly_billed_retry_stays_unknown(keys, monkeypatch):
    import httpx
    from openai import APIStatusError
    from capture import http
    from types import SimpleNamespace
    calls = []
    def create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise httpx.ReadTimeout('synthetic')
        raise APIStatusError('synthetic', response=httpx.Response(401, request=httpx.Request(
            'POST', 'https://example.invalid')), body=None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(http, 'openai_client', lambda *a: client)
    monkeypatch.setattr(http.time, 'sleep', lambda *a: None)
    reader = SecondReader()
    reader.before_fallback = lambda reply: False
    reply = reader.call('synthetic')
    assert len(calls) == 2
    assert reply.extra['may_have_been_billed'] and reply.extra['fallback_skipped']


def test_auth_classification_survives_successful_fallback_in_job_log(tmp_path, keys, monkeypatch):
    import httpx
    from openai import APIStatusError
    from capture import http
    from types import SimpleNamespace
    from tests.test_r38_timeouts import FakeClient
    refusal = FakeClient(APIStatusError('synthetic', response=httpx.Response(
        403, request=httpx.Request('POST', 'https://example.invalid')), body=None))
    answer = SimpleNamespace(model=OPENROUTER_MODEL,
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"events":[]}'))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))
    fallback = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: answer)))
    monkeypatch.setattr(http, 'openai_client', lambda base, key: refusal if 'baseten' in base else fallback)
    runs = tmp_path/'runs'; runs.mkdir()
    rid = start(runs, FakeReader(), SecondReader(), companies=['PTGX'])
    job = job_of(runs, rid)
    notes = [line for line in job['log'] if 'HTTP 403' in line.get('note','')]
    assert len(notes) == 1
    assert 'before inference' in notes[0]['note']
    assert job['unpriced_calls'] == 0
    assert run_of(runs, rid)['run']['turns_total'] == 3
