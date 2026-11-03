"""LETTUCE-OPTION under Codex's quiet-workflow contract: an optional, advisory, bounded LettuceDetect support check on
research runs. Fakes only (an in-process fake detector, and fake worker scripts for the subprocess boundary): nothing is
downloaded, installed or loaded. Register: OUTCOME-001, IDENTITY-001, CLAIM-001, UNKNOWN-001, NEXT-CALLER-001."""

import json
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from capture import support_process as sp
from capture import support_signal as ss
from runs import research
from server import jobs
from server.app import create_app
from tests import research_fakes as F
from tests.test_research_s1 import PAGE1, PR1, happy_script, run_json


class FakeDetector:
    model, weights_sha256 = ss.MODEL, sp.PINNED_WEIGHTS_SHA256

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


def _run(runs_dir, detector=None, *, worker=None, python=None, weights=None, timeout=None, keep=False, **choices):
    reader = F.FakeResponses(happy_script())
    tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE1)})
    tools.support_detector = detector
    tools.support_worker, tools.support_python, tools.support_weights = worker, python, weights
    tools.support_timeout_s = timeout
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    client = TestClient(app)
    r = client.post("/api/runs/new", json={"mode": "research", "companies": ["PTGX"], "as_of": "2026-11-02", **choices})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    if keep:
        assert client.post(f"/api/runs/{rid}/decisions", json={"target": "e1", "action": "keep", "note": None,
                                                               "fix": None}).status_code == 200
    return client, rid


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")
_DIGEST = re.compile(r'"(?:digest|checkpoint_digest)": "[^"]*"')


def _normalized(path):
    return _DIGEST.sub('"digest": "-"', _TIME.sub("T", path.read_text()))


# ---------------------------------------------------------------- off is today's run


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
    assert outs[0] == outs[1] == outs[2]
    assert jobs.validate_choices({"mode": "research", "companies": ["PTGX"]}) == \
        jobs.validate_choices({"mode": "research", "companies": ["PTGX"], "support_check": None})


def test_the_option_is_validated_and_page_mode_refuses_it():
    with pytest.raises(ValueError):
        jobs.validate_choices({"mode": "research", "companies": ["PTGX"], "support_check": "something-else"})
    with pytest.raises(ValueError):
        jobs.validate_choices({"companies": ["PTGX"], "support_check": "lettucedetect"})
    assert jobs.validate_choices({"mode": "research", "companies": ["PTGX"],
                                  "support_check": "lettucedetect"})["support_check"] == "lettucedetect"


# ---------------------------------------------------------------- the contract: field, passage, advisory, identical


def test_the_displayed_title_is_checked_against_its_exact_cited_passage(runs_dir):
    det = FakeDetector([])
    client, rid = _run(runs_dir, det, support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("research"))
    sc = row["research"]["support_check"]
    passage, claim = det.calls[0]
    assert claim == row["title"] and sc["field"] == "title" and sc["claim"] == row["title"]     # no composite
    assert passage == "\n".join(s["quote"] for s in row["research"]["evidence"]) == sc["passage"]   # no truncation
    assert sc["result"] == "no_finding" and "not a statement that the claim is supported" in sc["reason"]
    assert sc["advisory"] and not re.search(r"verif", json.dumps(sc), re.I)          # never 'verified'
    assert not any(i["code"] == "unsupported_spans" for i in row["research"]["issues"])
    served = client.get(f"/api/runs/{rid}").json()
    assert served["support_findings"]["e1"]["result"] == "no_finding"               # the next caller: the API


def _projection(runs_dir, rid):
    from report.data import build_view
    from runs.state import current_state
    run = run_json(runs_dir, rid)
    decisions = [json.loads(l) for l in (runs_dir / rid / "decisions.jsonl").read_text().splitlines() if l.strip()]
    st = current_state(run, decisions)
    view = build_view(run, decisions, generated_at="2026-11-02T00:00:00+00:00")
    targets = {k: {f: v.get(f) for f in ("accepted", "on_calendar", "needs_attention", "review_bucket", "status",
                                          "group", "semantic_status", "date_display")}
               for k, v in st["targets"].items()}
    calendar = {key: [r["id"] for r in view[key]] for key in ("calendar", "past", "later", "contingent", "undated",
                                                             "timing_unresolved", "open_rows")}
    return {"targets": targets, "counts": st["counts"], "mistakes": st["mistakes"], "unresolved": st["unresolved"],
            "excluded": st["excluded"], "view_counts": view["counts"], "calendar": calendar,
            "checks": {r["id"]: r["checks"] for r in run["rows"]},
            "issues": {r["id"]: r["research"]["issues"] for r in run["rows"] if r.get("research")},
            "decisions": [{k: v for k, v in d.items() if k != "at"} for d in decisions]}


@pytest.mark.parametrize("keep", [False, True])
def test_on_versus_off_changes_no_acceptance_decision_calendar_or_count(tmp_path, keep):
    """Codex acceptance (4): before/after acceptance, decisions, calendar and report counts are identical, even when
    the detector flags wording."""
    off, on = tmp_path / "off", tmp_path / "on"
    off.mkdir()
    on.mkdir()
    _, rid_off = _run(off, MustNotRun(), keep=keep)
    _, rid_on = _run(on, FakeDetector([{"start": 0, "end": 6, "confidence": 0.98}]), keep=keep,
                     support_check="lettucedetect")
    row = next(r for r in run_json(on, rid_on)["rows"] if r.get("research"))
    assert row["research"]["support_check"]["result"] == "unsupported_spans"          # a finding did arrive
    assert row["research"]["support_check"]["spans"][0]["text"] == row["title"][0:6]  # an exact substring
    assert _projection(off, rid_off) == _projection(on, rid_on)


def test_a_finding_never_changes_the_keep_basis(runs_dir):
    from runs.review import review_basis
    from runs.state import current_state
    _, rid = _run(runs_dir, FakeDetector([]), support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("research"))
    values = current_state(run, [])["targets"][row["id"]]["values"]
    before = review_basis(run, row, values)
    row["research"]["support_check"]["result"] = "unsupported_spans"
    assert review_basis(run, row, values) == before


# ---------------------------------------------------------------- currency keyed by material identity (Manus 3)


@pytest.mark.parametrize("change", ["source_id_same_words", "claim", "page_words_hash", "weights"])
def test_a_changed_claim_or_source_hides_the_old_spans(runs_dir, change):
    _, rid = _run(runs_dir, FakeDetector([{"start": 0, "end": 6}]), support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("research"))
    assert research.support_view(run, row)["result"] == "unsupported_spans"
    if change == "source_id_same_words":         # a different source with the very same words
        run["sources"].append(dict(run["sources"][0], id="src-r9-ptgx"))
        row["research"]["evidence"][0]["source_id"] = "src-r9-ptgx"
    elif change == "claim":
        row["title"] = row["title"] + " (edited)"
    elif change == "page_words_hash":
        run["sources"][0]["pages"][0]["words_sha256"] = "0" * 64
    else:
        row["research"]["support_check"]["weights_sha256"] = "1" * 64
    view = research.support_view(run, row)
    assert view["result"] == "stale" and view["spans"] == []


# ---------------------------------------------------------------- could_not_run is honest and never blocks


@pytest.mark.parametrize("det,why", [(FakeDetector(tokens=5000), "tokens > limit 4096"),
                                     (FakeDetector(err=RuntimeError("boom")), "RuntimeError")])
def test_in_process_failures_are_could_not_run(runs_dir, det, why):
    _, rid = _run(runs_dir, det, support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    sc = next(r for r in run["rows"] if r.get("research"))["research"]["support_check"]
    assert sc["result"] == "could_not_run" and why in sc["reason"]
    assert run["run"]["research"]["companies"]["PTGX"]["status"] == "finished"
    assert run["run"]["research"]["support_check"]["label"] == "Support check could not run for 1 of 1 proposals"


# ---------------------------------------------------------------- the out-of-process worker boundary


def _worker(tmp_path, body):
    w = tmp_path / "fake_worker.py"
    w.write_text("import json, sys, time\nreq = json.loads(sys.stdin.read())\n" + body)
    return str(w)


def _weights(tmp_path):
    d = tmp_path / "weights" / ss.MODEL_REVISION
    d.mkdir(parents=True)
    return str(d)


def test_the_worker_runs_once_per_company_and_its_results_are_recorded(runs_dir, tmp_path):
    w = _worker(tmp_path, "print(json.dumps({'weights_sha256': '" + sp.PINNED_WEIGHTS_SHA256 + "', "
                          "'load_seconds': 0.1, 'error': None, 'results': {i['id']: {'result': 'no_finding', "
                          "'spans': [], 'model': 'm'} for i in req['items']}}))\n"
                          "open(" + repr(str(tmp_path / "calls")) + ", 'a').write('x')\n")
    _, rid = _run(runs_dir, worker=w, python=sys.executable, weights=_weights(tmp_path), support_check="lettucedetect")
    run = run_json(runs_dir, rid)
    sc = next(r for r in run["rows"] if r.get("research"))["research"]["support_check"]
    assert sc["result"] == "no_finding" and sc["weights_sha256"] == sp.PINNED_WEIGHTS_SHA256
    assert (tmp_path / "calls").read_text() == "x"                                 # one worker for the company
    assert run["run"]["research"]["support_check"]["companies"]["PTGX"]["worker"]["status"] == "ok"


@pytest.mark.parametrize("body,why", [
    ("time.sleep(30)\n", "timeout"),
    ("sys.exit(3)\n", "exited with code 3"),
    ("print('not json')\n", "malformed JSON"),
    ("print(json.dumps({'weights_sha256': 'other', 'results': {}, 'error': None}))\n", "not the pinned one"),
])
def test_worker_failures_are_could_not_run_with_the_reason_and_bounded(runs_dir, tmp_path, body, why):
    began = time.monotonic()
    _, rid = _run(runs_dir, worker=_worker(tmp_path, body), python=sys.executable, weights=_weights(tmp_path),
                  timeout=1.0, support_check="lettucedetect")
    assert time.monotonic() - began < 20                                           # never waits unbounded
    run = run_json(runs_dir, rid)
    sc = next(r for r in run["rows"] if r.get("research"))["research"]["support_check"]
    assert sc["result"] == "could_not_run" and why in sc["reason"]
    assert run["run"]["research"]["companies"]["PTGX"]["status"] == "finished"


def test_a_missing_interpreter_is_could_not_run(runs_dir, monkeypatch):
    monkeypatch.delenv(sp.PYTHON_ENV, raising=False)
    monkeypatch.delenv(sp.WEIGHTS_ENV, raising=False)
    _, rid = _run(runs_dir, support_check="lettucedetect")
    sc = next(r for r in run_json(runs_dir, rid)["rows"] if r.get("research"))["research"]["support_check"]
    assert sc["result"] == "could_not_run" and "interpreter is not installed" in sc["reason"]


def test_a_person_stop_kills_the_worker(tmp_path):
    w = _worker(tmp_path, "time.sleep(30)\n")
    began = time.monotonic()
    results, meta = sp.run_batch([{"id": "e1", "claim": "c", "passage": "p"}], python=sys.executable,
                                 weights=_weights(tmp_path), worker=w, timeout_s=30, should_stop=lambda: True)
    assert time.monotonic() - began < 5 and meta["reason"] == "stopped by a person"
    assert results["e1"]["result"] == "could_not_run"


def test_the_options_endpoint_offers_it_off_by_default(runs_dir, monkeypatch):
    monkeypatch.delenv(sp.PYTHON_ENV, raising=False)
    app = create_app(runs_dir=runs_dir, reader=F.FakeResponses([]), background=False, second_reader=None,
                     wonder_reader=None, sam_reader=None)
    opt = TestClient(app).get("/api/research/options").json()["support_check"]
    assert opt["choices"] == ["lettucedetect"] and opt["default"] is None and opt["available"] is False
    assert "Advisory only" in opt["note"]


# ---------------------------------------------------------------- IDENTITY-001: a person's Fix makes the finding stale


@pytest.mark.parametrize("field", ["title", "quote"])
def test_a_persons_fix_to_the_claim_or_quote_makes_the_served_finding_stale(runs_dir, field):
    client, rid = _run(runs_dir, FakeDetector([{"start": 0, "end": 6}]), support_check="lettucedetect")
    assert client.get(f"/api/runs/{rid}").json()["support_findings"]["e1"]["result"] == "unsupported_spans"
    row = next(r for r in run_json(runs_dir, rid)["rows"] if r["id"] == "e1")
    new = row[field] + " (edited)" if field == "title" else row[field].replace("expected", "anticipated")
    r = client.post(f"/api/runs/{rid}/decisions", json={"target": "e1", "action": "fix", "note": None,
                                                        "fix": {"field": field, "from": row[field], "to": new}})
    assert r.status_code == 200, r.text
    lines = [json.loads(l) for l in (runs_dir / rid / "decisions.jsonl").read_text().splitlines()]
    assert any(l["action"] == "fix" and l["fix"]["field"] == field for l in lines)        # a real decisions line
    served = client.get(f"/api/runs/{rid}").json()["support_findings"]["e1"]
    assert served["result"] == "stale" and served["spans"] == []
    # Undo the fix: the effective proposal is the checked one again, so the finding is current again
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": "e1", "action": "undo", "note": None,
                                                           "fix": None}).status_code == 200
    assert client.get(f"/api/runs/{rid}").json()["support_findings"]["e1"]["result"] == "unsupported_spans"


def test_an_unrelated_fix_keeps_the_finding_current(runs_dir):
    client, rid = _run(runs_dir, FakeDetector([{"start": 0, "end": 6}]), support_check="lettucedetect")
    row = next(r for r in run_json(runs_dir, rid)["rows"] if r["id"] == "e1")
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": "e1", "action": "fix", "note": None,
                                                           "fix": {"field": "program", "from": row["program"],
                                                                   "to": "PN-X"}}).status_code == 200
    assert client.get(f"/api/runs/{rid}").json()["support_findings"]["e1"]["result"] == "unsupported_spans"
