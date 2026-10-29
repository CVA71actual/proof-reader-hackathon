"""A spending limit that is an integer too large for a float is refused like any other bad limit."""

import json
import shutil

import pytest

from server import jobs
from tests.test_run_controls import PTGX_ONLY, RUNS, FakeReader, make, run_json

HUGE = "1" + "0" * 1000


def test_validate_choices_refuses_a_huge_integer_limit():
    with pytest.raises(ValueError):
        jobs.validate_choices({**PTGX_ONLY, "spending_limit_usd": 10**1000})


def test_the_new_run_endpoint_refuses_a_huge_integer_limit(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    shutil.copytree(RUNS / "sample", runs_dir / "sample")
    client, reader = make(runs_dir)
    body = json.dumps({**PTGX_ONLY, "spending_limit_usd": 1}).replace(
        '"spending_limit_usd": 1', f'"spending_limit_usd": {HUGE}').encode()
    got = client.post("/api/runs/new", content=body, headers={"content-type": "application/json"})
    assert got.status_code == 422 and isinstance(got.json()["detail"], str)
    assert reader.prompts == []


def test_resume_refuses_a_huge_integer_limit(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    shutil.copytree(RUNS / "sample", runs_dir / "sample")
    client, reader = make(runs_dir, FakeReader(cost=0.60))
    rid = client.post("/api/runs/new", json={**PTGX_ONLY, "spending_limit_usd": 0.5}).json()["id"]
    before = run_json(runs_dir, rid)["run"]["choices"]["spending_limit_usd"]
    got = client.post(f"/api/runs/{rid}/resume", content=('{"spending_limit_usd":%s}' % HUGE).encode(),
                      headers={"content-type": "application/json"})
    assert got.status_code == 422 and isinstance(got.json()["detail"], str)
    assert run_json(runs_dir, rid)["run"]["choices"]["spending_limit_usd"] == before
