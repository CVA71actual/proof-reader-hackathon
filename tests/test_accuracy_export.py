"""Job 10: accuracy.json beside a scored run, served by the decisions server with its hashes checked.

The sample run scored against the IONS answer key is the worked case (an author's key: sealed false, complete true).
"""

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from accuracy import export
from accuracy.key import load_key
from accuracy.run import main as run_main
from accuracy.run import score_run
from server.app import create_app
from tests.conftest import REPO

RUNS = REPO / "runs"
KEYS = REPO / "accuracy" / "keys"
IONS_KEY = KEYS / "ions_ars_2026_p95.key.json"
PLANTED = KEYS / "ions_ars_2026_p95.planted.json"
PTGX_KEY = KEYS / "ptgx_8k_2026-08-05_ex991_p3.key.json"
NOW = datetime(2026, 10, 29, 4, 47, tzinfo=timezone.utc)


def plants():
    doc = json.loads(PLANTED.read_text())
    return doc["plants"], json.loads((REPO / doc["reading"]).read_text())


@pytest.fixture
def sample(tmp_path):
    d = tmp_path / "sample"
    shutil.copytree(RUNS / "sample", d)
    (d / export.FILE_NAME).unlink(missing_ok=True)       # score it fresh here, whatever is committed
    return d


def scored(run_dir, key_path=IONS_KEY, with_plants=True):
    p, reading = plants() if with_plants else (None, None)
    return score_run(load_key(key_path), run_dir, str(key_path.relative_to(REPO)), p, reading)


def exported(run_dir, **kw):
    out = export.write(run_dir, scored(run_dir), now=NOW, tip="abc1234", dirty=False, **kw)
    return json.loads(out.read_text())


# ---------------------------------------------------------------- what the file holds


def test_the_samples_export_says_whose_key_it_is_and_what_was_scored(sample):
    doc = exported(sample)
    assert doc["export_version"] == 1 and doc["run"] == "sample" and doc["sample"] is True
    assert (doc["date"], doc["product_tip"], doc["product_dirty"]) == ("2026-10-29", "abc1234", False)
    assert doc["key"]["sealed"] is False and doc["key"]["complete"] is True, "an author's key, and it says so"
    assert (doc["key"]["page_number"], doc["key"]["cells"], doc["key"]["events"]) == (95, 69, 0)
    m = doc["models"]["Muse Spark"]
    assert (m["values"]["found"], m["values"]["N"]) == (69, 69) and m["events"] is None
    assert (m["planted"]["caught"], m["planted"]["total"]) == (12, 12)
    assert doc["planted"] == {"caught": 12, "total": 12, "by_kind": m["planted"]["by_kind"]}
    assert doc["readers"]["first"] == ["Muse Spark"]


def test_the_agreement_numbers_come_from_the_runs_own_rows(sample):
    run = json.loads((sample / "run.json").read_text())
    compared = [r for r in run["rows"] if any(t["step"] == "Second reading" for t in r["trail"])]
    split = [r for r in compared if r.get("disagreement")]
    doc = exported(sample)
    a = doc["agreement"]
    assert (a["rows_compared"], a["rows_disagreed"], a["rows_agreed"]) == (len(compared), len(split),
                                                                           len(compared) - len(split))
    assert a["rate"] == round((len(compared) - len(split)) / len(compared), 4) and a["rows_disagreed"] >= 1
    assert a["both_wrong"] == {"rows": None, "values": None}, "no event key and no second read of the table: no claim"
    assert "Agreement is not evidence" in a["note"]


def test_the_mistake_counts_by_type_are_from_this_scoring_and_from_the_runs_own_log(sample):
    doc = exported(sample)
    assert doc["mistake_log"]["from_scoring"] == {}, "every key value was found"
    assert doc["mistake_log"]["from_run"] == {"PERIOD_MISREAD": 1, "WRONG_COMPANY": 1, "WRONG_EVENT_TYPE": 1}
    assert doc["mistake_log"]["from_run_by_caught_by"] == {"code": 1, "second_reader": 1, "person": 1}


def test_a_scoring_with_wrong_values_counts_them_by_type(sample):
    run = json.loads((sample / "run.json").read_text())
    cell = run["tables"][0]["rows"][0]["cells"][0]
    from tests.test_accuracy import recheck
    from tests.conftest import FIXTURES
    words = json.loads((FIXTURES / "ions_ars_2026_p95.words.json").read_text())
    run["tables"][0]["rows"][0]["cells"][0] = {**cell, **recheck({**cell, "value": "115,316"}, "Product sales, net", words),
                                                "id": cell["id"], "value": "115,316"}
    (sample / "run.json").write_text(json.dumps(run))
    doc = exported(sample)
    assert doc["models"]["Muse Spark"]["values"]["found"] == 68
    assert doc["mistake_log"]["from_scoring"] == {"WRONG_VALUE": 1}


def test_the_shape_the_screens_read_is_there(sample):
    """ui/app.js accuracyLineFrom: key.sealed, models[name].values {found, N}, events {found, M}, planted {caught, total}."""
    doc = exported(sample)
    assert isinstance(doc["key"]["sealed"], bool)
    for m in doc["models"].values():
        assert {"found", "N"} <= set(m["values"]) or m["values"] is None
    assert {"caught", "total"} <= set(doc["planted"])
    label = "Sealed set: " if doc["key"]["sealed"] is True else "Unsealed (author key): "
    m = doc["models"]["Muse Spark"]
    line = label + f"values {m['values']['found']}/{m['values']['N']} · planted {doc['planted']['caught']}/{doc['planted']['total']}"
    assert line == "Unsealed (author key): values 69/69 · planted 12/12"


def test_a_key_that_says_sealed_is_exported_as_sealed_and_a_partial_one_as_not_complete(sample, tmp_path):
    key = load_key(IONS_KEY)
    sealed = {**key, "sealed": True, "complete": False, "cells": key["cells"][:10]}
    path = tmp_path / "k.json"
    path.write_text(json.dumps(sealed))
    doc = json.loads(export.write(sample, score_run(sealed, sample, str(path)), now=NOW, tip="t", dirty=False).read_text())
    assert doc["key"]["sealed"] is True and doc["key"]["complete"] is False and doc["key"]["cells"] == 10
    assert doc["models"]["Muse Spark"]["values"]["unscored"] == 59


def test_events_and_the_second_reader_on_the_two_reader_ptgx_run(tmp_path):
    d = tmp_path / "ptgx-r5"
    shutil.copytree(RUNS / "ptgx-r5", d)
    doc = json.loads(export.write(d, score_run(load_key(PTGX_KEY), d, "k"), now=NOW, tip="t", dirty=False).read_text())
    m = doc["models"]["Muse Spark"]
    assert (m["events"]["found"], m["events"]["M"]) == (2, 2) and m["values"] is None
    assert doc["sample"] is False
    assert doc["readers"]["second_reader"]["status"] == "read" and doc["readers"]["second_reader"]["input"] == "image"
    a = doc["agreement"]
    assert (a["rows_compared"], a["rows_agreed"], a["rows_disagreed"], a["rate"]) == (2, 2, 0, 1.0)
    assert a["both_wrong"]["rows"] == 0, "both agreed and the key says both were right"


def test_both_readers_agreeing_on_a_wrong_event_is_counted(tmp_path):
    d = tmp_path / "ptgx-r5"
    shutil.copytree(RUNS / "ptgx-r5", d)
    key = load_key(PTGX_KEY)
    key = {**key, "events": [{**key["events"][0], "date_text": "Q4 2030"}, key["events"][1]]}   # the key says Q4 2030
    from accuracy.score import score_events
    run = json.loads((d / "run.json").read_text())
    ev = score_events(run["rows"], key, run.get("exclusions"))
    assert ev["counts"]["wrong_date"] == 1
    doc = export.agreement(run, None, ev)
    assert doc["both_wrong"]["rows"] == 1 and doc["rows_agreed"] == 2


# ---------------------------------------------------------------- the file and its hashes


def test_the_file_binds_the_numbers_to_the_run_and_to_itself(sample):
    doc = exported(sample)
    assert doc["run_sha256"] == export.sha256_bytes((sample / "run.json").read_bytes())
    assert doc["content_sha256"] == export.content_hash(doc)
    data, problem = export.read_if_intact(sample)
    assert problem is None and json.loads(data) == doc


def test_a_changed_number_is_noticed(sample):
    exported(sample)
    f = sample / export.FILE_NAME
    doc = json.loads(f.read_text())
    doc["models"]["Muse Spark"]["values"]["found"] = 10
    f.write_text(json.dumps(doc))
    data, problem = export.read_if_intact(sample)
    assert data is None and "does not match the hash recorded in it" in problem


def test_a_file_scored_from_another_version_of_the_run_is_refused(sample):
    exported(sample)
    run = json.loads((sample / "run.json").read_text())
    run["run"]["label"] = "Sample run, edited"
    (sample / "run.json").write_text(json.dumps(run))
    data, problem = export.read_if_intact(sample)
    assert data is None and "different version of run.json" in problem


def test_no_file_is_not_a_problem_and_damage_is_named(sample):
    assert export.read_if_intact(sample) == (None, None)
    (sample / export.FILE_NAME).write_text("{not json")
    assert export.read_if_intact(sample)[1] == "This run's accuracy.json is not valid JSON."
    (sample / export.FILE_NAME).write_text("[1]")
    assert "does not match the hash" in export.read_if_intact(sample)[1]


def test_a_new_scoring_replaces_the_old_one(sample):
    exported(sample)
    first = (sample / export.FILE_NAME).read_text()
    export.write(sample, scored(sample, with_plants=False), now=NOW, tip="t2", dirty=True)
    doc = json.loads((sample / export.FILE_NAME).read_text())
    assert doc["product_tip"] == "t2" and doc["product_dirty"] is True and doc["planted"] is None
    assert (sample / export.FILE_NAME).read_text() != first


def test_git_state_names_a_commit_here_or_says_it_cannot():
    tip, dirty = export.git_state()
    assert (tip is None) == (dirty is None) and (tip is None or len(tip) >= 7 or tip.isalnum())
    assert export.git_state(Path("/nonexistent-dir-for-test")) == (None, None)


# ---------------------------------------------------------------- the server route


@pytest.fixture
def served(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    shutil.copytree(RUNS / "sample", runs / "sample")
    (runs / "sample" / export.FILE_NAME).unlink(missing_ok=True)
    exported(runs / "sample")
    return TestClient(create_app(runs_dir=runs, ui_dir=runs / "no-ui")), runs


def test_the_server_serves_the_file_at_both_places_the_screens_look(served):
    c, runs = served
    want = (runs / "sample" / export.FILE_NAME).read_bytes()
    for url in ("/runs/sample/accuracy.json", "/api/runs/sample/accuracy"):
        r = c.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "application/json" and r.content == want
    assert r.json()["key"]["sealed"] is False


def test_a_run_with_no_scoring_has_no_accuracy_file(served):
    c, runs = served
    (runs / "sample" / export.FILE_NAME).unlink()
    for url in ("/runs/sample/accuracy.json", "/api/runs/sample/accuracy"):
        r = c.get(url)
        assert r.status_code == 404 and r.json()["detail"] == "No accuracy scoring is saved for this run."
    assert c.get("/runs/nope/accuracy.json").status_code == 404


def test_the_server_refuses_a_file_changed_after_it_was_written(served):
    c, runs = served
    f = runs / "sample" / export.FILE_NAME
    doc = json.loads(f.read_text())
    doc["planted"]["caught"] = 0
    f.write_text(json.dumps(doc))
    r = c.get("/runs/sample/accuracy.json")
    assert r.status_code == 409 and "changed after it was written" in r.json()["detail"]


def test_the_server_refuses_a_file_that_belongs_to_another_version_of_the_run(served):
    c, runs = served
    run_file = runs / "sample" / "run.json"
    run = json.loads(run_file.read_text())
    run["run"]["label"] = "changed"
    run_file.write_text(json.dumps(run))
    r = c.get("/api/runs/sample/accuracy")
    assert r.status_code == 409 and "different version of run.json" in r.json()["detail"]


def test_the_server_refuses_the_whole_run_when_a_page_file_changed_like_every_other_route(served):
    c, runs = served
    page = runs / "sample" / "pages" / "p95.png"
    data = bytearray(page.read_bytes())
    data[len(data) // 2] ^= 1
    page.write_bytes(bytes(data))
    r = c.get("/runs/sample/accuracy.json")
    assert r.status_code == 409 and "does not match the hash recorded in run.json" in r.json()["detail"]


# ---------------------------------------------------------------- the command


def test_the_command_exports_with_export_alone_and_prints_where(sample, capsys):
    assert run_main(["--key", str(IONS_KEY), "--planted", str(PLANTED), "--export", str(sample)]) == 0
    out = capsys.readouterr().out
    assert f"Wrote {sample / export.FILE_NAME}" in out
    data, problem = export.read_if_intact(sample)
    assert problem is None and json.loads(data)["models"]["Muse Spark"]["planted"]["total"] == 12


def test_the_command_takes_run_and_export_together_when_they_are_the_same_run(sample, capsys):
    assert run_main(["--key", str(IONS_KEY), "--run", str(sample), "--export", str(sample)]) == 0
    assert (sample / export.FILE_NAME).is_file()


def test_the_command_refuses_a_mismatched_or_unusable_export(sample, tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_main(["--key", str(IONS_KEY), "--run", str(sample), "--export", str(tmp_path / "other")])
    with pytest.raises(SystemExit):
        run_main(["--key", str(IONS_KEY), "--reading", "x", "--words", "y", "--export", "sample"])
    assert not (sample / export.FILE_NAME).exists()


def test_a_refused_key_writes_nothing(sample, tmp_path, capsys):
    key = load_key(IONS_KEY)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**key, "cells": [{**key["cells"][0], "value": "999,999,999"}]}))
    assert run_main(["--key", str(bad), "--export", str(sample)]) == 2
    assert not (sample / export.FILE_NAME).exists()


# ---------------------------------------------------------------- the committed sample


def test_the_committed_sample_accuracy_is_a_valid_unsealed_author_key_scoring_of_the_committed_sample_run():
    data, problem = export.read_if_intact(RUNS / "sample")
    assert problem is None and data is not None, problem
    doc = json.loads(data)
    assert doc["sample"] is True and doc["key"]["sealed"] is False and doc["key"]["complete"] is True
    assert doc["key"]["path"] == "accuracy/keys/ions_ars_2026_p95.key.json"
    m = doc["models"]["Muse Spark"]
    assert (m["values"]["found"], m["values"]["N"], m["planted"]["caught"], m["planted"]["total"]) == (69, 69, 12, 12)
    assert doc["product_tip"] and doc["date"]


def test_the_committed_sample_accuracy_is_what_the_scorer_makes_now_apart_from_when_and_where(tmp_path):
    d = tmp_path / "sample"
    shutil.copytree(RUNS / "sample", d)
    (d / export.FILE_NAME).unlink()
    fresh = exported(d)
    committed = json.loads((RUNS / "sample" / export.FILE_NAME).read_text())
    drop = ("date", "made_at", "product_tip", "product_dirty", "content_sha256")
    assert {k: v for k, v in fresh.items() if k not in drop} == {k: v for k, v in committed.items() if k not in drop}


# ---------------------------------------------------------------- Job 49c: accuracy scores the effective status


def _scored(d, mutate, decisions=None):
    run = json.loads((d / "run.json").read_text())
    mutate(run["rows"])
    (d / "run.json").write_text(json.dumps(run))
    if decisions:
        (d / "decisions.jsonl").write_text("".join(json.dumps(x) + "\n" for x in decisions))
    return score_run(load_key(PTGX_KEY), d, "k")["events"]


def _copy(tmp_path):
    d = tmp_path / "ptgx-r5"
    shutil.copytree(RUNS / "ptgx-r5", d)
    return d


def test_a_saved_pass_that_now_fails_scores_unsupported_not_found(tmp_path):
    base = _scored(_copy(tmp_path / "a"), lambda rows: None)
    assert base["counts"]["found"] == 2 and base["counts"]["unsupported"] == 0

    def break_check(rows):
        c = rows[0]["checks"][0]
        c["result"], c["reason"] = "fail", "The check now fails."
        rows[0]["status"], rows[0]["group"] = "matched", "kept"          # the saved status is stale
    sc = _scored(_copy(tmp_path / "b"), break_check)
    assert (sc["counts"]["found"], sc["counts"]["unsupported"]) == (1, 1)


def test_a_saved_fail_that_now_passes_scores_found(tmp_path):
    def fail_then_repair(rows):
        rows[0]["status"], rows[0]["group"] = "failed", "open"            # saved as failed; its checks all pass
    sc = _scored(_copy(tmp_path), fail_then_repair)
    assert sc["counts"]["found"] == 2


def test_a_keep_does_not_change_the_score(tmp_path):
    def hold(rows):
        c = rows[0]["checks"][0]
        c["result"], c["reason"] = "fail", "The check now fails."
    plain = _scored(_copy(tmp_path / "a"), hold)
    run = json.loads((_copy(tmp_path / "x") / "run.json").read_text())
    keep = {"by": "person", "target": run["rows"][0]["id"], "action": "keep", "note": None, "at": "t"}
    kept = _scored(_copy(tmp_path / "b"), hold, [keep])
    assert plain["counts"] == kept["counts"] and plain["counts"]["found"] == 1
