"""Job 11: the confirmed prices and `python -m runs.demo`.

The demo reads saved files only: it lists the runs, prints one run's audit counts, its saved accuracy numbers (labelled by
what the key says it is) and its cost per kept row, then serves the app on this machine.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from accuracy import export
from capture import read_events, read_table, research_instructions
from runs import build_run, demo, prices
from tests.conftest import FIXTURES, REPO
from tests.test_cost_and_words import cost_at, prices_of

RUNS = REPO / "runs"
PIN = read_events.PIN


def run_json(name):
    return json.loads((RUNS / name / "run.json").read_text())


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    shutil.copytree(RUNS, d, ignore=shutil.ignore_patterns("report", "twelve-*"))
    return d


# ---------------------------------------------------------------- the confirmed prices flow into the committed runs


def test_the_saved_ptgx_runs_cost_what_their_saved_tokens_cost_at_the_confirmed_prices():
    first = json.loads(read_events.READING_PATH.read_text())["usage"]
    second = json.loads((FIXTURES / f"{PIN}.events.second.json").read_text())["usage"]
    spark = prices.cost_usd("Muse Spark", first["prompt_tokens"], first["completion_tokens"])
    assert spark == pytest.approx(0.028184) and run_json("ptgx-r4a")["run"]["spend_usd"] == pytest.approx(spark, abs=1e-6)
    r5 = run_json("ptgx-r5")   # no run.prices: tallied at the placeholder Spark Open price, spend kept as recorded
    both = spark + cost_at(prices_of(r5), "Muse Spark Open", second["prompt_tokens"], second["completion_tokens"])
    assert r5["run"]["spend_usd"] == pytest.approx(both, abs=5e-6)   # each step's cost is rounded to a millionth


def test_the_scan_run_prices_its_sam_requests_per_image_and_its_read_per_token():
    run = run_json("ptgx-scan")
    sam = sum(t["cost_usd"] for r in run["rows"] for t in r["trail"] if t["who"] == "SAM 3.1")
    read = json.loads((FIXTURES / f"{PIN}.scan_reading.json").read_text())["reads"][0]["usage"]
    spark = prices.cost_usd("Muse Spark", read["prompt_tokens"], read["completion_tokens"])
    assert sam == pytest.approx(3 * 0.0025, abs=1e-6)
    assert run["run"]["spend_usd"] == pytest.approx(sam + spark, abs=1e-6)


def test_every_model_step_in_every_committed_run_is_priced_by_the_table_now():
    for name in ("sample", "ptgx-r4a", "ptgx-r5", "ptgx-scan"):
        run = run_json(name)
        costs = [t["cost_usd"] for r in run["rows"] for t in r["trail"] if t["tag"] == "Model"]
        assert costs and all(c is not None for c in costs)
        assert run["run"]["spend_usd"] == pytest.approx(prices.run_spend_usd(run["rows"]), abs=1e-6)


def test_the_live_wonder_fixture_is_priced_at_the_confirmed_wonder_price():
    rec = json.loads((FIXTURES / f"{PIN}.wonder.json").read_text())
    u = rec["usage"]
    assert rec["cost_usd"] == pytest.approx((u["prompt_tokens"] * 4.00 + u["completion_tokens"] * 20.00) / 1_000_000, abs=1e-6)
    assert "cost_usd_at_placeholder_price" not in rec
    sam = json.loads((FIXTURES / f"{PIN}.sam.json").read_text())
    assert sam["cost_usd"] == pytest.approx(0.0075) and "cost_usd_at_placeholder_price" not in sam


def test_no_confirmed_price_is_still_called_a_placeholder_in_the_code_or_the_docs():
    """Every line of runs/prices.py that says PLACEHOLDER is about Muse Spark Open, the one price not yet read."""
    lines = [l for l in (REPO / "runs" / "prices.py").read_text().splitlines() if "PLACEHOLDER" in l]
    assert all("Spark Open" in l or "Baseten" in l for l in lines)
    for doc in ("README.md", "runs/README.md", "server/README.md"):
        for l in (REPO / doc).read_text().splitlines():
            if "placeholder" in l.lower() and ("price" in l.lower() or "cost" in l.lower()):
                assert "Spark Open" in l or "Baseten" in l or "placeholder until it is read" in l, (doc, l)


# ---------------------------------------------------------------- the demo


def say(capsys, *args, runs_dir=None, **kw):
    argv = list(args) + (["--runs-dir", str(runs_dir)] if runs_dir else [])
    code = demo.main(argv, **kw)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_demo_lists_ptgx_record_but_keeps_unbound_historical_decisions_out_of_acceptance(capsys):
    code, out, _ = say(capsys, "--no-serve")
    assert code == 0
    head, _, rest = out.partition("\nptgx-r5\n")
    assert head.startswith("Saved runs:")
    for rid in ("demo", "ptgx-r4a", "ptgx-r5", "ptgx-scan", "sample"):
        assert re.search(rf"^  {rid} +#\d+", head, re.M)
    assert "ptgx-r5 · PTGX · two readers: saved" in rest and "pages read: 1" in rest
    # PIVOT: the saved model results remain recorded; unbound historical Keeps are not current acceptance.
    assert [r["group"] for r in run_json("ptgx-r5")["rows"]] == ["kept", "kept"]
    # CALENDAR-RULE-001: kept by you and included automatically are counted apart; nothing is kept here
    assert re.search(r"rows: Kept by you 0, Included automatically \(not reviewed by you\) \d+, Needs your review \d+", rest)
    assert re.search(r"calendar: 0 kept by you, \d+ included automatically", rest)
    spend = run_json("ptgx-r5")["run"]["spend_usd"]
    assert f"cost: ${spend:.4f}; accepted rows: 0; cost per accepted row: not computed" in rest
    # Job 50b: ptgx-r5's committed accuracy.json predates input binding: no counts, a plain re-score reason
    assert "Accuracy: not shown. Accuracy file predates input binding; re-score" in rest
    assert "Muse Spark — events" not in rest and "Unsealed" not in rest and "turns:" not in rest


def test_the_demo_can_show_the_sample_run_and_says_it_is_sample_data(capsys):
    code, out, _ = say(capsys, "--no-serve", "--run", "sample")
    assert code == 0 and "Unsealed (author key): Muse Spark — values 69/69 · planted 12/12" in out
    assert "This run is sample data." in out and "[sample]" in out


def test_a_run_with_no_saved_scoring_says_so(capsys):
    _, out, _ = say(capsys, "--no-serve", "--run", "ptgx-r4a")
    assert "Accuracy: no scoring is saved for this run." in out and "Unsealed" not in out and "Sealed set" not in out


def test_a_scoring_that_does_not_hold_its_hashes_is_not_shown_and_the_reason_is(capsys, runs_dir):
    f = runs_dir / "sample" / export.FILE_NAME
    doc = json.loads(f.read_text())
    doc["models"]["Muse Spark"]["values"]["found"] = 0
    f.write_text(json.dumps(doc))
    _, out, _ = say(capsys, "--no-serve", "--run", "sample", runs_dir=runs_dir)
    assert "Accuracy: not shown. This run's accuracy.json does not match the hash recorded in it" in out
    assert "values 0/" not in out and "Muse Spark — values" not in out


def test_the_label_is_sealed_only_when_the_key_says_sealed(capsys, runs_dir):
    f = runs_dir / "sample" / export.FILE_NAME
    doc = json.loads(f.read_text())
    doc["key"]["sealed"] = True
    doc["content_sha256"] = export.content_hash(doc)
    f.write_text(json.dumps(doc))
    _, out, _ = say(capsys, "--no-serve", "--run", "sample", runs_dir=runs_dir)
    assert "Sealed set: Muse Spark — values 69/69" in out and "Key: sealed, complete;" in out


def test_an_unknown_run_is_refused_naming_the_saved_ones(capsys):
    code, out, err = say(capsys, "--no-serve", "--run", "nope")
    assert code == 2 and not out and "There is no run 'nope'. Saved runs: demo, ptgx-r4a, ptgx-r5" in err


def test_it_serves_the_app_on_this_machine_only_on_the_documented_port_and_the_app_lists_the_runs(capsys):
    seen = []
    code, out, _ = say(capsys, serve=lambda app, host, port: seen.append((app, host, port)))
    assert code == 0 and len(seen) == 1
    app, host, port = seen[0]
    assert isinstance(app, FastAPI) and (host, port) == ("127.0.0.1", 8777)
    assert "Serving the app at http://127.0.0.1:8777/  (this machine only; Ctrl-C stops it)" in out
    ids = [r["id"] for r in TestClient(app).get("/api/runs").json()]
    assert "sample" in ids and "ptgx-r5" not in ids   # runs/LISTED.json: the list; unlisted runs stay served
    assert TestClient(app).get("/runs/sample/accuracy.json").json()["key"]["sealed"] is False
    legacy = TestClient(app).get("/runs/ptgx-r5/accuracy.json")
    assert legacy.status_code == 409 and "re-score" in legacy.json()["detail"] and "models" not in legacy.text


def test_another_port_is_taken_and_there_is_no_way_to_bind_elsewhere(capsys):
    seen = []
    say(capsys, "--port", "8800", serve=lambda app, host, port: seen.append((host, port)))
    assert seen == [("127.0.0.1", 8800)]
    with pytest.raises(SystemExit):
        demo.main(["--host", "0.0.0.0", "--no-serve"])


def test_nothing_is_served_with_no_serve_and_no_key_or_model_is_touched(capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the demo read the key file")

    monkeypatch.setattr(read_table, "load_env_file", boom)
    seen = []
    code, out, _ = say(capsys, "--no-serve", serve=lambda *a: seen.append(a))
    assert code == 0 and not seen and "Serving" not in out


def test_the_readme_says_how_to_run_the_demo_without_a_key():
    # public README (4 Nov): the README gives the command; the six-step walkthrough lives in the build notes
    readme = (REPO / "README.md").read_text()
    assert ".venv/bin/python -m runs.demo" in readme and "docs/BUILD_NOTES_PAGE_READER.md" in readme
    text = (REPO / "docs" / "BUILD_NOTES_PAGE_READER.md").read_text()
    section = text.split("## Run it without a key\n", 1)[1].split("\n## ", 1)[0]
    assert ".venv/bin/python -m runs.demo" in section and "127.0.0.1:8777/#/ptgx-r5/audit" in section
    assert "proof-reader-docs" not in text and "proof-reader-docs" not in (REPO / "server/README.md").read_text()
    walk = [l for l in section.splitlines() if l[:2] in {f"{n}." for n in range(1, 7)}]
    assert len(walk) == 6 and "ptgx-r5" in walk[0] and "twelve-4" in walk[4]


def test_the_committed_accuracy_files_hold_their_hashes_after_the_rebuild():
    data, problem = export.read_if_intact(RUNS / "sample")
    assert problem is None and data is not None, problem
    data, problem = export.read_if_intact(RUNS / "ptgx-r5")      # legacy: refused, never upgraded
    assert data is None and "re-score" in problem


# ---------------------------------------------------------------- Job 35: reports for every run; the app opens on the printed run


def test_every_committed_run_has_a_pdf_and_a_word_report_that_open():
    import docx

    for run_json_file in sorted(RUNS.glob("*/run.json")):
        folder = run_json_file.parent / "report"
        pdfs, docxs = sorted(folder.glob("*.pdf")), sorted(folder.glob("*.docx"))
        assert len(pdfs) == 1 and len(docxs) == 1, run_json_file.parent.name
        assert pdfs[0].read_bytes().startswith(b"%PDF")
        assert docx.Document(str(docxs[0])).paragraphs


def test_the_default_run_setting_is_served_and_the_demo_prints_where_to_open(runs_dir, capsys):
    from server.app import create_app

    # CVA 2026-11-03: the New Run box (research mode) shows the full research brief
    fields = {"research_instructions_default": research_instructions.RESEARCH_DEFAULT,
              "research_instructions_max_length": 4000, "turn_limit_per_company_default": 200}
    assert TestClient(create_app(runs_dir=runs_dir)).get("/api/settings").json() == {"default_run": None, **fields}
    assert TestClient(create_app(runs_dir=runs_dir, default_run="nope")).get("/api/settings").json() == {
        "default_run": None, **fields}
    assert TestClient(create_app(runs_dir=runs_dir, default_run="ptgx-r4a")).get("/api/settings").json() == {
        "default_run": "ptgx-r4a", **fields}

    seen = {}
    demo.main(["--run", "ptgx-r4a", "--runs-dir", str(runs_dir)],
              serve=lambda app, host, port: seen.update(body=TestClient(app).get("/api/settings").json()))
    assert seen["body"] == {"default_run": "ptgx-r4a", **fields}
    assert "Open http://127.0.0.1:8777/#/ptgx-r4a/audit" in capsys.readouterr().out

    demo.main(["--runs-dir", str(runs_dir)],
              serve=lambda app, host, port: seen.update(body=TestClient(app).get("/api/settings").json()))
    assert seen["body"] == {"default_run": "ptgx-r5", **fields}
    assert "Open http://127.0.0.1:8777/#/ptgx-r5/audit" in capsys.readouterr().out
