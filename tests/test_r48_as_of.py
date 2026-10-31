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


def test_a_period_ending_20_oct_is_not_past_as_of_13_oct():
    row = {"date": "2026-10-15", "date_end": "2026-10-20", "date_text": "15-20 October 2026",
           "checks": [{"name": "date_match", "result": "pass"}]}
    assert jobs.past_reason(row, run_reference_date(_run("2026-10-13"))) is None
    assert jobs.past_reason(row, run_reference_date(_run())) is not None


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
