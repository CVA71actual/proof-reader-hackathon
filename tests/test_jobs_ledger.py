"""Jobs ledger seams (Manus 05:31): INTEGRATION-001 a concurrent unknown voice cost is never overwritten by a worker's
save; INTEGRATION-002 a finished job releases only its own reservations; INTEGRATION-003 the smoke CLI names its own
ceiling, not the authorized run caps. Fakes only; no provider call."""

import json

import pytest

from runs import research
from server import jobs
from tests import research_fakes as F
from tests.test_research_s1 import PAGE1, PR1, happy_script


def _job(runs_dir, cap=30.0):
    run = jobs.create_run(runs_dir, jobs.validate_choices({"mode": "research", "companies": ["PTGX"],
                                                           "spending_limit_usd": cap}), as_of="2026-11-02")
    return run["run"]["id"]


def _read(runs_dir, rid, name):
    return json.loads((runs_dir / rid / name).read_text())


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def test_integration_001_a_concurrent_unknown_voice_cost_survives_the_workers_save(runs_dir):
    rid = _job(runs_dir)
    script = happy_script()
    first = script[0]

    def voice_fails_meanwhile(req):
        jobs.fail_voice(runs_dir, rid, None, billed_attempts=1, limited=True)   # a possibly billed voice failure
        return first
    reader = F.FakeResponses([voice_fails_meanwhile] + script[1:])
    research.run_research(runs_dir, rid, reader, F.tools({PR1: (200, "text/html", PAGE1)}))
    job, run = _read(runs_dir, rid, "job.json"), _read(runs_dir, rid, "run.json")
    assert job["unpriced_calls"] == 1 and run["run"]["unpriced_calls"] == 1      # never reset to 0
    assert job["voice_unpriced_calls"] == 1 and "Muse Voice Transcribe" in job["unpriced_models"]
    assert len(reader.requests) == 1                     # no further paid request without explicit acceptance
    assert run["run"]["status"] == "needs_input"
    assert run["run"]["research"]["companies"]["PTGX"]["limitation"]["kind"] == "unpriced"
    assert jobs.unknown_unaccepted(job)


def test_integration_001_the_worker_counts_a_voice_unknown_once(runs_dir):
    rid = _job(runs_dir)
    jobs.fail_voice(runs_dir, rid, None, billed_attempts=1, limited=True)
    job = jobs.Job.__new__(jobs.Job)                      # the reconcile alone, on a worker's stale copy
    job.job = {"unpriced_calls": 0, "log": []}
    job.meta = {}
    disk = _read(runs_dir, rid, "job.json")
    job._reconcile_voice_unpriced(disk)
    job._reconcile_voice_unpriced(disk)                   # a second save or refresh adds nothing more
    assert job.job["unpriced_calls"] == 1 and job.meta["unpriced_calls"] == 1


def test_integration_002_a_finished_job_leaves_a_siblings_ticket_alone(runs_dir):
    rid = _job(runs_dir, cap=1.0)
    voice = jobs.reserve_budget(rid, 0.0, 1.0, 0.9)        # an outstanding voice ticket (not the job's)
    try:
        research.run_research(runs_dir, rid, F.FakeResponses([], key=False), F.tools({}))   # researcher unavailable
        assert jobs._RESERVED.get(rid) == {voice: 0.9}                          # still held for its owner
        assert jobs.reserve_budget(rid, 0.0, 1.0, 0.9) is None                 # a second $0.90 is refused
    finally:
        jobs.release_budget(rid, voice)
    assert not jobs._RESERVED.get(rid)


def test_integration_002_release_owned_releases_only_the_owners_tokens():
    a, b = object(), object()
    ta = jobs.reserve_budget("own-test", 0, None, 0.1, owner=a)
    tb = jobs.reserve_budget("own-test", 0, None, 0.1, owner=b)
    tn = jobs.reserve_budget("own-test", 0, None, 0.1)
    assert jobs.release_owned("own-test", a) == 1
    assert set(jobs._RESERVED["own-test"]) == {tb, tn}
    jobs.release_budget("own-test", tb)
    jobs.release_budget("own-test", tn)


@pytest.mark.parametrize("cap", ["80", "100"])
def test_integration_003_the_smoke_cli_names_its_own_ceiling(cap, capsys):
    from tools import research_smoke
    assert research_smoke.main(["--ticker", "PTGX", "--cap-usd", cap, "--i-have-the-operator-go"]) == 2
    out = capsys.readouterr().out
    assert "this smoke script's own ceiling" in out and "$80 for the 10-company submission run" in out
    assert "$100 for the judges' run" in out and "the $30 CVA approved" not in out
