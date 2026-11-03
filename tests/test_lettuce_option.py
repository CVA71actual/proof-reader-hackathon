"""LETTUCE-OPTION: the optional LettuceDetect support check on a research run. A fake detector only: nothing is
downloaded, installed or loaded. Register rows: OUTCOME-001, UNKNOWN-001, CLAIM-001, NEXT-CALLER-001."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from capture import support_signal as ss
from runs import research, research_audit
from server import jobs
from server.app import create_app
from tests import research_fakes as F
from tests.test_research_s1 import PAGE1, PR1, happy_script, run_json


class FakeDetector:
    """capture/support_signal.py's detector shape; records every call."""
    model = ss.MODEL
    weights_sha256 = "fakesha"

    def __init__(self, spans=(), tokens=100, err=None):
        self.spans, self.tokens, self.err, self.calls = list(spans), tokens, err, []

    def count_tokens(self, passage, claim):
        return self.tokens

    def predict(self, passage, claim):
        self.calls.append((passage, claim))
        if self.err:
            raise self.err
        return self.spans


class MustNotRun:
    model, weights_sha256 = ss.MODEL, None

    def count_tokens(self, *a):
        raise AssertionError("the support check ran on a run with it off")

    predict = count_tokens


def _run(runs_dir, detector, **choices):
    reader = F.FakeResponses(happy_script())
    tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1)})
    tools.support_detector = detector
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    client = TestClient(app)
    r = client.post("/api/runs/new", json={"mode": "research", "companies": ["PTGX"], "as_of": "2026-11-02", **choices})
    assert r.status_code == 202, r.text
    return client, r.json()["id"]


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


_STAMP = re.compile(r'"(?:at|started_at|finished_at|captured_at|retrieved_at|checked_at|recorded_at|'
                    r'first_asked_at|given_at|created_at|digest|checkpoint_digest)": "[^"]*"')


def _normalized(path):
    return _STAMP.sub('"T": "-"', path.read_text())


def test_off_by_default_and_a_run_with_it_off_is_unchanged(tmp_path):
    outs = []
    for i, choice in enumerate(({}, {"support_check": None}, {"support_check": "off"})):
        d = tmp_path / f"r{i}"
        d.mkdir()
        _, rid = _run(d, MustNotRun(), **choice)
        meta = run_json(d, rid)["run"]
        assert "support_check" not in meta["choices"] and "support_check" not in meta["research"]
        assert "support_check" not in (d / rid / "run.json").read_text()
        outs.append(_normalized(d / rid / "run.json"))
    assert outs[0] == outs[1] == outs[2]                        # off, null and absent are the same run
    assert jobs.validate_choices({"mode": "research", "companies": ["PTGX"]}) == \
        jobs.validate_choices({"mode": "research", "companies": ["PTGX"], "support_check": None})


def test_the_option_is_validated_and_page_mode_refuses_it():
    with pytest.raises(ValueError):
        jobs.validate_choices({"mode": "research", "companies": ["PTGX"], "support_check": "something-else"})
    with pytest.raises(ValueError):
        jobs.validate_choices({"companies": ["PTGX"], "support_check": "lettucedetect"})
    ch = jobs.validate_choices({"mode": "research", "companies": ["PTGX"], "support_check": "lettucedetect"})
    assert ch["support_check"] == "lettucedetect"


def test_no_finding_is_recorded_on_the_row_and_is_not_an_issue(runs_dir):
    det = FakeDetector([])
    client, rid = _run(runs_dir, det, support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    assert run["run"]["choices"]["support_check"] == "lettucedetect"
    row = next(r for r in run["rows"] if r.get("research"))
    sc = row["research"]["support_check"]
    assert sc["result"] == "no_finding" and sc["findings_only"] and "not a statement" in sc["reason"]
    assert sc["basis"]["revision"] == 1 and sc["model"] == ss.MODEL and sc["weights_sha256"] == "fakesha"
    passage, claim = det.calls[0]
    assert passage == row["research"]["evidence"][0]["quote"]            # the same text the checks read
    assert claim == research.support_claim(row) and "Q2 2027" in claim
    assert not any(i["code"] == "unsupported_spans" for i in row["research"]["issues"])
    assert run["run"]["research"]["support_check"]["totals"] == {"no_finding": 1, "unsupported_spans": 0,
                                                                 "could_not_run": 0}
    # the next caller: the API serves it, the audit and the report carry it
    got = client.get(f"/api/runs/{rid}").json()
    served = next(r for r in got["rows"] if r["id"] == row["id"])
    assert served["research"]["support_check"]["result"] == "no_finding"
    assert got["run"]["research"]["support_check"]["totals"]["no_finding"] == 1


def test_unsupported_spans_are_a_row_issue_for_review(runs_dir):
    det = FakeDetector([{"start": 0, "end": 6, "confidence": 0.9}])
    _, rid = _run(runs_dir, det, support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("research"))
    sc = row["research"]["support_check"]
    assert sc["result"] == "unsupported_spans" and sc["spans"][0]["text"] == research.support_claim(row)[0:6]
    issue = next(i for i in row["research"]["issues"] if i["code"] == "unsupported_spans")
    assert issue["subject_id"] == row["id"]
    from runs.state import current_state
    assert current_state(run, [])["targets"][row["id"]]["accepted"] is False     # never an acceptance


@pytest.mark.parametrize("det,why", [
    (None, "pinned weights are not installed"),                                    # no package / weights here
    (FakeDetector(tokens=5000), "tokens > limit 4096"),                           # over-limit input
    (FakeDetector(err=RuntimeError("boom")), "RuntimeError"),                     # an error
])
def test_could_not_run_is_a_recorded_limitation_never_an_issue_and_the_run_goes_on(runs_dir, monkeypatch, det, why):
    monkeypatch.delenv(ss.WEIGHTS_ENV, raising=False)
    client, rid = _run(runs_dir, det, support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    meta = run["run"]
    assert meta["research"]["companies"]["PTGX"]["status"] == "finished"           # never blocks or fails
    row = next(r for r in run["rows"] if r.get("research"))
    sc = row["research"]["support_check"]
    assert sc["result"] == "could_not_run" and why in sc["reason"]
    assert not any(i["code"] == "unsupported_spans" for i in row["research"]["issues"])
    label = meta["research"]["support_check"]["label"]
    assert label == "Support check could not run for 1 of 1 proposals"
    from report.data import build_view
    view = build_view(run, [], generated_at="2026-11-02T00:00:00+00:00")
    assert label + "." in view["research_lines"]                                   # the report carries it


def test_the_options_endpoint_offers_it_off_by_default_without_installing_anything(runs_dir, monkeypatch):
    monkeypatch.delenv(ss.WEIGHTS_ENV, raising=False)
    app = create_app(runs_dir=runs_dir, reader=F.FakeResponses([]), background=False, second_reader=None,
                     wonder_reader=None, sam_reader=None)
    opt = TestClient(app).get("/api/research/options").json()["support_check"]
    assert opt["choices"] == ["lettucedetect"] and opt["default"] is None
    assert opt["available"] is False and opt["unavailable_reason"]
    assert opt["model_revision"] == ss.MODEL_REVISION


def test_the_support_check_does_not_change_the_keep_basis(runs_dir):
    from runs.review import review_basis
    from runs.state import current_state
    _, rid = _run(runs_dir, FakeDetector([]), support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("research"))
    values = current_state(run, [])["targets"][row["id"]]["values"]
    before = review_basis(run, row, values)
    row["research"]["support_check"]["result"] = "unsupported_spans"
    assert review_basis(run, row, values) == before         # an advisory finding never rewrites what a Keep binds
