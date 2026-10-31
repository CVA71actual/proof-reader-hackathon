"""Job 38: every model call has a timeout and one retry; a silent service never hangs a run or kills the job.

No test reaches the network: the services are fakes that raise what a silent service raises."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from capture import http, read_events, sam
from capture.reader import LiveReader, Reply, timeout_reply
from capture.second_reader import SecondReader
from server.app import create_app
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond, events_answer, fake_events

CHOICES = {"companies": ["PTGX", "IONS"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
           "spending_limit_usd": None, "note_for_agent": None}
PTGX_SID = "src-ptgx-8k-2026-08-05-ex991"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    return slept


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def start(runs_dir, first, second=None, **choices):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=first, second_reader=second,
                                   background=False))
    r = client.post("/api/runs/new", json={**CHOICES, **choices})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def run_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())


def job_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "job.json").read_text())


class Silent(Exception):
    pass


# ---------------------------------------------------------------- the helper


def test_the_constants_are_the_ordered_ones():
    assert (http.CONNECT_TIMEOUT_S, http.READ_TIMEOUT_S, http.VOICE_TIMEOUT_S, http.RETRY_DELAY_S) == (20, 120, 60, 5)
    t = http.timeout()
    assert (t.connect, t.read) == (20, 120)


def test_the_client_has_the_timeouts_and_no_library_retries():
    c = http.openai_client("https://example.invalid/v1", "k")
    assert (c.timeout.connect, c.timeout.read) == (20, 120) and c.max_retries == 0


def test_one_retry_after_five_seconds_then_the_error(no_sleep):
    calls = []

    def fn():
        calls.append(1)
        raise httpx.ReadTimeout("silent")

    with pytest.raises(httpx.ReadTimeout):
        http.call_with_retry(fn)
    assert len(calls) == 2 and no_sleep == [5]


def test_a_second_try_that_answers_is_the_answer(no_sleep):
    seen = []

    def fn():
        seen.append(1)
        if len(seen) == 1:
            raise httpx.ConnectError("down")
        return "ok"

    assert http.call_with_retry(fn) == "ok" and no_sleep == [5]


def test_another_error_is_not_retried(no_sleep):
    with pytest.raises(ValueError):
        http.call_with_retry(lambda: (_ for _ in ()).throw(ValueError("no")))
    assert no_sleep == []


class FakeClient:
    def __init__(self, exc):
        self.calls = 0
        outer = self

        class Completions:
            def create(self, **kw):
                outer.calls += 1
                raise exc

        self.chat = type("Chat", (), {"completions": Completions()})()
        self.responses = type("Resp", (), {"create": lambda s, **kw: Completions().create(**kw)})()


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("x"), httpx.ConnectError("x"), httpx.ConnectTimeout("x")])
def test_each_reader_says_the_plain_reason_after_two_tries(monkeypatch, exc):
    client = FakeClient(exc)
    monkeypatch.setattr(http, "openai_client", lambda base, key: client)
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("BASETEN_API_KEY", "k")
    r = LiveReader().call("hello")
    assert r.timed_out and r.error == "The Muse Spark service did not answer in time." and r.raw is None
    assert client.calls == 2
    client.calls = 0
    c = LiveReader().chat([{"role": "user", "content": "hi"}])
    assert c.timed_out and c.error == "The Muse Spark service did not answer in time." and client.calls == 2
    client.calls = 0
    s = SecondReader().call("hello")
    assert s.timed_out and s.error == "The Muse Spark Open service did not answer in time." and client.calls == 2
    client.calls = 0
    g = sam.SamReader().segment(b"png", "table")
    assert g.timed_out and g.error == "The SAM 3.1 service did not answer in time." and g.images is None
    assert client.calls == 2


# ---------------------------------------------------------------- the job goes on


class FirstSilentOnPtgx(FakeReader):
    def call(self, prompt, image=None):
        if "Protagonist" in prompt:
            self.prompts.append(prompt)
            return timeout_reply("Muse Spark", self.model)
        return super().call(prompt, image)


def test_a_silent_first_reader_sends_the_page_to_a_person_and_the_run_goes_on(runs_dir):
    rid = start(runs_dir, FirstSilentOnPtgx())
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["status"] == "done"
    reason = "The Muse Spark service did not answer in time."
    row = next(r for r in run["rows"] if r["source_id"] == PTGX_SID)
    assert row["status"] == "unreadable" and row["group"] == "open" and row["reason"] == reason
    steps = [t["step"] for t in row["trail"]]
    assert steps[-2:] == ["No answer from the model", "Sent to a person"]
    assert next(t for t in row["trail"] if t["step"] == "No answer from the model")["text"] == reason
    assert len(run["sources"]) == 2 and run["tables"]        # the next page was read
    assert run["run"]["turns_total"] >= 2 and not job.get("page_failures")


def test_a_silent_call_under_a_limit_is_a_counted_turn_with_unknown_cost(runs_dir):
    rid = start(runs_dir, FirstSilentOnPtgx(), companies=["PTGX"], spending_limit_usd=5)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["turns_total"] == 1 and job["unpriced_calls"] == 1
    assert any(r["status"] == "unreadable" for r in run["rows"])


class SecondSilent(FakeSecond):
    def call(self, prompt, image=None):
        self.prompts.append(prompt)
        return timeout_reply("Muse Spark Open", self.model, extra={"route": self.route})


def test_a_silent_second_reader_does_not_lose_the_first_readers_rows(runs_dir):
    ok = start(runs_dir, FakeReader(), FakeSecond(answer=events_answer(fake_events())), companies=["PTGX"])
    good_rows = run_of(runs_dir, ok)["rows"]
    rid = start(runs_dir, FakeReader(), SecondSilent(), companies=["PTGX"])
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["status"] == "done"
    first_rows = [r for r in run["rows"] if r["proposed_by"] == good_rows[0]["proposed_by"]]
    assert first_rows and all(r["status"] != "unreadable" for r in first_rows)
    sec = job["second"][PTGX_SID]
    assert sec["status"] == "unreadable" and sec["reason"] == "The Muse Spark Open service did not answer in time."
    assert run["run"]["second_reader"]["status"] == "unreadable"
    assert len(run["rows"]) <= len(good_rows) and not job.get("page_failures")


def test_the_reply_default_is_not_a_timeout():
    assert Reply("x", "m").timed_out is False


# ---------------------------------------------------------------- Job 38b: Codex's three items on PR #116

from tests import test_scan  # noqa: E402
from tests.test_second_reader import OPENROUTER_MODEL  # noqa: E402


class SamSilentOnTable(test_scan.FakeSam):
    def segment(self, image, concept):
        if concept == "table":
            self.calls.append((concept, image))
            return timeout_reply("SAM 3.1", "sam-3.1")
        return super().segment(image, concept)


def test_a_silent_sam_call_is_unknown_cost_not_zero_and_unpriced_under_a_limit(runs_dir):
    client, rid = test_scan.start(runs_dir, FakeReader(), SamSilentOnTable(test_scan.TWO_BOXES), spending_limit_usd=5)
    job, run = job_of(runs_dir, rid), run_of(runs_dir, rid)
    assert job["unpriced_calls"] == 1 and run["run"]["turns_total"] == 1
    assert run["run"]["spend_usd"] in (0, 0.0, None)


class SamRetriedOnTable(test_scan.FakeSam):
    def segment(self, image, concept):
        r = super().segment(image, concept)
        if concept == "table":
            r.extra = {"retried_after_timeout": True}
        return r


def test_a_sam_retry_is_charged_the_same_image_cost_again(runs_dir):
    from runs import prices
    client, rid = test_scan.start(runs_dir, FakeReader(), SamRetriedOnTable(test_scan.TWO_BOXES), spending_limit_usd=5)
    job, run = job_of(runs_dir, rid), run_of(runs_dir, rid)
    one = prices.image_cost_usd("SAM 3.1", 1)
    assert job["unpriced_calls"] == 0 and job["retries_charged"] == 1
    assert one and job["retry_worst_case_usd"] == one == run["run"]["retry_worst_case_usd"]


def test_a_silent_sam_call_with_no_limit_counts_no_unpriced_call(runs_dir):
    client, rid = test_scan.start(runs_dir, FakeReader(), SamSilentOnTable(test_scan.TWO_BOXES))
    assert job_of(runs_dir, rid)["unpriced_calls"] == 0


class BasetenSilentOpenRouterAnswers(SecondReader):
    def _ask(self, route, prompt, image=None):
        self.routes.append(route)
        if route == "baseten":
            return timeout_reply("Muse Spark Open", "baseten-m", extra={"route": "baseten"})
        return Reply(json.dumps({"events": []}), OPENROUTER_MODEL, prompt_tokens=100, completion_tokens=50,
                     extra={"route": "openrouter"})


@pytest.fixture
def both_keys(monkeypatch, tmp_path):
    from capture import read_table
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    monkeypatch.setenv("BASETEN_API_KEY", "bt")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")


def test_a_silent_baseten_turn_is_counted_before_the_openrouter_fallback(runs_dir, both_keys):
    second, first = BasetenSilentOpenRouterAnswers(), FakeReader()
    second.routes = []
    rid = start(runs_dir, first, second, companies=["PTGX"], spending_limit_usd=50)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert second.routes == ["baseten", "openrouter"]
    assert len(first.prompts) == 1 and run["run"]["turns_total"] == 3      # first reader + silent Baseten + OpenRouter
    assert job["unpriced_calls"] == 1                                       # the silent Baseten turn, unknown cost
    assert job["second"][PTGX_SID]["status"] == "read"


def test_the_fallback_is_skipped_when_the_budget_gate_refuses(runs_dir, both_keys, monkeypatch):
    from server import jobs
    real, n = jobs.reserve_budget, []

    def reserve(*a, **k):
        n.append(1)
        return None if len(n) == 3 else real(*a, **k)      # 1 first reader, 2 Baseten, 3 the fallback

    monkeypatch.setattr(jobs, "reserve_budget", reserve)
    second, first = BasetenSilentOpenRouterAnswers(), FakeReader()
    second.routes = []
    rid = start(runs_dir, first, second, companies=["PTGX"], spending_limit_usd=50)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert second.routes == ["baseten"] and len(n) >= 3
    assert run["run"]["turns_total"] == 2 and job["unpriced_calls"] == 1       # the silent turn is counted once
    sec = job["second"][PTGX_SID]
    assert sec["status"] == "unreadable" and sec["reason"] == "The Muse Spark Open service did not answer in time."


def test_the_fallback_without_a_limit_is_made_and_both_turns_are_counted(runs_dir, both_keys):
    second = BasetenSilentOpenRouterAnswers()
    second.routes = []
    rid = start(runs_dir, FakeReader(), second, companies=["PTGX"])
    run = run_of(runs_dir, rid)
    assert second.routes == ["baseten", "openrouter"]
    assert run["run"]["turns_total"] == 3 and job_of(runs_dir, rid)["unpriced_calls"] == 0


# ---- the CLI readers and voice


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("x"), httpx.ConnectError("x")])
def test_the_table_cli_saves_an_unreadable_record_on_a_silent_service(monkeypatch, exc):
    from capture import read_table
    client = FakeClient(exc)
    monkeypatch.setattr(http, "openai_client", lambda base, key: client)
    monkeypatch.setenv("MODEL_API_KEY", "k")
    rec = read_table.read_live(read_table.load_words())
    assert rec["readable"] is False and rec["parse_problem"] == "The Muse Spark service did not answer in time."
    assert "APITimeoutError" not in json.dumps(rec) and client.calls == 2


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("x"), httpx.ConnectError("x")])
def test_the_events_cli_saves_an_unreadable_record_on_a_silent_service(monkeypatch, exc):
    from capture.capture_source import load_entry
    from capture import read_table
    client = FakeClient(exc)
    monkeypatch.setattr(http, "openai_client", lambda base, key: client)
    monkeypatch.setenv("MODEL_API_KEY", "k")
    rec = read_events.read_live(read_table.load_words(), load_entry(read_events.PIN))
    assert rec["readable"] is False and rec["parse_problem"] == "The Muse Spark service did not answer in time."
    assert rec["call_failed"] is True and client.calls == 2


def test_voice_uses_the_shared_helper_one_retry_and_the_plain_reason(monkeypatch):
    from capture import voice
    from tests.test_r29_voice import wav
    seen = []

    def post(url, headers, files, timeout):
        seen.append(timeout)
        raise httpx.ReadTimeout("x")

    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setattr(voice.httpx, "post", post)
    with pytest.raises(voice.VoiceError) as e:
        voice.transcribe(wav())
    assert str(e.value) == "The Muse Voice Transcribe service did not answer in time. The attempt may have been billed."
    assert e.value.billed_attempts == 2
    assert seen == [http.VOICE_TIMEOUT_S, http.VOICE_TIMEOUT_S] == [60, 60]


def _voice_post(monkeypatch, first_exc):
    from capture import voice
    calls = []

    class R:
        status_code = 200

        def json(self):
            return {"transcript": "ok", "audioDurationMs": 3000}

    def post(url, headers, files, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise first_exc
        return R()

    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setattr(voice.httpx, "post", post)
    return calls


def test_voice_connect_failure_then_success_is_a_single_cost(monkeypatch):
    from capture import voice
    from runs import prices
    from tests.test_r29_voice import wav
    calls = _voice_post(monkeypatch, httpx.ConnectTimeout("x"))
    got = voice.transcribe(wav())
    assert got["transcript"] == "ok" and len(calls) == 2
    assert got["attempts"] == 2 and got["billed_attempts"] == 1
    assert got["cost_usd"] == prices.audio_cost_usd(voice.NAME, 3000)


def test_voice_read_timeout_then_success_is_a_doubled_cost(monkeypatch):
    from capture import voice
    from runs import prices
    from tests.test_r29_voice import wav
    calls = _voice_post(monkeypatch, httpx.ReadTimeout("x"))
    got = voice.transcribe(wav())
    assert len(calls) == 2 and got["attempts"] == 2 and got["billed_attempts"] == 2
    assert got["cost_usd"] == round(2 * prices.audio_cost_usd(voice.NAME, 3000), prices.ROUND_TO)


def test_may_have_been_billed_and_on_retry():
    assert http.may_have_been_billed(httpx.ReadTimeout("x"))
    assert not http.may_have_been_billed(httpx.ConnectTimeout("x"))
    assert not http.may_have_been_billed(httpx.ConnectError("x"))
    assert not http.may_have_been_billed(ValueError("x"))
    seen, n = [], []

    def fn():
        n.append(1)
        if len(n) == 1:
            raise httpx.ReadTimeout("x")
        return "ok"

    assert http.call_with_retry(fn, on_retry=lambda e, b: seen.append(b)) == "ok" and seen == [True]


class FlakyThenAnswers(FakeReader):
    """FakeReader whose first try per call goes through the real retry helper and fails with `first`."""

    def __init__(self, first, **kw):
        super().__init__(**kw)
        self.first = first

    def call(self, prompt, image=None):
        from capture.reader import retried
        http.take_billed()
        n = []

        def attempt():
            n.append(1)
            if len(n) == 1:
                raise self.first
            return super(FlakyThenAnswers, self).call(prompt, image)

        reply = http.call_with_retry(attempt)
        reply.extra = retried(reply.extra)
        return reply


SENTENCE = ("One call to {m} was answered only on a second try; the first try may have been billed, "
            "so its cost is unknown.")


def test_a_read_timeout_then_success_under_a_limit_is_charged_at_the_worst_case_and_the_run_goes_on(runs_dir):
    reader = FlakyThenAnswers(httpx.ReadTimeout("x"))
    rid = start(runs_dir, reader, companies=["PTGX"], spending_limit_usd=5)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unpriced_calls"] == 0 and job["retried_after_timeout"] == 1 and job["retries_charged"] == 1
    assert run["run"]["status"] == "done"
    bound = job["retry_worst_case_usd"]
    assert bound > 0 and run["run"]["retry_worst_case_usd"] == bound
    assert run["run"]["spend_usd"] >= bound            # the lost try is a real cost inside the spend


def test_the_bound_is_the_same_prompt_with_the_longest_answer():
    from runs import prices
    assert prices.cost_usd("Muse Spark", 1000, http.MAX_COMPLETION_TOKENS) > prices.cost_usd("Muse Spark", 1000, 500)


def test_a_retry_with_no_bound_is_the_unpriced_stop_as_before(runs_dir):
    class NoTokens(FlakyThenAnswers):
        def call(self, prompt, image=None):
            r = super().call(prompt, image)
            r.prompt_tokens = None
            return r

    reader = NoTokens(httpx.ReadTimeout("x"))
    rid = start(runs_dir, reader, companies=["PTGX"], spending_limit_usd=5)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unpriced_calls"] == 1 and not job.get("retries_charged")
    assert run["run"]["status"] == "needs_input"
    assert run["run"]["status_reason"].endswith(SENTENCE.format(m=reader.model))


def test_a_retry_that_lands_the_charge_over_the_limit_stops_the_run_cleanly(runs_dir):
    reader = FlakyThenAnswers(httpx.ReadTimeout("x"))
    probe = start(runs_dir, FlakyThenAnswers(httpx.ReadTimeout("x")), companies=["PTGX"], spending_limit_usd=5)
    bound = job_of(runs_dir, probe)["retry_worst_case_usd"]
    rid = start(runs_dir, reader, companies=["PTGX", "IONS"], spending_limit_usd=bound)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["status"] == "needs_input" and job["unpriced_calls"] == 0
    assert "spending limit" in run["run"]["status_reason"]
    assert run["run"]["turns_total"] == 1                # nothing more was sent after the double charge


def test_every_text_call_asks_for_the_completion_cap():
    seen = []

    class Comp:
        def create(self, **kw):
            seen.append(kw)
            raise ValueError("stop")

    class C:
        chat = type("X", (), {"completions": Comp()})()

    from capture import reader as rd, read_table
    for fn in (lambda: rd.create(C(), "m", [], True), lambda: read_table._call(C(), [], True)):
        with pytest.raises(ValueError):
            fn()
    assert [k["max_tokens"] for k in seen] == [http.MAX_COMPLETION_TOKENS] * 2


def test_a_connect_failure_then_success_is_free_and_the_run_goes_on(runs_dir):
    rid = start(runs_dir, FlakyThenAnswers(httpx.ConnectError("x")), companies=["PTGX"], spending_limit_usd=5)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unpriced_calls"] == 0 and not job.get("retried_after_timeout")
    assert run["run"]["status"] == "done"


def test_a_read_timeout_then_success_with_no_limit_is_only_a_job_note(runs_dir):
    rid = start(runs_dir, FlakyThenAnswers(httpx.ReadTimeout("x")), companies=["PTGX"])
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["status"] == "done" and job["unpriced_calls"] == 0 and job["retried_after_timeout"] >= 1


# ---------------------------------------------------------------- Job 38d: a failed retry keeps the possible charge


def test_timeout_reply_carries_the_final_failure_class():
    http.take_billed()
    assert timeout_reply("X", "m").extra["may_have_been_billed"] is True                       # unknown: assume billed
    assert timeout_reply("X", "m", exc=httpx.ReadTimeout("x")).extra["may_have_been_billed"] is True
    assert timeout_reply("X", "m", exc=httpx.ConnectError("x"), extra={"route": "b"}).extra == {
        "route": "b", "may_have_been_billed": False}


def _always_fail(exc_first, exc_second):
    class Client:
        calls = 0
    n = []

    def create(**kw):
        n.append(1)
        raise exc_first if len(n) == 1 else exc_second
    return create, n


def test_the_live_reader_marks_the_last_failure_class(monkeypatch):
    from capture import read_table
    for first, second, want in ((httpx.ReadTimeout("x"), httpx.ConnectError("x"), True),
                                (httpx.ConnectError("x"), httpx.ReadTimeout("x"), True)):
        create, n = _always_fail(first, second)

        class C:
            chat = type("c", (), {"completions": type("k", (), {"create": staticmethod(create)})})
        monkeypatch.setattr(http, "openai_client", lambda *a, **k: C)
        monkeypatch.setenv("MODEL_API_KEY", "k")
        monkeypatch.setattr(read_table, "load_env_file", lambda: None)
        r = LiveReader().call("p")
        assert r.timed_out and r.extra["may_have_been_billed"] is want and len(n) == 2


class SilentPtgx(FakeReader):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def call(self, prompt, image=None):
        if "Protagonist" in prompt:
            self.prompts.append(prompt)
            return timeout_reply("Muse Spark", self.model, exc=self.exc)
        return super().call(prompt, image)


def test_connect_only_exhaustion_is_free_counts_the_turn_and_the_run_goes_on(runs_dir):
    rid = start(runs_dir, SilentPtgx(httpx.ConnectError("x")), spending_limit_usd=5)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unpriced_calls"] == 0 and run["run"]["status"] == "done" and run["run"]["turns_total"] >= 2
    row = next(r for r in run["rows"] if r["source_id"] == PTGX_SID)
    assert row["status"] == "unreadable"


def test_read_timeout_exhaustion_under_a_limit_is_one_unpriced_call(runs_dir):
    rid = start(runs_dir, SilentPtgx(httpx.ReadTimeout("x")), spending_limit_usd=5)
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unpriced_calls"] == 1
    assert next(r for r in run["rows"] if r["source_id"] == PTGX_SID)["status"] == "unreadable"


@pytest.fixture
def sample_dir(tmp_path):
    import shutil
    from tests.test_run_controls import RUNS
    d = tmp_path / "runs"
    d.mkdir()
    shutil.copytree(RUNS / "sample", d / "sample")
    return d


def _voice_client(runs_dir, monkeypatch, excs):
    from capture import voice
    from tests.test_r29_voice import wav
    seq = list(excs)

    def post(url, headers, files, timeout):
        if seq:
            raise seq.pop(0)

        class R:
            status_code = 200

            def json(self):
                return {"transcript": "keep it", "audioDurationMs": 1000}
        return R()

    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setattr(voice.httpx, "post", post)
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=None, background=False))
    row = json.loads((runs_dir / "sample" / "run.json").read_text())["rows"][0]["id"]
    f = runs_dir / "sample" / "run.json"
    doc = json.loads(f.read_text())
    doc["run"]["choices"]["spending_limit_usd"] = 5
    f.write_text(json.dumps(doc))
    return lambda: client.post(f"/api/runs/sample/voice/decide?target={row}", content=wav(1),
                               headers={"content-type": "audio/wav"})


def test_voice_read_timeout_twice_keeps_the_possible_charge_and_holds_the_next_call(sample_dir, monkeypatch):
    from server import jobs
    jobs._RESERVED.clear()
    post = _voice_client(sample_dir, monkeypatch, [httpx.ReadTimeout("x"), httpx.ReadTimeout("x")])
    r = post()
    assert r.status_code == 422 and "did not answer" in r.json()["detail"] and "may have been billed" in r.json()["detail"]
    job = job_of(sample_dir, "sample")
    assert job["unpriced_calls"] == 1 and "Muse Voice Transcribe" in job["unpriced_models"]
    assert jobs._RESERVED == {}
    assert post().status_code == 409                       # a later paid call is held


def test_voice_connect_error_twice_records_nothing_and_the_next_call_goes(sample_dir, monkeypatch):
    from server import jobs
    jobs._RESERVED.clear()
    post = _voice_client(sample_dir, monkeypatch, [httpx.ConnectError("x"), httpx.ConnectError("x")])
    r = post()
    assert r.status_code == 422 and "did not answer" in r.json()["detail"] and "billed" not in r.json()["detail"]
    jf = sample_dir / "sample" / "job.json"
    assert not jf.exists() or not json.loads(jf.read_text()).get("unpriced_calls")
    assert post().status_code == 200


def test_voice_one_read_timeout_then_connect_error_is_one_billed_attempt(monkeypatch):
    from capture import voice
    from tests.test_r29_voice import wav
    monkeypatch.setenv("MODEL_API_KEY", "k")
    seq = [httpx.ReadTimeout("x"), httpx.ConnectError("x")]

    def post(url, headers, files, timeout):
        raise seq.pop(0)
    monkeypatch.setattr(voice.httpx, "post", post)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    with pytest.raises(voice.VoiceError) as e:
        voice.transcribe(wav())
    assert e.value.billed_attempts == 1


# ---- Job 38e: any possibly-sent attempt is billable

def _mixed_call():
    """The real retry helper over a ReadTimeout then a ConnectError, ending in the reply a reader builds."""
    seq = [httpx.ReadTimeout("x"), httpx.ConnectError("x")]

    def fn():
        raise seq.pop(0)
    http.take_billed()
    try:
        http.call_with_retry(fn)
    except Exception as e:
        return timeout_reply("Muse Spark", "m", exc=e)


def test_mixed_retry_read_timeout_then_connect_error_is_billable_and_the_reverse_is_not():
    assert _mixed_call().extra["may_have_been_billed"] is True
    seq = [httpx.ConnectError("x"), httpx.ConnectError("x")]

    def fn():
        raise seq.pop(0)
    http.take_billed()
    try:
        http.call_with_retry(fn)
    except Exception as e:
        assert timeout_reply("X", "m", exc=e).extra["may_have_been_billed"] is False


class MixedPtgx(FakeReader):
    def call(self, prompt, image=None):
        if "Protagonist" in prompt:
            self.prompts.append(prompt)
            return _mixed_call()
        return super().call(prompt, image)


def test_mixed_sequence_under_a_limit_is_one_unpriced_call_at_job_level(runs_dir):
    rid = start(runs_dir, MixedPtgx(), spending_limit_usd=5)
    assert job_of(runs_dir, rid)["unpriced_calls"] == 1


class BasetenMixedOpenRouterAnswers(BasetenSilentOpenRouterAnswers):
    def _ask(self, route, prompt, image=None):
        if route == "baseten":
            self.routes.append(route)
            return _mixed_call()
        return super()._ask(route, prompt, image)


def test_mixed_baseten_turn_is_unpriced_and_gates_the_openrouter_fallback(runs_dir, both_keys, monkeypatch):
    from server import jobs
    real, n = jobs.reserve_budget, []

    def reserve(*a, **k):
        n.append(1)
        return None if len(n) == 3 else real(*a, **k)

    monkeypatch.setattr(jobs, "reserve_budget", reserve)
    second = BasetenMixedOpenRouterAnswers()
    second.routes = []
    rid = start(runs_dir, FakeReader(), second, companies=["PTGX"], spending_limit_usd=50)
    job = job_of(runs_dir, rid)
    assert second.routes == ["baseten"] and job["unpriced_calls"] == 1       # counted, fallback refused at the gate


# ---- voice: the first attempt's possible charge survives every later failure

def _voice_after_read_timeout(monkeypatch, second):
    from capture import voice
    from tests.test_r29_voice import wav
    seq = [httpx.ReadTimeout("x")]

    def post(url, headers, files, timeout):
        if seq:
            raise seq.pop(0)
        return second

    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setattr(voice.httpx, "post", post)
    with pytest.raises(voice.VoiceError) as e:
        voice.transcribe(wav())
    return e.value


class _Resp:
    def __init__(self, status=200, doc=None, bad=False):
        self.status_code, self.doc, self.bad = status, doc, bad

    def json(self):
        if self.bad:
            raise ValueError("no json")
        return self.doc


_VOICE_FAILS = [_Resp(500), _Resp(200, bad=True), _Resp(200, {"transcript": "x", "audioDurationMs": -5}),
                _Resp(200, {"transcript": "x", "audioDurationMs": 10**9}), _Resp(200, {"nope": 1})]


@pytest.mark.parametrize("resp", _VOICE_FAILS)
def test_voice_failure_after_a_read_timeout_keeps_billed_attempts(monkeypatch, resp):
    assert _voice_after_read_timeout(monkeypatch, resp).billed_attempts >= 1


@pytest.mark.parametrize("resp", _VOICE_FAILS)
def test_voice_route_failure_after_a_read_timeout_records_unknown_spend(sample_dir, monkeypatch, resp):
    from capture import voice
    from server import jobs
    jobs._RESERVED.clear()
    post = _voice_client(sample_dir, monkeypatch, [httpx.ReadTimeout("x")])
    monkeypatch.setattr(voice.httpx, "post", (lambda seq: lambda *a, **k: (_ for _ in ()).throw(seq.pop(0))
                                              if seq else resp)([httpx.ReadTimeout("x")]))
    assert post().status_code == 422
    assert job_of(sample_dir, "sample")["unpriced_calls"] == 1 and jobs._RESERVED == {}


def test_voice_release_and_marker_are_atomic_for_a_polling_worker(sample_dir, monkeypatch):
    import threading
    from server import jobs
    jobs._RESERVED.clear()
    token = jobs.reserve_voice(sample_dir, "sample", 5, 1.0)
    assert token is not None
    stop, bad = threading.Event(), []

    def worker():
        while not stop.is_set():
            with jobs.RUN_LOCK:
                held = bool(jobs._RESERVED.get("sample"))
                jf = sample_dir / "sample" / "job.json"
                marked = jf.is_file() and json.loads(jf.read_text()).get("unpriced_calls", 0) > 0
                if not held and not marked:
                    bad.append(1)          # the gate would pass

    real = jobs.record_voice_unpriced

    def slow(*a, **k):
        import time
        time.sleep(0.05)
        real(*a, **k)

    monkeypatch.setattr(jobs, "record_voice_unpriced", slow)
    t = threading.Thread(target=worker)
    t.start()
    import time
    time.sleep(0.02)
    jobs.fail_voice(sample_dir, "sample", token, 1, True)
    stop.set()
    t.join()
    assert not bad
