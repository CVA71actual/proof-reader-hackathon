"""ONE-MODEL-RUN-001: a New Run choice to use Muse Spark only. Every reader here is a fake: no network, no key."""

import hashlib
import io
import json
import subprocess

import pytest

from accuracy import export
from report.build import build as build_report
from runs.one_model import NO_SECOND_READER, ONE_MODEL_LINE
from server import jobs
from tests.test_run_controls import (PTGX_ONLY, RUN_SCHEMA, FakeReader, make, run_json,  # noqa: F401 (fixture)
                                     runs_dir, start)
from tests.test_ui import _grab_fn
from tests.conftest import REPO

UI = REPO / "ui"


class MustNotBeCalled:
    """A second reader, Muse Wonder or SAM 3.1 that fails the test if a one-model run touches it."""

    model = "must-not-be-called"

    def __init__(self, name):
        self.name = name

    def ready(self):
        raise AssertionError(f"{self.name} was asked in a one-model run")

    def call(self, *a, **k):
        raise AssertionError(f"{self.name} was called in a one-model run")

    def segment(self, *a, **k):
        raise AssertionError(f"{self.name} was called in a one-model run")


def readers():
    return {"second_reader": MustNotBeCalled("second reader"), "wonder_reader": MustNotBeCalled("Muse Wonder"),
            "sam_reader": MustNotBeCalled("SAM 3.1")}


@pytest.fixture
def job_calls(monkeypatch):
    """Every run_job call the server makes: (second, wonder, sam). The real job still runs."""
    calls, real = [], jobs.run_job

    def spy(runs_dir, run_id, reader, second=None, as_of=None, wonder=None, sam=None, index=None):
        calls.append((second, wonder, sam))
        return real(runs_dir, run_id, reader, second, as_of, wonder, sam, index)
    monkeypatch.setattr(jobs, "run_job", spy)
    return calls


# ---------------------------------------------------------------- the choice


def test_models_is_validated_and_defaults_to_all():
    assert jobs.validate_choices(PTGX_ONLY)["models"] == "all"
    assert jobs.validate_choices({**PTGX_ONLY, "models": None})["models"] == "all"
    assert jobs.validate_choices({**PTGX_ONLY, "models": "one"})["models"] == "one"
    for bad in ("two", "One", 1, True, ["one"]):
        with pytest.raises(ValueError, match='models must be "one"'):
            jobs.validate_choices({**PTGX_ONLY, "models": bad})


def test_invalid_models_is_a_plain_422(runs_dir):
    client, _ = make(runs_dir)
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "models": "both"})
    assert r.status_code == 422
    assert r.json()["detail"] == ('models must be "one" (Muse Spark only) or "all" '
                                  '(Muse Spark, checked by a second reader).')


def test_default_run_keeps_every_reader(runs_dir, monkeypatch):
    client, _ = make(runs_dir, second_reader=None, wonder_reader=None, sam_reader=None)
    meta = start(client)
    m = run_json(runs_dir, meta["id"])["run"]
    assert m["choices"]["models"] == "all"
    assert not any(t.get("step") == "Models chosen" for t in m.get("trail", []))
    # with readers given, an "all" run passes them to the job unchanged (the job itself is not run here)
    second, wonder, sam = object(), object(), object()
    calls = []
    monkeypatch.setattr(jobs, "run_job", lambda *a: calls.append((a[3], a[5], a[6])))
    client, _ = make(runs_dir, second_reader=second, wonder_reader=wonder, sam_reader=sam)
    start(client, models="all")
    assert calls == [(second, wonder, sam)]


# ---------------------------------------------------------------- the job


def test_one_model_run_calls_muse_spark_only_and_records_it(runs_dir, job_calls):
    client, reader = make(runs_dir, **readers())
    meta = start(client, models="one")
    assert job_calls == [(None, None, None)]
    run = run_json(runs_dir, meta["id"])
    RUN_SCHEMA.validate(run)
    m = run["run"]
    assert m["status"] == "done", m["status_reason"]
    assert m["choices"]["models"] == "one"
    assert reader.prompts, "Muse Spark read the page"
    assert "second_reader" not in m and "escalation" not in m
    step = [t for t in m["trail"] if t["step"] == "Models chosen"]
    assert len(step) == 1 and step[0]["text"] == ONE_MODEL_LINE and step[0]["tag"] == "Person"
    assert not any(r.get("found_by") == "second_reader" for r in run["rows"])


def test_one_model_resume_still_calls_muse_spark_only(runs_dir, job_calls, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 1)
    client, reader = make(runs_dir, **readers())
    meta = start(client, models="one", companies=["PTGX", "IONS"])
    assert run_json(runs_dir, meta["id"])["run"]["status"] == "paused_turn_limit"
    r = client.post(f"/api/runs/{meta['id']}/resume", json={})
    assert r.status_code == 202, r.text
    assert job_calls == [(None, None, None), (None, None, None)]
    run = run_json(runs_dir, meta["id"])
    RUN_SCHEMA.validate(run)
    assert run["run"]["resumes"] == 1 and run["run"]["choices"]["models"] == "one"


# ---------------------------------------------------------------- the surfaces


def test_reports_say_one_model_and_never_count_a_second_reader(runs_dir, tmp_path):
    client, _ = make(runs_dir, **readers())
    meta = start(client, models="one")
    out = build_report(meta["id"], out=tmp_path / "out", run_dir=runs_dir / meta["id"],
                       generated_at="2026-11-02T10:00:00+00:00")
    view = out["view"]
    assert view["models_line"] == ONE_MODEL_LINE
    assert NO_SECOND_READER in view["counters_line"] and "by the second reader" not in view["counters_line"]
    from docx import Document
    text = "\n".join(p.text for p in Document(io.BytesIO(out["docx"].read_bytes())).paragraphs)
    assert ONE_MODEL_LINE in text and NO_SECOND_READER in text and "by the second reader" not in text


def test_a_default_run_report_is_unchanged(runs_dir, tmp_path):
    client, _ = make(runs_dir, second_reader=None, wonder_reader=None, sam_reader=None)
    meta = start(client)
    view = build_report(meta["id"], out=tmp_path / "out", run_dir=runs_dir / meta["id"],
                        generated_at="2026-11-02T10:00:00+00:00")["view"]
    assert view["models_line"] is None and "by the second reader 0" in view["counters_line"]


def test_accuracy_export_has_no_second_reader_for_a_one_model_run(runs_dir):
    client, _ = make(runs_dir, **readers())
    meta = start(client, models="one")
    run_bytes = (runs_dir / meta["id"] / "run.json").read_bytes()
    run = json.loads(run_bytes)
    report = {"key": {"sealed": False}, "models": {"Muse Spark": {}},
              "mistake_log": {"from_run": {"by_type": {}, "by_caught_by": {"code": 0, "second_reader": 0,
                                                                            "person": 0}}},
              "inputs": {"run_sha256": hashlib.sha256(run_bytes).hexdigest(), "decisions_sha256": "0" * 64,
                         "words": "w.json", "words_sha256": None}}
    doc = export.build_export(report, run, run_bytes, tip="x", dirty=False)
    assert doc["agreement"] is None
    assert doc["readers"]["second_reader"] is None and doc["readers"]["models"] == ONE_MODEL_LINE
    assert "second_reader" not in doc["mistake_log"]["from_run_by_caught_by"]
    # an "all" run keeps both blocks as before
    all_run = {**run, "run": {**run["run"], "choices": {**run["run"]["choices"], "models": "all"}}}
    doc = export.build_export(report, all_run, run_bytes, tip="x", dirty=False)
    assert doc["agreement"] is not None and "models" not in doc["readers"]
    assert doc["mistake_log"]["from_run_by_caught_by"]["second_reader"] == 0


# ---------------------------------------------------------------- the UI


def test_new_run_dialog_offers_the_choice_with_the_second_reader_as_default():
    html = (UI / "index.html").read_text()
    assert '<input type="radio" name="nr-models" id="nr-models-one" value="one"> Muse Spark only</label>' in html
    assert ('<input type="radio" name="nr-models" id="nr-models-all" value="all" checked> '
            'Muse Spark, checked by a second reader</label>') in html
    assert html.index('id="nr-limit"') < html.index('id="nr-models-one"') < html.index('id="nr-note"')


def test_ui_sends_models_on_run_now_and_continue():
    js = (UI / "app.js").read_text()
    assert js.count("models: nrModels() }).then(function (res) {") == 2
    for fn in ("startRunNow", "startRunFromIntake"):
        assert "models: nrModels()" in _grab_fn(js, fn), fn
    script = "\n".join((
        "var picked = false;",
        "var document = { getElementById: function (id) {"
        " return id === 'nr-models-one' ? { checked: picked } : null; } };",
        _grab_fn(js, "nrModels"),
        "var a = nrModels(); picked = true; var b = nrModels();",
        "console.log(a + ' ' + b);"))
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "all one"


def test_ui_run_card_and_counters_do_not_claim_a_second_reader():
    js = (UI / "app.js").read_text()
    assert f"var ONE_MODEL_LINE = '{ONE_MODEL_LINE}';" in js
    assert f"var NO_SECOND_READER = '{NO_SECOND_READER}';" in js
    assert "if (oneModelRun(meta)) host.append(el('div', 'jb-sub', ONE_MODEL_LINE));" in _grab_fn(js, "updateRunCardJob")
    audit = _grab_fn(js, "renderAudit")
    assert "if (d[0] === 'MREADER' && oneModelRun(meta)) {" in audit
    assert "countersEl.append(el('span', 'ctchip zero', NO_SECOND_READER));" in audit
    script = "\n".join((_grab_fn(js, "oneModelRun"),
                        "console.log([oneModelRun({choices: {models: 'one'}}), oneModelRun({choices: {models: 'all'}}),"
                        " oneModelRun({choices: {}}), oneModelRun(null)].join(' '));"))
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "true false false false"


def test_shared_job_entry_honors_one_model_without_api_wrapper(runs_dir):
    choices = jobs.validate_choices({**PTGX_ONLY, "models": "one"})
    meta = jobs.create_run(runs_dir, choices)["run"]
    reader = FakeReader()
    jobs.run_job(runs_dir, meta["id"], reader,
                 second=MustNotBeCalled("second"), wonder=MustNotBeCalled("wonder"),
                 sam=MustNotBeCalled("sam"))
    run = run_json(runs_dir, meta["id"])
    assert run["run"]["status"] == "done"
    assert reader.prompts
    assert "second_reader" not in run["run"]
    assert "escalation" not in run["run"]


def test_export_summary_uses_one_model_label():
    js = (UI / "app.js").read_text()
    fragment = "if (oneModelRun(meta)) kv('Second reader', NO_SECOND_READER);\n    else kv('Mistakes caught by the second reader', String(st.mistakes.second_reader));"
    assert fragment in _grab_fn(js, "renderExport")
    script = "var out=[];function kv(k,v){out.push([k,v]);}var st={mistakes:{second_reader:0}};" + \
        "var NO_SECOND_READER=" + json.dumps(NO_SECOND_READER) + ";" + _grab_fn(js,"oneModelRun") + \
        "var meta={choices:{models:'one'}};" + fragment + \
        "meta={choices:{models:'all'}};" + fragment + "console.log(JSON.stringify(out));"
    got = subprocess.run(["node", "-"], input=script,text=True,capture_output=True)
    assert got.returncode == 0, got.stderr
    assert json.loads(got.stdout) == [["Second reader",NO_SECOND_READER], ["Mistakes caught by the second reader","0"]]
