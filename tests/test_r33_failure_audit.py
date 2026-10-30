"""Job 33: fixes for the failure audit. B1: one per-run budget gate for the run worker and the voice route.
B2: an unreadable row says why the reader stopped. Every reader and transcriber here is a fake: no network."""

import json

import pytest
from fastapi.testclient import TestClient

from capture import reader_tools, voice
from capture.reader import Reply
from runs import prices
from server import jobs
from server.app import create_app
from tests.test_r26_reader_tools import SCHEMA, ToolReader, ask, final, run, steps, tool_call
from tests.test_r29_voice import wav
from tests.test_run_controls import FakeReader, MODEL, PRICE_IN, PTGX_ONLY, make, run_json, runs_dir, start  # noqa: F401


@pytest.fixture(autouse=True)
def clean_gate():
    jobs._RESERVED.clear()
    yield
    jobs._RESERVED.clear()


def voice_client(runs_dir, reader, heard=None):
    calls = []

    def fake(data):
        calls.append(1)
        if heard:
            heard()
        return {"transcript": "keep it", "audio_ms": 1000, "cost_usd": prices.audio_cost_usd(voice.NAME, 1000)}

    return TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader, background=False,
                                 transcriber=fake)), calls


def post_voice(client, rid, target="e1"):
    return client.post(f"/api/runs/{rid}/voice/decide?target={target}", content=wav(1),
                       headers={"content-type": "audio/wav"})


def set_run(runs_dir, rid, spend, limit):
    f = runs_dir / rid / "run.json"
    doc = json.loads(f.read_text())
    doc["run"]["spend_usd"], doc["run"]["choices"]["spending_limit_usd"] = spend, limit
    f.write_text(json.dumps(doc))
    return doc["rows"][0]["id"]


# ------------------------------------------------------------------ B1


def test_a_voice_call_in_flight_holds_the_budget_against_a_model_call(runs_dir):
    row = set_run(runs_dir, "sample", 0.9984, 1.00)
    seen = {}

    def during_voice():        # the model call arrives while the transcription is in flight
        seen["model"] = jobs.reserve_budget("sample", 0.9984, 1.00, jobs.MODEL_CALL_UNBOUNDED, alone_may_cross=True)

    client, calls = voice_client(runs_dir, None, during_voice)
    assert post_voice(client, "sample", row).status_code == 200
    assert seen["model"] is None                               # refused: the voice reservation is held
    assert jobs._RESERVED == {}                                # and released once the voice call was counted
    assert jobs.reserve_budget("sample", 0.99, 1.00, 0.01, alone_may_cross=True) is not None


def test_a_model_call_in_flight_holds_the_budget_against_a_voice_call(runs_dir):
    row = set_run(runs_dir, "sample", 0.9984, 1.00)
    client, calls = voice_client(runs_dir, None)
    token = jobs.reserve_budget("sample", 0.9984, 1.00, jobs.MODEL_CALL_UNBOUNDED, alone_may_cross=True)
    assert token is not None
    r = post_voice(client, "sample", row)
    assert r.status_code == 409 and calls == []                # refused before any transcription is paid for
    assert r.json()["detail"] == "A model call is in progress; try the spoken decision again in a moment."
    assert list(jobs._RESERVED["sample"].values()) == [float("inf")]   # the worker's hold is unbounded; voice added none
    jobs.release_budget("sample", token)
    assert post_voice(client, "sample", row).status_code == 200  # sequential: it works once the call is counted


def test_the_run_worker_holds_its_reservation_during_a_model_call_and_releases_it_when_counted(runs_dir):
    seen = []

    class Reader(FakeReader):
        def call(self, prompt, image=None):
            rid = next(d.name for d in runs_dir.iterdir() if d.name != "sample")
            held = dict(jobs._RESERVED.get(rid, {}))
            seen.append(held)
            # a voice request for the same run arrives now: the worker's hold takes the room it would need
            assert jobs.reserve_budget(rid, 0.0, 0.01, 0.00005) is None
            return super().call(prompt, image)

    client, reader = make(runs_dir, Reader(cost=0.005))
    rid = client.post("/api/runs/new", json={**PTGX_ONLY, "spending_limit_usd": 0.01}).json()["id"]
    assert seen and all(list(h.values()) == [jobs.MODEL_CALL_UNBOUNDED] for h in seen)
    assert jobs._RESERVED == {}
    assert run_json(runs_dir, rid)["run"]["spend_usd"] > 0


def test_sequential_calls_still_work_under_a_limit(runs_dir):
    client, reader = make(runs_dir, FakeReader(cost=0.001))
    rid = start(client, spending_limit_usd=1.0)["id"]
    doc = run_json(runs_dir, rid)
    assert doc["run"]["status"] == "done" and jobs._RESERVED == {}
    assert doc["run"]["spend_usd"] == 0.001 * len(reader.prompts)


def test_the_gate_refuses_when_spend_plus_reservations_pass_the_limit():
    t = jobs.reserve_budget("g", 0.9984, 1.00, 0.00155)
    assert t is not None
    assert jobs.reserve_budget("g", 0.9984, 1.00, 0.00155) is None       # 0.9984 + 0.00155 + 0.00155 > 1.00
    assert jobs.reserve_budget("g", 0.9984, 1.00, 0.01, alone_may_cross=True) is None
    jobs.release_budget("g", t)
    assert jobs.reserve_budget("g", 0.9984, 1.00, 0.01, alone_may_cross=True) is not None   # alone: the old rule
    jobs._RESERVED.clear()


# ------------------------------------------------------------------ B2


def test_model_call_exhaustion_is_not_reported_as_three_refusals(runs_dir):
    cap = reader_tools.MAX_MODEL_CALLS
    bad = Reply("not json at all", MODEL, prompt_tokens=1000, completion_tokens=0)
    reader = ToolReader(*[ask(tool_call("quote_on_page", quote=f"q{i}")) for i in range(cap - 1)], bad)
    doc = run(runs_dir, reader)
    [row] = doc["rows"]
    assert len(reader.sent) == cap
    assert row["status"] == "unreadable"
    assert row["reason"] == (f"The reader used its {cap} model calls on this page without a readable answer; "
                             f"sent to a person.") == "The reader used its 12 model calls on this page without a readable answer; sent to a person."
    assert len(steps(row, "Answer ")) == 1 and steps(row, "Answer ")[0]["step"] == "Answer 1 not understood"
    [capstep] = steps(row, "Model-call cap")
    assert "12" in capstep["step"]
    assert steps(row, "Sent to a person")[0]["text"] == row["reason"]
    SCHEMA.validate(doc)


def test_three_parse_failures_still_say_three_refusals(runs_dir):
    from server.jobs import THREE_REFUSALS
    bad = Reply("nope", MODEL, prompt_tokens=1000, completion_tokens=0)
    doc = run(runs_dir, ToolReader(bad, bad, bad))
    [row] = doc["rows"]
    assert row["reason"] == THREE_REFUSALS and not steps(row, "Model-call cap")
    assert len(steps(row, "Answer ")) == 3


# ------------------------------------------------------------------ 33c: the persisted spend is the truth


def test_voice_spend_between_model_calls_is_never_overwritten_by_the_worker(runs_dir):
    class Reader(FakeReader):
        def call(self, prompt, image=None):
            if not self.prompts:      # a voice call is counted after the worker's check, before it counts this call
                rid = next(d.name for d in runs_dir.iterdir() if d.name != "sample")
                jobs.count_voice_turn(runs_dir, rid, {"audio_ms": 1000, "cost_usd": 0.00005})
            return super().call(prompt, image)

    client, reader = make(runs_dir, Reader(cost=0.99998))
    rid = client.post("/api/runs/new", json={**PTGX_ONLY, "spending_limit_usd": 1.00}).json()["id"]
    doc = run_json(runs_dir, rid)
    assert doc["run"]["spend_usd"] == 1.00003          # the worker's 0.99998 lands on the voice call's 0.00005
    assert doc["run"]["status"] == "needs_input"       # and the limit is seen to be crossed
    assert jobs._RESERVED == {}


def test_a_run_with_no_voice_counts_as_before(runs_dir):
    client, reader = make(runs_dir, FakeReader(cost=0.25))
    rid = client.post("/api/runs/new", json={**PTGX_ONLY, "spending_limit_usd": 1.00}).json()["id"]
    doc = run_json(runs_dir, rid)
    assert doc["run"]["spend_usd"] == 0.25 * len(reader.prompts) and doc["run"]["status"] == "done"


def test_a_stale_worker_budgets_from_the_persisted_spend(runs_dir):
    client, reader = make(runs_dir, FakeReader(cost=0.001))
    rid = start(client, spending_limit_usd=1.0)["id"]
    job = jobs.Job.__new__(jobs.Job)
    doc = run_json(runs_dir, rid)
    doc["run"]["spend_usd"] = 0.9
    (runs_dir / rid / "run.json").write_text(json.dumps(doc))
    stale = json.loads(json.dumps(doc))
    stale["run"]["spend_usd"] = 0.5
    job.dir, job.run, job.meta = runs_dir / rid, stale, stale["run"]
    job.refresh_spend()
    assert job.meta["spend_usd"] == 0.9


# ------------------------------------------------------------------ 33d: every worker save merges what voice persisted


def test_a_later_plain_worker_save_keeps_voice_spend_and_log(runs_dir):
    client, reader = make(runs_dir, FakeReader(cost=0.001))
    rid = start(client, spending_limit_usd=1.0)["id"]
    d = runs_dir / rid
    doc = run_json(runs_dir, rid)
    job = jobs.Job.__new__(jobs.Job)          # the worker's in-memory copy, as of its last count_turn
    job.dir, job.run, job.meta = d, json.loads(json.dumps(doc)), None
    job.meta = job.run["run"]
    job.job = json.loads((d / "job.json").read_text())
    before_spend, before_log = job.meta["spend_usd"], len(job.job["log"])
    jobs.count_voice_turn(runs_dir, rid, {"audio_ms": 1000, "cost_usd": 0.00005})      # voice, after the reservation went
    job.save("a later plain save")
    after = run_json(runs_dir, rid)
    assert after["run"]["spend_usd"] == round(before_spend + 0.00005, 6)
    saved = json.loads((d / "job.json").read_text())
    assert [t["model"] for t in saved["voice_turns"]] == ["Muse Voice Transcribe"]
    notes = [e.get("note") for e in saved["log"]]
    assert "Spoken decision transcribed." in notes and len(saved["log"]) == before_log + 2
    job.save("and another")                   # idempotent: nothing doubled
    again = json.loads((d / "job.json").read_text())
    assert len(again["voice_turns"]) == 1 and [e.get("note") for e in again["log"]].count("Spoken decision transcribed.") == 1
