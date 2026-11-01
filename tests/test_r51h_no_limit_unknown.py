"""Job 51h: a possibly-billed call is counted whether or not the run has a spending limit; with no limit the one gate
pauses the run on the unknown cost and a resume needs accept_unknown_cost true. No test reaches the network."""

from fastapi.testclient import TestClient

from server.app import create_app
from tests.test_r38_timeouts import (CHOICES, BasetenSilentOpenRouterAnswers, both_keys, job_of, no_sleep,  # noqa: F401
                                     run_of, runs_dir)
from tests.test_run_controls import RUN_SCHEMA, FakeReader


def test_no_limit_possibly_billed_timeout_pauses_and_resumes_only_with_acceptance(runs_dir, both_keys):
    second = BasetenSilentOpenRouterAnswers()
    second.routes = []
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(),
                                   second_reader=second, background=False))
    r = client.post("/api/runs/new", json={**CHOICES, "companies": ["PTGX"]})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unpriced_calls"] == 1 and run["run"]["choices"]["spending_limit_usd"] is None
    assert run["run"]["status"] == "needs_input"
    assert "could not be priced" in run["run"]["status_reason"] and "cost unknown" in run["run"]["status_reason"]
    assert second.routes == ["baseten"]                                    # no fallback dispatched

    for body in (None, {"accept_unknown_cost": False}):
        r = client.post(f"/api/runs/{rid}/resume", json=body)
        assert r.status_code == 422 and "accept_unknown_cost" in r.json()["detail"]
    assert run_of(runs_dir, rid)["run"]["status"] == "needs_input" and second.routes == ["baseten"]

    r = client.post(f"/api/runs/{rid}/resume", json={"accept_unknown_cost": True})
    assert r.status_code == 202, r.text
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    steps = [t for t in run["run"]["trail"] if t["step"] == "Unknown cost accepted"]
    assert len(steps) == 1 and steps[0]["tag"] == "Person"
    assert job["unknown_cost_accepted"] == 1 and job["unpriced_calls"] == 1
    assert run["run"]["status"] == "done" and second.routes == ["baseten"]       # it continued; no fallback after
    assert job["second"]["src-ptgx-8k-2026-08-05-ex991"]["status"] == "unreadable"
    assert run["run"]["status"] == "done"
    RUN_SCHEMA.validate(run)

