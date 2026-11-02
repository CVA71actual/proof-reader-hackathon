"""PIVOT RED 3: one physical request is one auditable attempt.

A 400/422 is never retried automatically (no JSON-mode-off retry). The failed
attempt is returned with its status and may_have_been_billed=True (an HTTP
status alone is not a no-charge signal; only 401/403 are pre-inference
refusals). Any further request is a separate attempt that passes the
ordinary Job gate first. Raw provider bodies are never recorded.
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from capture import http, read_table
from capture.reader import LiveReader
from capture.second_reader import SecondReader, BASETEN_BASE_URL, OPENROUTER_BASE_URL
from capture.wonder import WonderReader

BODY = 'SECRET-PROVIDER-BODY'


def _status_error(code):
    return APIStatusError(BODY, response=httpx.Response(code, request=httpx.Request(
        'POST', 'https://example.invalid')), body={'error': BODY})


class Spy:
    """Request 1 fails with `code`; any later request would succeed."""

    def __init__(self, code):
        self.code, self.requests = code, []

    def client(self, base, key):
        def create(**kw):
            self.requests.append((base, kw))
            if len(self.requests) == 1:
                raise _status_error(self.code)
            msg = SimpleNamespace(content='{"events": []}', tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], model='m',
                                   usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(read_table, 'ENV_FILE', tmp_path / 'absent')
    for name in ('MODEL_API_KEY', 'BASETEN_API_KEY', 'OPENROUTER_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _install(env, code):
    spy = Spy(code)
    env.setattr(http, 'openai_client', spy.client)
    return spy


def _assert_one_failed_attempt(spy, reply, code):
    assert len(spy.requests) == 1, spy.requests
    assert reply.error and reply.raw is None and not reply.timed_out
    assert reply.extra['http_status'] == code
    assert reply.extra['may_have_been_billed'] is True
    assert reply.extra['billing_classification_reason']
    assert BODY not in json.dumps(reply.extra) and BODY not in reply.error


@pytest.mark.parametrize('code', [400, 422])
@pytest.mark.parametrize('cls', [LiveReader, WonderReader])
def test_first_reader_call_makes_one_request(env, cls, code):
    env.setenv('MODEL_API_KEY', 'synthetic')
    spy = _install(env, code)
    reply = cls().call('synthetic prompt')
    _assert_one_failed_attempt(spy, reply, code)
    assert reply.model == cls.model
    assert spy.requests[0][1].get('response_format') == {'type': 'json_object'}


@pytest.mark.parametrize('code', [400, 422])
def test_tools_rejection_makes_one_request_and_is_possibly_billed(env, code):
    env.setenv('MODEL_API_KEY', 'synthetic')
    spy = _install(env, code)
    reply = LiveReader().chat([{'role': 'user', 'content': 'x'}], tools=[{'type': 'function'}])
    _assert_one_failed_attempt(spy, reply, code)
    assert reply.extra['tools_rejected'] is True


@pytest.mark.parametrize('code', [401, 403])
def test_auth_refusal_rule_is_unchanged(env, code):
    env.setenv('MODEL_API_KEY', 'synthetic')
    spy = _install(env, code)
    reply = LiveReader().call('synthetic prompt')
    assert len(spy.requests) == 1
    assert reply.extra['http_status'] == code and reply.extra['may_have_been_billed'] is False


@pytest.mark.parametrize('code', [400, 422])
@pytest.mark.parametrize('route,key,base', [('baseten', 'BASETEN_API_KEY', BASETEN_BASE_URL),
                                            ('openrouter', 'OPENROUTER_API_KEY', OPENROUTER_BASE_URL)])
def test_second_reader_route_makes_one_request(env, route, key, base, code):
    env.setenv(key, 'synthetic')
    spy = _install(env, code)
    reply = SecondReader().call('synthetic prompt')
    _assert_one_failed_attempt(spy, reply, code)
    assert reply.extra['route'] == route and spy.requests[0][0] == base


@pytest.mark.parametrize('code', [400, 422])
def test_fallback_is_attempt_two_only_through_the_gate(env, code):
    env.setenv('BASETEN_API_KEY', 'synthetic')
    env.setenv('OPENROUTER_API_KEY', 'synthetic')
    spy = _install(env, code)
    seen = []
    reader = SecondReader()
    reader.before_fallback = lambda reply: seen.append((len(spy.requests), reply)) or False
    reply = reader.call('synthetic prompt')
    _assert_one_failed_attempt(spy, reply, code)
    assert len(seen) == 1 and reply.extra['fallback_skipped']
    # Admitted by the gate: exactly one more request, after the gate saw attempt 1.
    spy2 = _install(env, code)
    order = []
    reader = SecondReader()
    reader.before_fallback = lambda reply: order.append(('gate', len(spy2.requests))) or True
    reply = reader.call('synthetic prompt')
    assert order == [('gate', 1)]
    assert [b for b, _ in spy2.requests] == [BASETEN_BASE_URL, OPENROUTER_BASE_URL]
    assert reply.extra['route'] == 'openrouter' and not reply.error


# ---------------------------------------------------------------- through the Job

from tests.test_r38_timeouts import start, run_of, job_of


@pytest.mark.parametrize('limit', [None, 50.0])
@pytest.mark.parametrize('code', [400, 422])
def test_job_records_one_unpriced_failed_attempt_and_applies_nothing(env, tmp_path, code, limit):
    from server import jobs
    env.setenv('MODEL_API_KEY', 'synthetic')
    spy = _install(env, code)
    released = []
    real_release = jobs.release_budget
    env.setattr(jobs, 'release_budget', lambda rid, held: (released.append(held) if held is not None else None,
                                                          real_release(rid, held))[1])
    runs = tmp_path / 'runs'
    runs.mkdir()
    rid = start(runs, LiveReader(), None, companies=['PTGX'], spending_limit_usd=limit)
    run, job = run_of(runs, rid), job_of(runs, rid)
    assert len(spy.requests) == 1
    assert run['run']['turns_total'] == 1 and not run['rows']
    assert run['run']['status'] in ('failed', 'needs_input')
    assert job['unpriced_calls'] == 1
    notes = [line for line in job['log'] if line.get('http_status') == code]
    assert len(notes) == 1
    assert notes[0]['model'] and notes[0]['route'] and 'billed' in notes[0]['note']
    assert BODY not in json.dumps(job) and BODY not in json.dumps(run)
    if limit is not None:
        assert len(released) == 1
    # Reload: the single failed attempt is preserved, nothing replayed.
    assert job_of(runs, rid) == job and len(spy.requests) == 1


@pytest.mark.parametrize('code', [400, 422])
def test_tools_rejection_has_no_fresh_request_without_the_gate(env, tmp_path, code):
    from server import jobs
    from tests.test_r26_reader_tools import PTGX_ONLY
    from fastapi.testclient import TestClient
    from server.app import create_app
    env.setenv('MODEL_API_KEY', 'synthetic')
    spy = _install(env, code)
    order = []
    real_gate = jobs.Job.stopped_before_a_call

    def gate(self, reader=None):
        order.append(('gate', len(spy.requests)))
        return real_gate(self, reader)
    env.setattr(jobs.Job, 'stopped_before_a_call', gate)
    runs = tmp_path / 'runs'
    runs.mkdir()
    client = TestClient(create_app(runs_dir=runs, ui_dir=runs / 'no-ui', reader=LiveReader(),
                                   second_reader=None, wonder_reader=None, sam_reader=None, background=False))
    r = client.post('/api/runs/new', json={**PTGX_ONLY, 'reader_tools': True})
    assert r.status_code == 202, r.text
    rid = r.json()['id']
    job = job_of(runs, rid)
    assert job['unpriced_calls'] == 1
    # Every physical request was preceded by its own gate check.
    gated = [n for kind, n in order]
    for i in range(len(spy.requests)):
        assert i in gated, (order, len(spy.requests))
    assert spy.requests[0][1].get('tools')
    # The possibly-billed rejection holds the run for the person's say on unknown cost.
    assert len(spy.requests) == 1
    assert run_of(runs, rid)['run']['status'] == 'needs_input'


# ---------------------------------------------------------------- the CLI/helper readers (no Job ledger)

def _assert_one_failed_record(spy, rec, code):
    assert len(spy.requests) == 1, spy.requests
    assert spy.requests[0][1].get('response_format') == {'type': 'json_object'}
    assert rec['readable'] is False and rec['call_failed'] is True
    assert rec['http_status'] == code and rec['may_have_been_billed'] is True
    assert rec['billing_classification_reason']
    assert BODY not in json.dumps(rec)


@pytest.mark.parametrize('code', [400, 422])
def test_table_cli_makes_one_request_and_records_the_attempt(env, code):
    env.setenv('MODEL_API_KEY', 'synthetic')
    spy = _install(env, code)
    rec = read_table.read_live(read_table.load_words())
    _assert_one_failed_record(spy, rec, code)


@pytest.mark.parametrize('code', [400, 422])
def test_events_cli_makes_one_request_and_records_the_attempt(env, code):
    from capture import read_events
    from capture.capture_source import load_entry
    env.setenv('MODEL_API_KEY', 'synthetic')
    spy = _install(env, code)
    rec = read_events.read_live(read_table.load_words(), load_entry(read_events.PIN))
    _assert_one_failed_record(spy, rec, code)


@pytest.mark.parametrize('code', [400, 422])
def test_default_second_reader_without_a_gate_never_falls_back(env, code):
    # PIVOT RED 3b: both keys, no callback installed -> exactly one request.
    env.setenv('BASETEN_API_KEY', 'synthetic')
    env.setenv('OPENROUTER_API_KEY', 'synthetic')
    spy = _install(env, code)
    reply = SecondReader().call('synthetic prompt')
    _assert_one_failed_attempt(spy, reply, code)
    assert [b for b, _ in spy.requests] == [BASETEN_BASE_URL]
    assert reply.extra['route'] == 'baseten' and reply.extra['fallback_skipped'] is True
