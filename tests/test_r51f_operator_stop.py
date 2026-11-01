"""Job 51f: a run left "running" by a worker stopped from outside is put right by a reviewed step, never by hand; and a
run reads on resume exactly the source set it started with. No network and no key: every run is read by a fake reader.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from runs import sources
from server import jobs, run_steps
from server.app import create_app
from tests.test_run_controls import BOTH, RUN_SCHEMA, FakeReader, runs_dir  # noqa: F401  (the fixture)

LIMIT = 5.0
REASON_1 = "stopped by the Operator; 1 potentially billed call(s), cost unknown"


class DiesOnCall(FakeReader):
    """A fake reader whose worker is stopped from outside on call number `n`: SystemExit is not an Exception, so the
    job's own handler does not see it and run.json is left "running", as a SIGTERM leaves it."""

    def __init__(self, n, **kw):
        super().__init__(**kw)
        self.n = n

    def call(self, prompt, image=None):
        if len(self.prompts) + 1 == self.n:
            self.prompts.append(prompt)
            raise SystemExit("stopped from outside")
        return super().call(prompt, image)


@pytest.fixture
def index_file(tmp_path):
    """A sources index file (PTGX events page, IONS table page) whose page and words files are copies in tmp_path,
    so a test may change their bytes."""
    files = tmp_path / "filings"
    files.mkdir()
    pages = []
    builtin = sources.builtin_index()
    for ticker in ("PTGX", "IONS"):
        r = dict(builtin.by_ticker[ticker][0])
        for key, path_key in (("page_file", "page_path"), ("words_file", "words_path")):
            src = r.pop(path_key)
            dst = files / f"{ticker.lower()}-{key}{'.pdf' if key == 'page_file' else '.json'}"
            shutil.copyfile(src, dst)
            r[key] = str(dst)
        r.pop("local_file", None)
        pages.append(r)
    path = tmp_path / "sources_index.test.json"
    path.write_text(json.dumps({"pages": pages}, indent=1))
    return path


def client_for(runs_dir, index_path, reader=None):
    return TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader or FakeReader(),
                                 background=False, index=sources.load_index(index_path)))


def meta(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())["run"]


def job_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "job.json").read_text())


def stale_running(runs_dir, index_path, limit=LIMIT):
    """A run whose worker died during its second call (the IONS page): run.json says "running"."""
    index = sources.load_index(index_path)
    choices = jobs.validate_choices({**BOTH, "spending_limit_usd": limit}, index)
    rid = jobs.create_run(runs_dir, choices)["run"]["id"]
    with pytest.raises(SystemExit):
        jobs.run_job(runs_dir, rid, DiesOnCall(2), None, None, None, None, index)
    assert meta(runs_dir, rid)["status"] == "running" and rid not in jobs.ACTIVE_RUNS
    return rid


def write_stop(runs_dir, rid, **doc):
    body = {"stopped_at": jobs.now_utc(), "signal": "SIGTERM", **doc}
    (runs_dir / rid / jobs.OPERATOR_STOP_FILE).write_text(json.dumps(body))
    return body


# ---------------------------------------------------------------- record_operator_stop


def test_a_stale_running_run_becomes_needs_input_with_the_unknown_counted(runs_dir, index_file):
    rid = stale_running(runs_dir, index_file)
    before = job_of(runs_dir, rid)["unpriced_calls"]
    write_stop(runs_dir, rid, note="worker stopped at 21:31:44Z")
    r = client_for(runs_dir, index_file).post(f"/api/runs/{rid}/operator-stop")
    assert r.status_code == 200, r.text
    m, job = meta(runs_dir, rid), job_of(runs_dir, rid)
    assert m["status"] == "needs_input" and m["status_reason"] == REASON_1
    assert job["unpriced_calls"] == before + 1 and m["unpriced_calls"] == before + 1
    assert "unknown_cost_accepted" not in job
    steps = [t for t in m["trail"] if t["step"] == "Operator stop recorded"]
    assert len(steps) == 1 and steps[0]["tag"] == "Person"
    assert "SIGTERM" in steps[0]["text"] and "cost is unknown" in steps[0]["text"]
    assert "Note: worker stopped at 21:31:44Z" in steps[0]["text"]
    assert job["operator_stop"]["potentially_billed_calls"] == 1 and job["log"][-1]["reason"] == REASON_1
    RUN_SCHEMA.validate(json.loads((runs_dir / rid / "run.json").read_text()))


def test_the_command_line_takes_the_same_path_and_counts_n_calls(runs_dir, index_file, capsys):
    rid = stale_running(runs_dir, index_file)
    write_stop(runs_dir, rid, potentially_billed_calls=2)
    assert run_steps.main(["record_operator_stop", rid, "--runs-dir", str(runs_dir)]) == 0
    m = meta(runs_dir, rid)
    assert m["status_reason"] == "stopped by the Operator; 2 potentially billed call(s), cost unknown"
    assert job_of(runs_dir, rid)["unpriced_calls"] == 2 and m["unpriced_calls"] == 2
    assert "Operator stop recorded" in capsys.readouterr().out


def test_a_second_application_is_refused_and_counts_nothing(runs_dir, index_file, capsys):
    rid = stale_running(runs_dir, index_file)
    write_stop(runs_dir, rid)
    client = client_for(runs_dir, index_file)
    assert client.post(f"/api/runs/{rid}/operator-stop").status_code == 200
    # even if the status were "running" again, the recorded stop is never applied twice
    path = runs_dir / rid / "run.json"
    run = json.loads(path.read_text())
    run["run"]["status"] = "running"
    path.write_text(json.dumps(run))
    r = client.post(f"/api/runs/{rid}/operator-stop")
    assert r.status_code == 409 and "never applied twice" in r.json()["detail"]
    assert run_steps.main(["record_operator_stop", rid, "--runs-dir", str(runs_dir)]) == 2
    assert "never applied twice" in capsys.readouterr().err
    assert job_of(runs_dir, rid)["unpriced_calls"] == 1
    assert len([t for t in meta(runs_dir, rid)["trail"] if t["step"] == "Operator stop recorded"]) == 1


def test_a_run_that_is_not_running_is_refused(runs_dir, index_file):
    client = client_for(runs_dir, index_file, FakeReader(cost=LIMIT + 1))
    rid = client.post("/api/runs/new", json={**BOTH, "spending_limit_usd": LIMIT}).json()["id"]
    assert meta(runs_dir, rid)["status"] == "needs_input"
    write_stop(runs_dir, rid)
    r = client.post(f"/api/runs/{rid}/operator-stop")
    assert r.status_code == 409 and "this run is needs_input" in r.json()["detail"]
    assert job_of(runs_dir, rid)["unpriced_calls"] == 0 and "operator_stop" not in job_of(runs_dir, rid)


def test_a_run_driven_by_this_process_is_not_stale(runs_dir, index_file):
    rid = stale_running(runs_dir, index_file)
    write_stop(runs_dir, rid)
    jobs.ACTIVE_RUNS.add(rid)
    try:
        with pytest.raises(jobs.JobError, match="still going in this server"):
            jobs.record_operator_stop(runs_dir, rid)
    finally:
        jobs.ACTIVE_RUNS.discard(rid)
    assert meta(runs_dir, rid)["status"] == "running"


def test_a_run_written_after_the_stop_time_is_refused(runs_dir, index_file):
    rid = stale_running(runs_dir, index_file)
    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    write_stop(runs_dir, rid, stopped_at=earlier)
    with pytest.raises(jobs.JobError, match="may still be going") as e:
        jobs.record_operator_stop(runs_dir, rid)
    assert e.value.status == 409 and meta(runs_dir, rid)["status"] == "running"


@pytest.mark.parametrize("text, words", [
    ("{not json", "not valid JSON"),
    ("[]", "must be a JSON object"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM", "cost_usd": 0}), "cost_usd"),
    (json.dumps({"signal": "SIGTERM"}), "stopped_at must be"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44", "signal": "SIGTERM"}), "time zone"),
    (json.dumps({"stopped_at": "yesterday", "signal": "SIGTERM"}), "stopped_at must be"),
    (json.dumps({"stopped_at": "2999-01-01T00:00:00Z", "signal": "SIGTERM"}), "in the future"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z"}), "signal must be"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "sigterm"}), "signal must be"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": 15}), "signal must be"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM", "potentially_billed_calls": -1}),
     "potentially_billed_calls"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM", "potentially_billed_calls": True}),
     "potentially_billed_calls"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM", "potentially_billed_calls": "1"}),
     "potentially_billed_calls"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM", "potentially_billed_calls": 1.5}),
     "potentially_billed_calls"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM", "note": ["x"]}), "note must be"),
    (json.dumps({"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM", "note": "a\nb"}), "note must be"),
])
def test_a_malformed_stop_file_is_refused_with_the_reason(runs_dir, index_file, text, words):
    rid = stale_running(runs_dir, index_file)
    (runs_dir / rid / jobs.OPERATOR_STOP_FILE).write_text(text)
    r = client_for(runs_dir, index_file).post(f"/api/runs/{rid}/operator-stop")
    assert r.status_code == 422 and words in r.json()["detail"]
    assert meta(runs_dir, rid)["status"] == "running" and job_of(runs_dir, rid)["unpriced_calls"] == 0


def test_a_missing_stop_file_is_refused(runs_dir, index_file):
    rid = stale_running(runs_dir, index_file)
    r = client_for(runs_dir, index_file).post(f"/api/runs/{rid}/operator-stop")
    assert r.status_code == 409 and "There is no OPERATOR_STOP.json" in r.json()["detail"]


def test_resume_after_the_record_goes_through_the_unknown_cost_gate(runs_dir, index_file):
    rid = stale_running(runs_dir, index_file)
    write_stop(runs_dir, rid)
    reader = FakeReader()
    client = client_for(runs_dir, index_file, reader)
    assert client.post(f"/api/runs/{rid}/operator-stop").status_code == 200
    r = client.post(f"/api/runs/{rid}/resume")
    assert r.status_code == 422 and "accept_unknown_cost" in r.json()["detail"]
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": LIMIT + 1})
    assert r.status_code == 422 and "accept_unknown_cost" in r.json()["detail"]
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": LIMIT, "accept_unknown_cost": True})
    assert r.status_code == 409
    assert meta(runs_dir, rid)["status"] == "needs_input" and reader.prompts == []
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": LIMIT + 1, "accept_unknown_cost": True})
    assert r.status_code == 202, r.text
    m, job = meta(runs_dir, rid), job_of(runs_dir, rid)
    assert len(reader.prompts) == 1 and m["status"] == "done"        # the IONS page, the one the stop cut off
    assert job["unpriced_calls"] == 1 and job["unknown_cost_accepted"] == 1
    assert [t["step"] for t in m["trail"]][:3] == ["Operator stop recorded", "Unknown cost accepted",
                                                  "Spending limit raised"]


def test_zero_potentially_billed_calls_leaves_nothing_unknown(runs_dir, index_file):
    rid = stale_running(runs_dir, index_file)
    write_stop(runs_dir, rid, potentially_billed_calls=0)
    client = client_for(runs_dir, index_file)
    assert client.post(f"/api/runs/{rid}/operator-stop").status_code == 200
    m = meta(runs_dir, rid)
    assert m["status_reason"] == "stopped by the Operator; no potentially billed call" and m["unpriced_calls"] == 0
    assert client.post(f"/api/runs/{rid}/resume").status_code == 202 and meta(runs_dir, rid)["status"] == "done"


# ---------------------------------------------------------------- the source set is frozen when a run starts


def stopped_at_limit(runs_dir, index_file):
    client = client_for(runs_dir, index_file, FakeReader(cost=LIMIT + 1))
    rid = client.post("/api/runs/new", json={**BOTH, "spending_limit_usd": LIMIT}).json()["id"]
    assert meta(runs_dir, rid)["status"] == "needs_input"
    return rid


def test_a_run_records_its_source_set_when_it_starts(runs_dir, index_file):
    rid = stopped_at_limit(runs_dir, index_file)
    frozen = job_of(runs_dir, rid)["source_freeze"]
    assert frozen["by"] == "run start" and frozen["index"]["path"] == str(index_file)
    assert frozen["index"]["sha256"] == jobs._file_sha(index_file)
    index = sources.load_index(index_file)
    for p in frozen["pages"]:
        rec = next(r for t in ("PTGX", "IONS") for r in index.by_ticker[t] if r["source_id"] == p["source_id"])
        assert p["page_sha256"] == rec["page_sha256"] and p["words_sha256"] == rec["words_sha256"]
    assert len(frozen["pages"]) == 2


def test_unchanged_sources_resume(runs_dir, index_file):
    rid = stopped_at_limit(runs_dir, index_file)
    r = client_for(runs_dir, index_file).post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99})
    assert r.status_code == 202 and meta(runs_dir, rid)["status"] == "done"


def test_a_changed_manifest_refuses_resume(runs_dir, index_file):
    rid = stopped_at_limit(runs_dir, index_file)
    index_file.write_text(index_file.read_text() + "\n")       # the same pages; one byte more in the index
    reader = FakeReader()
    r = client_for(runs_dir, index_file, reader).post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99})
    assert r.status_code == 409 and jobs.SOURCE_SET_CHANGED in r.json()["detail"]
    assert meta(runs_dir, rid)["status"] == "needs_input" and meta(runs_dir, rid)["resumes"] == 0
    assert reader.prompts == []


@pytest.mark.parametrize("which", ["page_file", "words_file"])
def test_one_changed_source_byte_refuses_resume(runs_dir, index_file, which):
    rid = stopped_at_limit(runs_dir, index_file)
    pages = json.loads(index_file.read_text())["pages"]
    target = next(p for p in pages if p["ticker"] == "IONS")[which]
    data = bytearray(open(target, "rb").read())
    data[-2] ^= 0x01
    open(target, "wb").write(bytes(data))
    r = client_for(runs_dir, index_file).post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99})
    assert r.status_code == 409 and jobs.SOURCE_SET_CHANGED in r.json()["detail"]
    assert f"the {which.split('_')[0]} file of " in r.json()["detail"]


def test_a_different_index_given_is_irrelevant_the_run_reads_its_own(runs_dir, index_file):
    """Job 51g reversed 51f here: the frozen index is the run's own, so another index given to resume_run or to the
    job is not used; the run's recorded index file is."""
    rid = stopped_at_limit(runs_dir, index_file)
    builtin = sources.builtin_index()
    jobs.resume_run(runs_dir, rid, 99, index=builtin)
    reader = FakeReader()
    jobs.run_job(runs_dir, rid, reader, None, None, None, None, builtin)
    m = meta(runs_dir, rid)
    assert m["status"] == "done" and len(reader.prompts) == 1      # the IONS page of the run's own index


# ---------------------------------------------------------------- a run started before Job 51f


def pre_51f(runs_dir, index_file):
    rid = stopped_at_limit(runs_dir, index_file)
    path = runs_dir / rid / "job.json"
    job = json.loads(path.read_text())
    del job["source_freeze"]
    path.write_text(json.dumps(job))
    return rid


def test_an_old_run_is_refused_until_a_person_records_its_source_set(runs_dir, index_file, capsys):
    rid = pre_51f(runs_dir, index_file)
    client = client_for(runs_dir, index_file)
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99})
    assert r.status_code == 409 and "record_source_freeze" in r.json()["detail"]
    sha = jobs._file_sha(index_file)
    wrong = "0" * 64
    args = ["record_source_freeze", rid, "--runs-dir", str(runs_dir), "--index", str(index_file)]
    assert run_steps.main(args + ["--index-sha256", wrong]) == 2
    assert "not the" in capsys.readouterr().err and "source_freeze" not in job_of(runs_dir, rid)
    assert client.post(f"/api/runs/{rid}/source-freeze", json={"index_sha256": "abc"}).status_code == 422
    assert run_steps.main(args + ["--index-sha256", sha]) == 0
    frozen = job_of(runs_dir, rid)["source_freeze"]
    assert frozen["by"] == "record_source_freeze" and frozen["checked_index_sha256"] == sha
    step = [t for t in meta(runs_dir, rid)["trail"] if t["step"] == "Source set recorded"]
    assert len(step) == 1 and step[0]["tag"] == "Person" and sha in step[0]["text"]
    RUN_SCHEMA.validate(json.loads((runs_dir / rid / "run.json").read_text()))
    r = client.post(f"/api/runs/{rid}/source-freeze", json={"index_sha256": sha})
    assert r.status_code == 409 and "never recorded again" in r.json()["detail"]
    assert client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 99}).status_code == 202


def test_an_old_run_whose_files_no_longer_match_the_index_is_not_recorded(runs_dir, index_file):
    rid = pre_51f(runs_dir, index_file)
    target = next(p for p in json.loads(index_file.read_text())["pages"] if p["ticker"] == "PTGX")["words_file"]
    open(target, "a").write(" ")
    client = client_for(runs_dir, index_file)
    r = client.post(f"/api/runs/{rid}/source-freeze", json={"index_sha256": jobs._file_sha(index_file)})
    assert r.status_code == 409 and jobs.SOURCE_SET_CHANGED in r.json()["detail"]
    assert "source_freeze" not in job_of(runs_dir, rid)


def test_a_run_that_already_went_without_a_record_is_stopped_by_the_job(runs_dir, index_file):
    rid = pre_51f(runs_dir, index_file)
    reader = FakeReader()
    jobs.run_job(runs_dir, rid, reader, None, None, None, None, sources.load_index(index_file))
    m = meta(runs_dir, rid)
    assert reader.prompts == [] and m["status"] == "needs_input" and "record_source_freeze" in m["status_reason"]
