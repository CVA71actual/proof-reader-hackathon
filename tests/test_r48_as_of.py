"""Job 48: one reference date per run (`as_of`), read at one place (runs.state.run_reference_date)."""
import copy
import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from report.data import build_view, load_run_dir
from runs import calendar as cal
from runs.state import as_of_note, current_state, run_reference_date
from server import jobs

REPO = Path(__file__).resolve().parent.parent
SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))


def _run(as_of=None, started="2026-10-31T09:00:00+00:00"):
    run, _ = load_run_dir(REPO / "runs" / "sample")
    run = copy.deepcopy(run)
    run["run"]["started_at"] = started
    run["run"].pop("as_of", None)
    if as_of:
        run["run"]["as_of"] = as_of
    return run


def test_default_is_the_start_day_and_as_of_overrides_it():
    assert run_reference_date(_run()) == date(2026, 10, 31)
    assert run_reference_date(_run("2026-10-13")) == date(2026, 10, 13)


def test_window_anchor_is_as_of():
    run = _run("2026-10-13")
    _, anchor, end = cal.run_interval_partition(run, current_state(run, []))
    assert (anchor, end) == ("2026-10-13", "2027-04-13")
    assert cal.run_interval_partition(_run(), current_state(_run(), []))[1] == "2026-10-31"


def test_a_printed_past_period_is_not_automatically_excluded():
    row = {"date": "2026-10-15", "date_end": "2026-10-20", "date_text": "15-20 October 2026",
           "checks": [{"name": "date_match", "result": "pass"}]}
    assert jobs.past_reason(row, run_reference_date(_run("2026-10-13"))) is None
    assert jobs.past_reason(row, run_reference_date(_run())) is None  # DATE-LITERAL-001


def test_wording_only_when_they_differ():
    assert as_of_note(_run("2026-10-13")) == "as of 13 Oct 2026 · run on 31 Oct 2026"
    assert as_of_note(_run()) is None
    assert as_of_note(_run("2026-10-31")) is None
    assert build_view(_run("2026-10-13"), [], generated_at="2026-10-31T00:00:00Z")["as_of_note"]
    assert build_view(_run(), [], generated_at="2026-10-31T00:00:00Z")["as_of_note"] is None


def test_schema_accepts_as_of_and_started_at_is_untouched():
    assert not list(SCHEMA.iter_errors(_run("2026-10-13")))
    bad = _run("13 Oct")
    assert list(SCHEMA.iter_errors(bad))
    assert _run("2026-10-13")["run"]["started_at"] == "2026-10-31T09:00:00+00:00"


def test_validate_as_of():
    assert jobs.validate_as_of(None) is None
    assert jobs.validate_as_of("2026-10-13") == "2026-10-13"
    for bad in ("13 Oct 2026", "2026-13-01", 5, "2026-10-13T00:00"):
        with pytest.raises(ValueError):
            jobs.validate_as_of(bad)


# ---------------------------------------------------------------- Job 48b: one clock

def _job_run(tmp_path, started, as_of="2026-08-05"):
    run = jobs.create_run(tmp_path, {"companies": ["PTGX"], "event_types": [], "time_window_months": None,
                                     "spending_limit_usd": None, "note_for_agent": None}, as_of=as_of)
    f = tmp_path / run["run"]["id"] / "run.json"
    run["run"]["started_at"] = started
    f.write_text(json.dumps(run))
    return tmp_path / run["run"]["id"]


def test_same_as_of_started_on_different_days_gives_the_same_coverage_date(tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    days = []
    for d, started in (("a", "2026-09-01T09:00:00+00:00"), ("b", "2026-10-31T09:00:00+00:00")):
        job = jobs.Job(_job_run(tmp_path / d, started), reader=None)
        days.append(job.coverage_date({"published_date": "2020-01-01"}))
    assert days == [date(2026, 8, 5)] * 2


def test_conflicting_job_as_of_is_refused_and_a_matching_one_is_fine(tmp_path):
    d = _job_run(tmp_path, "2026-10-31T09:00:00+00:00")
    with pytest.raises(jobs.JobError):
        jobs.Job(d, reader=None, as_of=date(2026, 9, 1))
    assert jobs.Job(d, reader=None, as_of=date(2026, 8, 5)).coverage_date({}) == date(2026, 8, 5)


def test_a_retained_exclusion_reason_with_another_date_does_not_move_the_reference():
    from runs import recheck_dates as rd
    run = _run("2026-10-13")
    run["exclusions"] = [{"id": "x1", "target": "e1", "rule": "event_in_past", "by": "code",
                          "reason": "Ended before this run began (2026-01-01)."}]
    assert not hasattr(rd, "_today_of") and not hasattr(rd, "run_day")
    assert run_reference_date(run) == date(2026, 10, 13)


def test_no_usable_date_is_unresolved_not_today():
    run = _run()
    run["run"]["started_at"] = "not a date"
    from tests.pivot_assertions import fresh_keep
    state = current_state(run, [fresh_keep(run, [], "e1")])
    parts, anchor, end = cal.run_interval_partition(run, state)
    assert (anchor, end) == (None, None)
    assert len(parts["contingent"]) > 0
    assert parts["calendar"] == [] and parts["past"] == [] and parts["later"] == []


UNRESOLVED = {"start": None, "end": None, "unresolved": True,
              "reason": "This run has no usable reference date (as_of or started_at)."}


def test_api_window_for_a_run_with_no_usable_date(tmp_path):
    import shutil
    from fastapi.testclient import TestClient
    from server.app import create_app
    shutil.copytree(REPO / "runs" / "sample", tmp_path / "sample")
    f = tmp_path / "sample" / "run.json"
    data = json.loads(f.read_text())
    data["run"]["started_at"] = "not a date"
    data["run"].pop("as_of", None)
    f.write_text(json.dumps(data))
    body = TestClient(create_app(runs_dir=tmp_path, ui_dir=tmp_path / "no-ui")).get("/api/runs/sample").json()
    assert body["calendar_window"] == UNRESOLVED
    assert body["as_of_note"] == UNRESOLVED["reason"]
    assert body["calendar_partition"]["calendar"] == []


def test_report_text_for_a_run_with_no_usable_date():
    run = _run()
    run["run"]["started_at"] = "not a date"
    view = build_view(run, [], generated_at="2026-10-31T00:00:00Z")
    assert view["calendar_window"] == UNRESOLVED["reason"]
