"""Job 51g: a resume compares the run with its OWN frozen index, never the server's default; the unknown-cost gate does
not depend on a spending limit; record_operator_stop completes a half-applied stop without counting twice. No network
and no key: every run is read by a fake reader.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from runs import sources
from server import jobs, run_steps
from server.app import create_app
from tests.test_r51f_operator_stop import (LIMIT, REASON_1, client_for, index_file, job_of, meta,  # noqa: F401
                                           pre_51f, stale_running, write_stop)
from tests.test_run_controls import BOTH, RUN_SCHEMA, FakeReader, runs_dir  # noqa: F401  (the fixtures)


@pytest.fixture
def own_index(index_file):
    """The fixture index under a non-default name, as the real runs were read (sources_index.fourteen-1013.json)."""
    path = index_file.with_name("sources_index.fourteen-1013.json")
    index_file.rename(path)
    return path


def builtin_server(runs_dir, reader=None):
    """A server whose default index is another one (the built-in index): irrelevant to a frozen run."""
    return TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader or FakeReader(),
                                 background=False, index=sources.builtin_index()))


# ---------------------------------------------------------------- 1. the run's own frozen index


def test_a_run_frozen_on_a_non_default_index_resumes_with_acceptance(runs_dir, own_index):
    rid = stale_running(runs_dir, own_index)
    frozen = job_of(runs_dir, rid)["source_freeze"]["index"]
    assert frozen == {"path": str(own_index.resolve()), "name": "sources_index.fourteen-1013.json",
                      "sha256": jobs._file_sha(own_index)}
    write_stop(runs_dir, rid)
    reader = FakeReader()
    client = builtin_server(runs_dir, reader)
    assert client.post(f"/api/runs/{rid}/operator-stop").status_code == 200
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": LIMIT + 1, "accept_unknown_cost": True})
    assert r.status_code == 202, r.text
    m = meta(runs_dir, rid)
    assert m["status"] == "done" and len(reader.prompts) == 1        # the IONS page, from the run's own index
    assert {s["id"] for s in json.loads((runs_dir / rid / "run.json").read_text())["sources"]} == \
        {p["source_id"] for p in job_of(runs_dir, rid)["source_freeze"]["pages"]}


def test_a_server_with_no_index_given_also_uses_the_runs_own(runs_dir, own_index):
    client = client_for(runs_dir, own_index, FakeReader(cost=LIMIT + 1))
    rid = client.post("/api/runs/new", json={**BOTH, "spending_limit_usd": LIMIT}).json()["id"]
    plain = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(),
                                  background=False))       # the server default index (runs/sources_index.json or built-in)
    r = plain.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99})
    assert r.status_code == 202, r.text
    assert meta(runs_dir, rid)["status"] == "done"


def test_the_runs_own_index_changed_still_refuses(runs_dir, own_index):
    rid = stale_running(runs_dir, own_index)
    write_stop(runs_dir, rid)
    client = builtin_server(runs_dir)
    assert client.post(f"/api/runs/{rid}/operator-stop").status_code == 200
    own_index.write_text(own_index.read_text() + "\n")
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": LIMIT + 1, "accept_unknown_cost": True})
    assert r.status_code == 409 and jobs.SOURCE_SET_CHANGED in r.json()["detail"]
    assert "sources_index.fourteen-1013.json" in r.json()["detail"]


def test_a_missing_recorded_index_file_is_refused_with_a_plain_reason(runs_dir, own_index):
    rid = stale_running(runs_dir, own_index)
    write_stop(runs_dir, rid)
    reader = FakeReader()
    client = builtin_server(runs_dir, reader)
    assert client.post(f"/api/runs/{rid}/operator-stop").status_code == 200
    os.remove(own_index)
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": LIMIT + 1, "accept_unknown_cost": True})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "sources_index.fourteen-1013.json" in detail and "not on disk" in detail
    m, job = meta(runs_dir, rid), job_of(runs_dir, rid)
    assert m["status"] == "needs_input" and m["resumes"] == 0 and "unknown_cost_accepted" not in job
    assert reader.prompts == []


def test_the_job_itself_stops_when_its_recorded_index_is_gone(runs_dir, own_index):
    client = client_for(runs_dir, own_index, FakeReader(cost=LIMIT + 1))
    rid = client.post("/api/runs/new", json={**BOTH, "spending_limit_usd": LIMIT}).json()["id"]
    jobs.resume_run(runs_dir, rid, 99)
    os.remove(own_index)
    reader = FakeReader()
    jobs.run_job(runs_dir, rid, reader, None, None, None, None, sources.builtin_index())
    m = meta(runs_dir, rid)
    assert reader.prompts == [] and m["status"] == "needs_input" and "not on disk" in m["status_reason"]


def test_an_old_run_records_the_operator_supplied_index_and_resumes_on_it(runs_dir, own_index, monkeypatch):
    rid = pre_51f(runs_dir, own_index)
    monkeypatch.chdir(own_index.parent)            # the Operator names the file relative to where they stand
    sha = jobs._file_sha(own_index)
    assert run_steps.main(["record_source_freeze", rid, "--runs-dir", str(runs_dir), "--index", own_index.name,
                           "--index-sha256", sha]) == 0
    frozen = job_of(runs_dir, rid)["source_freeze"]
    assert frozen["index"]["path"] == str(own_index.resolve()) and frozen["index"]["name"] == own_index.name
    step = [t for t in meta(runs_dir, rid)["trail"] if t["step"] == "Source set recorded"][0]
    assert own_index.name in step["text"] and sha in step["text"]
    monkeypatch.chdir(runs_dir)
    r = builtin_server(runs_dir).post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99})
    assert r.status_code == 202, r.text
    assert meta(runs_dir, rid)["status"] == "done"


# ---------------------------------------------------------------- 2. the unknown-cost gate needs no limit


def paused_unpriced_no_limit(runs_dir, index_file):
    """A run with no spending limit, paused at its turn limit, holding one unpriced call nobody has accepted."""
    index = sources.load_index(index_file)
    rid = jobs.create_run(runs_dir, jobs.validate_choices(dict(BOTH), index), 1)["run"]["id"]
    jobs.run_job(runs_dir, rid, FakeReader(priced=False), None, None, None, None, index)
    m = meta(runs_dir, rid)
    assert m["status"] == "paused_turn_limit" and m["choices"]["spending_limit_usd"] is None
    # with no limit the job does not count unpriced calls itself; an Operator stop (or an older run) leaves the count
    job = job_of(runs_dir, rid)
    job["unpriced_calls"] = 1
    jobs._write(runs_dir / rid / "job.json", job)
    return rid


def test_an_unknown_cost_with_no_limit_needs_acceptance(runs_dir, index_file):
    rid = paused_unpriced_no_limit(runs_dir, index_file)
    reader = FakeReader()
    client = client_for(runs_dir, index_file, reader)
    for body in (None, {"accept_unknown_cost": False}):
        r = client.post(f"/api/runs/{rid}/resume", json=body)
        assert r.status_code == 422 and "accept_unknown_cost" in r.json()["detail"]
        assert "could not be priced" in r.json()["detail"]
    m = meta(runs_dir, rid)
    assert m["status"] == "paused_turn_limit" and m["resumes"] == 0 and reader.prompts == []
    r = client.post(f"/api/runs/{rid}/resume", json={"accept_unknown_cost": True})
    assert r.status_code == 202, r.text
    m, job = meta(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unknown_cost_accepted"] == 1 and job["unpriced_calls"] == 1
    steps = [t for t in m["trail"] if t["step"] == "Unknown cost accepted"]
    assert len(steps) == 1 and steps[0]["tag"] == "Person" and "no spending limit" in steps[0]["text"]
    assert "Spending limit raised" not in [t["step"] for t in m["trail"]]
    RUN_SCHEMA.validate(json.loads((runs_dir / rid / "run.json").read_text()))


def test_an_accepted_unknown_with_no_limit_is_not_asked_again(runs_dir, index_file):
    rid = paused_unpriced_no_limit(runs_dir, index_file)
    index = sources.load_index(index_file)
    jobs.resume_run(runs_dir, rid, None, True)
    jobs.run_job(runs_dir, rid, FakeReader(), None, None, None, None, index)     # priced calls only: it finishes
    assert meta(runs_dir, rid)["status"] == "done"
    assert not jobs.unknown_unaccepted(job_of(runs_dir, rid))      # nothing new is unknown: no acceptance asked again
    assert len([t for t in meta(runs_dir, rid)["trail"] if t["step"] == "Unknown cost accepted"]) == 1


def test_an_operator_stop_on_a_run_with_no_limit_needs_acceptance(runs_dir, index_file):
    rid = stale_running(runs_dir, index_file, limit=None)
    write_stop(runs_dir, rid)
    client = client_for(runs_dir, index_file)
    assert client.post(f"/api/runs/{rid}/operator-stop").status_code == 200
    r = client.post(f"/api/runs/{rid}/resume")
    assert r.status_code == 422 and "accept_unknown_cost" in r.json()["detail"]
    r = client.post(f"/api/runs/{rid}/resume", json={"accept_unknown_cost": True})
    assert r.status_code == 202, r.text
    assert job_of(runs_dir, rid)["unknown_cost_accepted"] == 1


# ---------------------------------------------------------------- 3. record_operator_stop after a crash


def test_a_half_applied_operator_stop_is_completed_without_counting_twice(runs_dir, index_file, monkeypatch, capsys):
    rid = stale_running(runs_dir, index_file)
    before = job_of(runs_dir, rid)["unpriced_calls"]
    write_stop(runs_dir, rid, note="crash test")
    real = jobs._write

    def crash_on_run_json(path, doc):
        if path.name == "run.json":
            raise SystemExit("stopped between the two writes")
        return real(path, doc)

    monkeypatch.setattr(jobs, "_write", crash_on_run_json)
    with pytest.raises(SystemExit):
        jobs.record_operator_stop(runs_dir, rid)
    monkeypatch.setattr(jobs, "_write", real)
    job = job_of(runs_dir, rid)
    assert job["operator_stop"] and job["unpriced_calls"] == before + 1
    assert meta(runs_dir, rid)["status"] == "running"                 # the half-applied state

    assert run_steps.main(["record_operator_stop", rid, "--runs-dir", str(runs_dir)]) == 0
    m, job2 = meta(runs_dir, rid), job_of(runs_dir, rid)
    assert m["status"] == "needs_input" and m["status_reason"] == REASON_1
    assert job2["unpriced_calls"] == before + 1 and m["unpriced_calls"] == before + 1
    assert job2["operator_stop"] == job["operator_stop"] and len(job2["log"]) == len(job["log"])
    steps = [t for t in m["trail"] if t["step"] == "Operator stop recorded"]
    assert len(steps) == 1 and steps[0]["at"] == job["operator_stop"]["recorded_at"]
    assert "Note: crash test" in steps[0]["text"]
    RUN_SCHEMA.validate(json.loads((runs_dir / rid / "run.json").read_text()))
    capsys.readouterr()

    # both applied: refused, nothing counted
    r = client_for(runs_dir, index_file).post(f"/api/runs/{rid}/operator-stop")
    assert r.status_code == 409 and "never applied twice" in r.json()["detail"]
    assert job_of(runs_dir, rid)["unpriced_calls"] == before + 1
