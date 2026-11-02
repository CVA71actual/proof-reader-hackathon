"""Job 9: the twelve-company run on the server side: pages come from the sources index, a run is labelled from its
choices, and `python -m runs.twelve` runs it from the command line by the same code path as the endpoint.

The index here is a fake of three pages (two companies) pointing at the repo's fixture pages; every model is a fake. No
test reaches the network.
"""

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import read_events, read_table
from capture.capture_source import load_entry
from report.build import build as build_report
from runs import build_entry, build_run, sources, twelve
from runs.calendar import calendar_entries
from runs.state import current_state
from server import jobs
from server.app import create_app
from tests.conftest import FIXTURES, REPO
from tests.test_escalation import FakeWonder
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond

RUN_SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))
PIN = read_events.PIN


def answer(prompt, n):
    """Reads the page the way the offline readers do, for whichever company the prompt names."""
    m = re.search(r"company's SEC filing: (.+?) \(ticker (\w+)\)", prompt)
    if not m:
        return json.dumps(read_table.build_fake_table(read_table.load_words()))
    entry = {**load_entry(PIN), "company": m[1], "ticker": m[2]}
    return json.dumps({"events": read_events.build_fake_events(read_events.load_words(), entry)})


def record(base: dict, **kw) -> dict:
    out = {k: v for k, v in base.items() if k not in ("entries",)}
    out["page_file"], out["words_file"] = str(FIXTURES / base["page_file"]), str(FIXTURES / base["words_file"])
    return {**out, **kw}


def index_doc():
    ptgx = load_entry(PIN)
    ions = {k: v for k, v in json.loads((FIXTURES / "MANIFEST.json").read_text()).items() if k != "entries"}
    return {"index_version": 1, "companies": {
        "AAA": {"pages": [
            record(ptgx, ticker="AAA", company="Alpha Bio, Inc.", accession="0000000001-26-000001", id="aaa-pr-p3"),
            record(ions, ticker="AAA", company="Alpha Bio, Inc.", kind="table", id="aaa-ars-p95")]},
        "BBB": {"pages": [
            record(ptgx, ticker="BBB", company="Beta Therapeutics, Inc.", accession="0000000002-26-000002",
                   id="bbb-pr-p3")]},
    }}


@pytest.fixture
def index_file(tmp_path):
    p = tmp_path / "sources_index.json"
    p.write_text(json.dumps(index_doc()))
    return p


@pytest.fixture
def index(index_file):
    return sources.load_index(index_file)


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def client(runs_dir, index, first=None, second="fake", wonder="fake", **kw):
    first = first or FakeReader(answer=answer)
    second = FakeSecond(answer=answer) if second == "fake" else second
    wonder = FakeWonder() if wonder == "fake" else wonder
    return TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=first, second_reader=second,
                                 wonder_reader=wonder, sam_reader=None, index=index, background=False, **kw)), first


CHOICES = {"companies": ["AAA", "BBB"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
           "spending_limit_usd": None, "note_for_agent": None}


def start(c, **choices):
    r = c.post("/api/runs/new", json={**CHOICES, **choices})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def run_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())


# ---------------------------------------------------------------- the index


def test_the_index_file_is_read_company_by_company_with_every_page_in_order(index):
    assert index.tickers == ["AAA", "BBB"]
    aaa = index.pages("AAA")
    assert [(p["kind"], p["source_id"]) for p in aaa] == [("events", "src-aaa-pr-p3"), ("table", "src-aaa-ars-p95")]
    assert aaa[0]["record"]["file_stem"] == "aaa-pr-p3" and aaa[0]["record"]["ticker"] == "AAA"
    assert index.pages("NOPE") == []


def test_a_flat_list_and_a_list_of_pages_are_read_too():
    pages = [p for c in index_doc()["companies"].values() for p in c["pages"]]
    assert sources.parse_index({"pages": pages}).tickers == ["AAA", "BBB"]
    assert sources.parse_index(pages).tickers == ["AAA", "BBB"]


def test_missing_fields_are_named_and_an_id_is_derived_when_there_is_none():
    p = index_doc()["companies"]["BBB"]["pages"][0]
    bare = {k: v for k, v in p.items() if k not in ("id", "kind", "source_id", "file_stem")}
    rec = sources.parse_index([bare]).pages("BBB")[0]["record"]
    assert rec["id"] == "bbb-0000000002-26-000002-99.1-p3" and rec["kind"] == "events" and rec["source_id"] == f"src-{rec['id']}"
    with pytest.raises(sources.SourcesError, match="the page needs page_file, words_sha256"):
        sources.parse_index([{k: v for k, v in bare.items() if k not in ("words_sha256", "page_file")}])
    with pytest.raises(sources.SourcesError, match="the kind must be one of"):
        sources.parse_index([{**p, "kind": "chart"}])
    with pytest.raises(sources.SourcesError, match="used twice"):
        sources.parse_index([p, p])
    with pytest.raises(sources.SourcesError, match="whole number"):
        sources.parse_index([{**p, "page_number": "3"}])


def test_relative_paths_are_found_beside_the_index(tmp_path):
    folder = tmp_path / "idx"
    folder.mkdir()
    ptgx = load_entry(PIN)
    for name in (ptgx["page_file"], ptgx["words_file"]):
        shutil.copy(FIXTURES / name, folder / name)
    doc = {"pages": [{**ptgx, "ticker": "CCC", "company": "Gamma", "id": "ccc"}]}
    rec = sources.parse_index(doc, folder).pages("CCC")[0]["record"]
    assert Path(rec["page_path"]) == folder / ptgx["page_file"] and Path(rec["words_path"]).is_file()


def test_with_no_index_file_the_built_in_index_is_the_two_old_pages_and_the_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "INDEX_PATH", tmp_path / "absent.json")
    idx = sources.load_index()
    assert idx.tickers == ["PTGX", "IONS", "PTGX-SCAN"]
    assert [(p["kind"], p["source_id"]) for t in idx.tickers for p in idx.pages(t)] == [
        ("events", build_entry.SOURCE_ID), ("table", build_run.IONS_SOURCE_ID),
        ("scan_events", "src-ptgx-8k-2026-08-05-ex991-scanned")]
    with pytest.raises(sources.SourcesError, match="There is no captured-pages file at"):
        sources.load_index(tmp_path / "also-absent.json")


def test_a_file_that_is_not_json_or_holds_nothing_is_refused_in_plain_words(tmp_path):
    bad = tmp_path / "i.json"
    bad.write_text("{not json")
    with pytest.raises(sources.SourcesError, match="is not valid JSON"):
        sources.load_index(bad)
    with pytest.raises(sources.SourcesError, match="holds no companies and no pages"):
        sources.parse_index({"something": 1})


# ---------------------------------------------------------------- capture on first use


def test_the_built_in_pages_are_captured_exactly_as_the_hard_coded_builders_did(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "INDEX_PATH", tmp_path / "absent.json")
    idx = sources.load_index()
    ptgx_rec = idx.pages("PTGX")[0]["record"]
    a, b = tmp_path / "a", tmp_path / "b"
    src, words, consumed = sources.capture_page(ptgx_rec, a)
    old_src, old_words, old_consumed = build_entry.ptgx_source(load_entry(PIN), b)
    assert src == old_src and words == old_words
    assert consumed == old_consumed == {"file": "pages/p3.words.json", "sha256": src["pages"][0]["words_sha256"], "kind": "captured"}
    assert (a / "pages" / "p3.png").read_bytes() == (b / "pages" / "p3.png").read_bytes()
    ions_rec = idx.pages("IONS")[0]["record"]
    manifest = {k: v for k, v in json.loads((FIXTURES / "MANIFEST.json").read_text()).items() if k != "entries"}
    src2, words2, _consumed2 = sources.capture_page(ions_rec, a)
    old2, oldw2, _old_consumed2 = build_run.ions_source(manifest, b)
    assert src2 == old2 and words2 == oldw2


def test_a_page_is_captured_into_the_runs_folder_with_hashes_under_its_own_name(index, tmp_path):
    rec = index.pages("AAA")[0]["record"]
    src, words, consumed = sources.capture_page(rec, tmp_path)
    page = src["pages"][0]
    assert page["image"] == "pages/aaa-pr-p3.png" and page["words"] == "pages/aaa-pr-p3.words.json"
    assert page["image_sha256"] == hashlib.sha256((tmp_path / page["image"]).read_bytes()).hexdigest()
    assert page["words_sha256"] == hashlib.sha256((tmp_path / page["words"]).read_bytes()).hexdigest()
    assert src["id"] == "src-aaa-pr-p3" and src["published_date"] == rec["filing_date"]
    assert src["rendered_from"]["format"] == "html" and words["words"]
    assert consumed["file"] == page["words"] and consumed["sha256"] == page["words_sha256"]


def test_a_page_or_words_file_that_differs_from_the_index_hash_is_refused_with_the_reason(index, tmp_path):
    rec = dict(index.pages("AAA")[0]["record"])
    with pytest.raises(sources.SourcesError, match=r"The page file for AAA page 3 .* does not match the hash"):
        sources.capture_page({**rec, "page_sha256": "0" * 64}, tmp_path)
    with pytest.raises(sources.SourcesError, match=r"The words file for AAA page 3 .* does not match the hash"):
        sources.capture_page({**rec, "words_sha256": "0" * 64}, tmp_path)
    with pytest.raises(sources.SourcesError, match="is missing"):
        sources.capture_page({**rec, "page_path": str(tmp_path / "nope.pdf")}, tmp_path)


# ---------------------------------------------------------------- the run


def test_every_indexed_page_of_each_chosen_company_is_read_and_nothing_hard_coded_is(runs_dir, index):
    c, first = client(runs_dir, index)
    rid = start(c, time_window_months=6)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    assert run["run"]["status"] == "done", run["run"]["status_reason"]
    assert [s["id"] for s in run["sources"]] == ["src-aaa-pr-p3", "src-aaa-ars-p95", "src-bbb-pr-p3"]
    assert len(run["tables"]) == 1 and run["tables"][0]["source_id"] == "src-aaa-ars-p95"
    assert {r["ticker"] for r in run["rows"] if r["source_id"] != "src-aaa-ars-p95"} == {"AAA", "BBB"}
    assert {r["company"] for r in run["rows"]} == {"Alpha Bio, Inc.", "Beta Therapeutics, Inc."}
    for src in run["sources"]:
        page = src["pages"][0]
        assert (runs_dir / rid / page["image"]).is_file() and (runs_dir / rid / page["words"]).is_file()
    assert len(first.prompts) == 3, "one first read per page: two event pages, one table page"


def test_event_pages_go_through_both_readers_and_a_table_page_through_the_table_reader(runs_dir, index):
    c, first = client(runs_dir, index)
    second = FakeSecond(answer=answer)
    c2, _ = client(runs_dir, index, first=first, second=second)
    rid = start(c2)
    run = run_of(runs_dir, rid)
    assert run["run"]["second_reader"]["status"] == "read"
    assert len(second.prompts) == 3, "the second reader read all three pages"
    assert sum("Protagonist" not in p and "ticker" in p for p in first.prompts) == 2
    assert any("The page holds a financial table" in p for p in first.prompts)
    assert all(t["source_id"] for t in run["tables"])


def test_the_label_is_made_from_the_choices_and_the_number_is_the_next_in_the_sequence(runs_dir, index):
    c, _ = client(runs_dir, index)
    a = run_of(runs_dir, start(c, time_window_months=6))["run"]
    b = run_of(runs_dir, start(c, companies=["BBB"], time_window_months=1))["run"]
    d = run_of(runs_dir, start(c, companies=["AAA"]))["run"]
    assert (a["label"], a["number"]) == ("2 companies · 6 months", 1)
    assert (b["label"], b["number"]) == ("BBB · 6 months", 2)
    assert (d["label"], d["number"]) == ("AAA · 6 months", 3)
    assert jobs.label_for(9, {"companies": list("ABCDEFGHIJKL"), "time_window_months": 3}) == "12 companies · 3 months"


def test_all_means_every_company_in_the_index_and_is_recorded_as_the_list_it_stands_for(runs_dir, index):
    c, _ = client(runs_dir, index)
    run = run_of(runs_dir, start(c, companies=["all"]))
    assert run["run"]["choices"]["companies"] == ["AAA", "BBB"] and run["run"]["label"] == "2 companies · 6 months"
    assert len(run["sources"]) == 3


def test_all_leaves_out_the_scanned_variant_of_the_built_in_pages(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "INDEX_PATH", tmp_path / "absent.json")
    assert jobs.validate_choices({**CHOICES, "companies": ["all"]})["companies"] == ["PTGX", "IONS"]


def test_an_unknown_company_is_refused_naming_what_is_indexed(runs_dir, index):
    c, first = client(runs_dir, index)
    r = c.post("/api/runs/new", json={**CHOICES, "companies": ["AAA", "ZZZ"]})
    assert r.status_code == 422 and "No source has been captured for ZZZ yet. Captured so far: AAA, BBB." == r.json()["detail"]
    assert not first.prompts and sorted(p.name for p in runs_dir.iterdir()) == []


def test_a_page_that_no_longer_matches_its_hash_is_skipped_with_the_reason_and_the_run_goes_on(
        runs_dir, index_file, tmp_path):
    doc = index_doc()
    doc["companies"]["BBB"]["pages"][0]["page_sha256"] = "0" * 64
    index_file.write_text(json.dumps(doc))
    c, first = client(runs_dir, sources.load_index(index_file))
    rid = start(c)
    run = run_of(runs_dir, rid)
    m = run["run"]
    assert m["status"] == "done" and m["finished_at"], "one bad page does not end the run"
    assert [s["id"] for s in run["sources"]] == ["src-aaa-pr-p3", "src-aaa-ars-p95"], "AAA read; BBB's page was not"
    [skipped] = m["skipped_pages"]
    assert (skipped["source_id"], skipped["ticker"]) == ("src-bbb-pr-p3", "BBB")
    assert "does not match the hash the captured-pages file recorded" in skipped["reason"]
    assert "1 page skipped (see skipped_pages)" in m["status_reason"] and len(first.prompts) == 2
    RUN_SCHEMA.validate(run)
    assert json.loads((runs_dir / rid / "job.json").read_text())["skipped"]["src-bbb-pr-p3"]["ticker"] == "BBB"


def test_the_turn_limit_pauses_between_pages_and_resume_reads_only_the_pages_left(runs_dir, index, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 2)
    c, first = client(runs_dir, index, second=None, wonder=None)
    rid = start(c)
    assert run_of(runs_dir, rid)["run"]["status"] == "paused_turn_limit" and len(first.prompts) == 2
    assert c.post(f"/api/runs/{rid}/resume").status_code == 202
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "done" and len(first.prompts) == 3 and m["turns_total"] == 3


def test_the_spending_limit_holds_across_the_pages_of_many_companies(runs_dir, index):
    c, first = client(runs_dir, index, first=FakeReader(cost=0.30, answer=answer), second=None, wonder=None)
    rid = start(c, spending_limit_usd=0.5)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and m["spend_usd"] == pytest.approx(0.60) and len(first.prompts) == 2
    assert m["status_reason"].startswith("Stopped at the spending limit")


def test_future_rows_are_retained_for_the_later_section(runs_dir, index, monkeypatch):
    import datetime
    monkeypatch.setattr(jobs, "today", lambda: datetime.date(2026, 10, 28))
    monkeypatch.setattr(jobs, "now_utc", lambda: "2026-10-28T12:00:00+00:00")
    c, _ = client(runs_dir, index, second=None, wonder=None)
    run = run_of(runs_dir, start(c, time_window_months=6))      # to 2027-04-28: 2H 2027 is out, Q1 2027 is in
    assert not any(r["group"] == "removed" for r in run["rows"])
    assert run["exclusions"] == []


def test_the_calendar_requires_explicit_keeps_for_checked_rows(runs_dir, index):
    c, _ = client(runs_dir, index)
    rid = start(c)
    run = run_of(runs_dir, rid)
    state = c.get(f"/api/runs/{rid}").json()["state"]
    entries = c.get(f"/api/runs/{rid}/calendar").json()["entries"]
    assert entries == [] and all(state["targets"][r["id"]]["needs_attention"] for r in run["rows"])
    accepted = {r["id"] for r in run["rows"] if state["targets"][r["id"]]["group"] in ("kept", "corrected")}
    for target in accepted:
        reply = c.post(f"/api/runs/{rid}/decisions", json={"target": target, "action": "keep"})
        assert reply.status_code == 200, reply.text
    entries = c.get(f"/api/runs/{rid}/calendar").json()["entries"]
    assert {e["id"] for e in entries} == accepted
    assert entries and {e["ticker"] for e in entries} == {"AAA", "BBB"}
    assert all(e["group"] in ("kept", "corrected") for e in entries)


def test_the_report_builds_for_a_many_company_run(runs_dir, index, tmp_path):
    c, _ = client(runs_dir, index)
    rid = start(c, time_window_months=6)
    built = build_report(rid, run_dir=runs_dir / rid, out=tmp_path / "rep")
    assert built["pdf"].read_bytes()[:5] == b"%PDF-" and built["docx"].stat().st_size > 1000


# ---------------------------------------------------------------- the command


def readers():
    return FakeReader(answer=answer), FakeSecond(answer=answer), FakeWonder(), None


def test_the_command_prints_checks_separately_and_no_accepted_cost_until_a_human_keeps(
        runs_dir, index_file, capsys):
    code = twelve.main(["--companies", "all", "--window", "6", "--limit", "5.00", "--runs-dir", str(runs_dir),
                        "--index", str(index_file)], readers=readers())
    out = capsys.readouterr().out
    run = run_of(runs_dir, "run-1")
    assert code == 0 and run["run"]["status"] == "done" and run["run"]["label"] == "2 companies · 6 months"
    assert run["run"]["choices"] == {"companies": ["AAA", "BBB"], "event_types": list(read_events.EVENT_TYPES),
                                     "time_window_months": 6, "spending_limit_usd": 5.0, "note_for_agent": None,
                                         "reader_tools": False}
    assert "Created run-1: 2 companies · 6 months (2 companies)" in out
    assert "run-1 · 2 companies · 6 months: done" in out and "pages read: 3" in out
    # GROUP-AXIS-001: every new row is a proposal, never accepted by checks.
    assert "check results:" in out
    assert re.search(r"rows: Accepted by you 0, Proposed — needs review \d+, Excluded \d+", out)
    assert re.search(r"values: kept \d+", out) and "needs attention:" in out
    assert re.search(r"mistakes caught by code \d+, second reader \d+, person \d+; excluded by code \d+", out)
    assert "calendar: 0 accepted rows" in out
    assert re.search(r"cost: \$\d+\.\d{4}; accepted rows: 0; cost per accepted row: not computed", out)
    assert "second reader: read (image)" in out
    assert (runs_dir / "run-1" / "report").is_dir() and "report:" in out
    assert len(list((runs_dir / "run-1" / "report").glob("*.pdf"))) == 1


def test_the_command_is_the_same_code_path_as_the_endpoint(runs_dir, index_file, index, capsys):
    twelve.main(["--companies", "AAA,BBB", "--window", "6", "--runs-dir", str(runs_dir), "--index", str(index_file),
                 "--no-report"], readers=readers())
    cmd = run_of(runs_dir, "run-1")
    c, _ = client(runs_dir, index)
    api = run_of(runs_dir, start(c, time_window_months=6))
    strip = lambda r: ([(x["id"], x["status"], x["group"], x["ticker"], x["date_text"]) for x in r["rows"]],
                       [s["id"] for s in r["sources"]], r["run"]["label"], r["run"]["turns_total"])
    assert strip(cmd) == strip(api)


def test_the_command_resumes_through_turn_limit_pauses(runs_dir, index_file, capsys):
    code = twelve.main(["--runs-dir", str(runs_dir), "--index", str(index_file), "--turns", "2", "--no-second",
                        "--no-wonder", "--no-report"], readers=readers())
    out = capsys.readouterr().out
    assert code == 0 and "paused_turn_limit: Stopped at the turn limit (2 turns)" in out and "resuming (1)" in out
    m = run_of(runs_dir, "run-1")["run"]
    assert m["status"] == "done" and m["resumes"] >= 1 and m["turns_total"] == 3


def test_the_command_stops_where_a_limit_stops_it_and_says_so(runs_dir, index_file, capsys):
    code = twelve.main(["--limit", "0.5", "--runs-dir", str(runs_dir), "--index", str(index_file), "--no-second",
                        "--no-wonder"], readers=(FakeReader(cost=0.30, answer=answer), None, None, None))
    out = capsys.readouterr().out
    assert code == 1 and "needs_input (Stopped at the spending limit" in out and "report:" not in out


def test_the_command_refuses_what_the_server_refuses(runs_dir, index_file, capsys):
    assert twelve.main(["--companies", "ZZZ", "--runs-dir", str(runs_dir), "--index", str(index_file)],
                       readers=readers()) == 2
    assert "No source has been captured for ZZZ yet" in capsys.readouterr().err
    assert twelve.main(["--window", "0", "--runs-dir", str(runs_dir), "--index", str(index_file)],
                       readers=readers()) == 2
    assert twelve.main(["--index", str(runs_dir / "none.json"), "--runs-dir", str(runs_dir)], readers=readers()) == 2
    assert not list(runs_dir.iterdir())


def test_a_second_command_run_is_the_next_run_and_the_first_is_not_touched(runs_dir, index_file, capsys):
    twelve.main(["--runs-dir", str(runs_dir), "--index", str(index_file), "--no-report"], readers=readers())
    before = (runs_dir / "run-1" / "run.json").read_bytes()
    twelve.main(["--runs-dir", str(runs_dir), "--index", str(index_file), "--no-report"], readers=readers())
    assert (runs_dir / "run-1" / "run.json").read_bytes() == before
    assert run_of(runs_dir, "run-2")["run"]["number"] == 2


def test_the_committed_runs_are_not_changed_by_any_of_this():
    for name in ("sample", "demo", "ptgx-r4a", "ptgx-r5"):
        run = json.loads((REPO / "runs" / name / "run.json").read_text())
        RUN_SCHEMA.validate(run)


def test_the_committed_sample_index_loads_and_a_run_reads_both_of_its_pages(runs_dir, capsys):
    path = REPO / "runs" / "sources_index.sample.json"
    idx = sources.load_index(path)
    assert idx.tickers == ["PTGX", "IONS"]
    assert twelve.main(["--runs-dir", str(runs_dir), "--index", str(path), "--no-report"],
                       readers=(FakeReader(), FakeSecond(), FakeWonder(), None)) == 0
    run = run_of(runs_dir, "run-1")
    RUN_SCHEMA.validate(run)
    assert [s["id"] for s in run["sources"]] == ["src-ptgx-8k-ex991-p3", "src-ions-ars-p95"]
    assert run["rows"] and run["tables"]


# ---------------------------------------------------------------- duplicate file stems (Codex P2 on #37)


def two_records(stem_a, stem_b):
    docs = index_doc()["companies"]
    a = {**docs["AAA"]["pages"][0], "id": "first", "file_stem": stem_a}
    b = {**docs["BBB"]["pages"][0], "id": "second", "file_stem": stem_b}
    return [a, b]


def test_two_distinct_pages_with_the_same_file_stem_are_refused_at_load():
    """Codex's case: two valid records, different ids and sources, one stem. Both would write pages/shared.png."""
    with pytest.raises(sources.SourcesError, match=r"Page 2 of the captured-pages file \(BBB\): the file stem shared is already used by "
                                                   r"page first, so their captured files would overwrite each other"):
        sources.parse_index(two_records("shared", "shared"))


@pytest.mark.parametrize("a,b", [("Shared", "shared"), ("a b", "a-b"), ("p3", "P3"), ("x/y", "x-y")])
def test_stems_that_fold_to_one_file_are_the_same_stem(a, b):
    """Case (a macOS file system folds it) and the characters the loader replaces make two stems one file name."""
    with pytest.raises(sources.SourcesError, match="would overwrite each other"):
        sources.parse_index(two_records(a, b))


def test_distinct_stems_load_and_the_default_stem_is_the_page_id_so_defaults_never_collide():
    idx = sources.parse_index(two_records("one", "two"))
    assert [p["record"]["file_stem"] for t in idx.tickers for p in idx.pages(t)] == ["one", "two"]
    recs = two_records("", "")
    for r in recs:
        r.pop("file_stem")
    idx2 = sources.parse_index(recs)
    assert [p["record"]["file_stem"] for t in idx2.tickers for p in idx2.pages(t)] == ["first", "second"]


def test_the_file_the_second_page_writes_is_not_the_first_pages_file(tmp_path):
    """What the refusal prevents: with one stem the second capture changes the first source's recorded image hash."""
    recs = sources.parse_index(two_records("one", "two"))
    first, second = (recs.pages(t)[0]["record"] for t in ("AAA", "BBB"))
    a, _, _ = sources.capture_page(first, tmp_path)
    b, _, _ = sources.capture_page(second, tmp_path)
    assert a["pages"][0]["image"] != b["pages"][0]["image"]
    for src in (a, b):
        page = src["pages"][0]
        assert hashlib.sha256((tmp_path / page["image"]).read_bytes()).hexdigest() == page["image_sha256"]
    clash = {**second, "file_stem": "one"}                  # the same two pages written under one stem
    c, _, _ = sources.capture_page(clash, tmp_path)
    assert c["pages"][0]["image"] == a["pages"][0]["image"], "the collision the loader now refuses"


def test_the_index_file_with_a_duplicate_stem_is_refused_when_loaded_from_disk(tmp_path):
    f = tmp_path / "i.json"
    f.write_text(json.dumps({"pages": two_records("same", "same")}))
    with pytest.raises(sources.SourcesError, match="file stem same is already used"):
        sources.load_index(f)


def test_the_built_in_index_has_a_stem_for_every_page_and_the_scan_has_its_own(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "INDEX_PATH", tmp_path / "absent.json")
    idx = sources.load_index()
    stems = [idx.pages(t)[0]["record"]["file_stem"] for t in idx.tickers]
    assert stems == ["p3", "p95", "p3-scanned"] and len(set(stems)) == 3


def test_a_run_of_a_page_and_its_scan_keeps_two_sets_of_files_and_both_hashes_hold(runs_dir, monkeypatch, tmp_path):
    from tests.test_scan import FakeSam, TWO_BOXES
    monkeypatch.setattr(sources, "INDEX_PATH", tmp_path / "absent.json")
    c = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(answer=answer),
                              second_reader=None, wonder_reader=None, sam_reader=FakeSam(TWO_BOXES), background=False))
    rid = c.post("/api/runs/new", json={**CHOICES, "companies": ["PTGX", "PTGX-SCAN"]}).json()["id"]
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "done", run["run"]["status_reason"]
    images = [s["pages"][0]["image"] for s in run["sources"]]
    assert sorted(images) == ["pages/p3-scanned.png", "pages/p3.png"]
    assert c.get(f"/api/runs/{rid}").status_code == 200, "every page file still matches the hash run.json recorded"


# ---------------------------------------------------------------- a company the index lists with no page; a run kept under a name


def index_with_empty_company():
    doc = index_doc()
    doc["companies"]["EEE"] = {"pages": []}
    return doc


def test_a_company_listed_with_no_page_is_recorded_as_empty_not_as_unknown():
    idx = sources.parse_index(index_with_empty_company())
    assert idx.tickers == ["AAA", "BBB"] and idx.empty == ["EEE"]
    assert sources.parse_index(index_doc()).empty == []


def test_all_leaves_out_a_company_with_no_page_and_does_not_stop_at_it(runs_dir, tmp_path):
    f = tmp_path / "i.json"
    f.write_text(json.dumps(index_with_empty_company()))
    c, first = client(runs_dir, sources.load_index(f), second=None, wonder=None)
    run = run_of(runs_dir, start(c, companies=["all"]))
    assert run["run"]["choices"]["companies"] == ["AAA", "BBB"] and run["run"]["label"] == "2 companies · 6 months"
    assert run["run"]["status"] == "done" and len(first.prompts) == 3


def test_a_company_with_no_page_asked_for_by_name_is_refused_naming_it(runs_dir, tmp_path):
    f = tmp_path / "i.json"
    f.write_text(json.dumps(index_with_empty_company()))
    c, first = client(runs_dir, sources.load_index(f))
    for companies in (["EEE"], ["AAA", "EEE"]):
        r = c.post("/api/runs/new", json={**CHOICES, "companies": companies})
        assert r.status_code == 422
        assert r.json()["detail"] == "The captured-pages file lists EEE but holds no page for it yet, so there is nothing to read."
    r = c.post("/api/runs/new", json={**CHOICES, "companies": ["ZZZ"]})
    assert r.json()["detail"].startswith("No source has been captured for ZZZ yet."), "still the plain unknown case"
    assert not first.prompts and not list(runs_dir.iterdir())


def test_the_command_says_which_companies_it_left_out(runs_dir, tmp_path, capsys):
    f = tmp_path / "i.json"
    f.write_text(json.dumps(index_with_empty_company()))
    twelve.main(["--runs-dir", str(runs_dir), "--index", str(f), "--no-report", "--no-second", "--no-wonder"],
                readers=(FakeReader(answer=answer), None, None, None))
    assert "left out, no page indexed for EEE" in capsys.readouterr().out
    twelve.main(["--companies", "AAA", "--runs-dir", str(runs_dir), "--index", str(f), "--no-report", "--no-second",
                 "--no-wonder"], readers=(FakeReader(answer=answer), None, None, None))
    assert "left out" not in capsys.readouterr().out


def test_a_run_can_be_kept_under_a_name_and_the_number_still_comes_from_the_sequence(runs_dir):
    choices = jobs.validate_choices(CHOICES, sources.parse_index(index_doc()))
    a = jobs.create_run(runs_dir, choices)
    b = jobs.create_run(runs_dir, choices, run_id="twelve-1")
    c = jobs.create_run(runs_dir, choices)
    assert [x["run"]["id"] for x in (a, b, c)] == ["run-1", "twelve-1", "run-3"]
    assert [x["run"]["number"] for x in (a, b, c)] == [1, 2, 3]
    assert (runs_dir / "twelve-1" / "run.json").is_file() and (runs_dir / "twelve-1" / "decisions.jsonl").read_text() == ""


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".hidden", "a b", "x" * 0 + "-dash", "twelve-1/../../etc"])
def test_a_run_name_that_is_not_a_plain_name_is_refused_and_nothing_is_made(runs_dir, bad):
    choices = jobs.validate_choices(CHOICES, sources.parse_index(index_doc()))
    with pytest.raises(ValueError, match="is not a plain name"):
        jobs.create_run(runs_dir, choices, run_id=bad)
    assert not list(runs_dir.iterdir())


def test_a_saved_run_is_never_overwritten_by_a_new_one_of_the_same_name(runs_dir):
    choices = jobs.validate_choices(CHOICES, sources.parse_index(index_doc()))
    jobs.create_run(runs_dir, choices, run_id="twelve-1")
    before = (runs_dir / "twelve-1" / "run.json").read_bytes()
    with pytest.raises(ValueError, match="a saved run is never overwritten"):
        jobs.create_run(runs_dir, choices, run_id="twelve-1")
    assert (runs_dir / "twelve-1" / "run.json").read_bytes() == before


def test_the_command_keeps_the_run_under_the_name_it_is_given(runs_dir, index_file, capsys):
    assert twelve.main(["--id", "twelve-1", "--runs-dir", str(runs_dir), "--index", str(index_file), "--no-report"],
                       readers=readers()) == 0
    run = run_of(runs_dir, "twelve-1")
    assert run["run"]["id"] == "twelve-1" and run["run"]["status"] == "done" and run["run"]["sample"] is False
    assert "Created twelve-1:" in capsys.readouterr().out
    assert twelve.main(["--id", "twelve-1", "--runs-dir", str(runs_dir), "--index", str(index_file)],
                       readers=readers()) == 2
    assert "never overwritten" in capsys.readouterr().err
    assert twelve.main(["--id", "../x", "--runs-dir", str(runs_dir), "--index", str(index_file)], readers=readers()) == 2


# ---------------------------------------------------------------- a page that breaks the run is skipped; the rest goes on


from capture.reader import Reply as _Reply


class Breaks(FakeReader):
    """A first reader whose calls fail for the prompts `bad` says yes to, `times` times (None: always)."""

    def __init__(self, bad, times=None, raises=False, **kw):
        super().__init__(answer=answer, **kw)
        self.bad, self.times, self.raises, self.failed = bad, times, raises, 0

    def call(self, prompt, image=None):
        if self.bad(prompt) and (self.times is None or self.failed < self.times):
            self.failed += 1
            self.prompts.append(prompt)
            if self.raises:
                raise RuntimeError("something broke on this page")
            return _Reply(None, "muse-spark-1.3", error="The model call did not succeed (APIError, status 503).")
        return super().call(prompt, image)


def beta(prompt):
    return "Beta Therapeutics" in prompt


def cmd(runs_dir, index_file, reader, *extra):
    return twelve.main(["--runs-dir", str(runs_dir), "--index", str(index_file), "--no-report", "--no-second",
                        "--no-wonder", *extra], readers=(reader, None, None, None))


def test_one_failure_is_retried_and_the_page_is_read(runs_dir, index_file, capsys):
    reader = Breaks(beta, times=1)
    assert cmd(runs_dir, index_file, reader) == 0
    out = capsys.readouterr().out
    run = run_of(runs_dir, "run-1")
    assert run["run"]["status"] == "done" and "skipped_pages" not in run["run"] and len(run["sources"]) == 3
    assert "failed: The model call did not succeed" in out and "resuming (1)" in out
    job = json.loads((runs_dir / "run-1" / "job.json").read_text())
    assert job["page_failures"] == {"src-bbb-pr-p3": 1} and "skipped" not in job


def test_a_page_that_stops_the_run_twice_is_skipped_and_the_rest_is_read(runs_dir, index_file, capsys):
    reader = Breaks(beta)                                   # every call for BBB's page fails
    assert cmd(runs_dir, index_file, reader) == 0
    out = capsys.readouterr().out
    run = run_of(runs_dir, "run-1")
    m = run["run"]
    assert m["status"] == "done" and [s["id"] for s in run["sources"]] == ["src-aaa-pr-p3", "src-aaa-ars-p95"]
    [skipped] = m["skipped_pages"]
    assert skipped["source_id"] == "src-bbb-pr-p3" and skipped["reason"].startswith("It stopped the run 2 times, the last time: ")
    assert "status 503" in skipped["reason"] and reader.failed == 2, "tried twice, then left"
    assert out.count("resuming") == 2 and "skipped pages: 1" in out and "BBB src-bbb-pr-p3: It stopped the run 2 times" in out
    RUN_SCHEMA.validate(run)


def test_a_page_that_raises_is_skipped_the_same_way_and_does_not_end_the_run(runs_dir, index_file, capsys):
    reader = Breaks(beta, raises=True)
    assert cmd(runs_dir, index_file, reader) == 0
    run = run_of(runs_dir, "run-1")
    assert run["run"]["status"] == "done" and len(run["sources"]) == 2
    assert "unexpected error (RuntimeError)" in run["run"]["skipped_pages"][0]["reason"]


def test_a_pages_that_come_after_a_broken_one_are_still_read(runs_dir, index_file, capsys):
    """AAA first, BBB broken, nothing after it here; so put the broken one first: index order is the run's order."""
    doc = index_doc()
    doc["companies"] = {"BBB": doc["companies"]["BBB"], "AAA": doc["companies"]["AAA"]}
    index_file.write_text(json.dumps(doc))
    assert cmd(runs_dir, index_file, Breaks(beta)) == 0
    run = run_of(runs_dir, "run-1")
    assert [s["id"] for s in run["sources"]] == ["src-aaa-pr-p3", "src-aaa-ars-p95"]
    assert run["run"]["skipped_pages"][0]["ticker"] == "BBB" and run["run"]["status"] == "done"


def test_a_skipped_page_stays_skipped_and_is_not_tried_again_on_a_later_resume(runs_dir, index, tmp_path):
    choices = jobs.validate_choices(CHOICES, index)
    jobs.create_run(runs_dir, choices)
    reader = Breaks(beta)
    for _ in range(3):
        jobs.run_job(runs_dir, "run-1", reader, None, None, None, None, index)
        status = run_of(runs_dir, "run-1")["run"]["status"]
        if status == "failed":
            jobs.resume_run(runs_dir, "run-1")
    assert status == "done" and reader.failed == 2
    before = reader.failed
    with pytest.raises(jobs.JobError):
        jobs.run_job(runs_dir, "run-1", reader, None, None, None, None, index)
    assert reader.failed == before


def test_a_limit_or_a_pause_is_not_counted_as_a_page_failure(runs_dir, index, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 1)
    c, first = client(runs_dir, index, second=None, wonder=None)
    rid = start(c)
    job = json.loads((runs_dir / rid / "job.json").read_text())
    assert run_of(runs_dir, rid)["run"]["status"] == "paused_turn_limit" and not job.get("page_failures")


def test_the_second_reader_failing_twice_on_a_page_keeps_that_pages_first_rows_and_goes_on(runs_dir, index, capsys):
    class DownSecond(FakeSecond):
        def call(self, prompt, image=None):
            self.prompts.append(prompt)
            return _Reply(None, "meta-models/muse-spark-open-1026", error="The model call did not succeed (APIError, status 503).")

    c, first = client(runs_dir, index, second=DownSecond(answer=answer), wonder=None)
    rid = start(c)
    assert run_of(runs_dir, rid)["run"]["status"] == "failed"
    for _ in range(12):
        if run_of(runs_dir, rid)["run"]["status"] != "failed":
            break
        c.post(f"/api/runs/{rid}/resume")
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "done" and len(run["sources"]) == 3, "every page's first read stands"
    assert {s["source_id"] for s in run["run"]["skipped_pages"]} == {"src-aaa-pr-p3", "src-aaa-ars-p95", "src-bbb-pr-p3"}
    assert all("The second reader" in s["reason"] for s in run["run"]["skipped_pages"])


# ---------------------------------------------------------------- pages/ holds exactly the pages run.json names


def _named(run):
    return {Path(p[k]).name for s in run["sources"] for p in s["pages"] for k in ("image", "words")}


def test_a_run_stopped_before_a_pages_first_read_leaves_no_files_for_it(runs_dir, index):
    c, first = client(runs_dir, index, first=FakeReader(cost=0.60, answer=answer), second=None, wonder=None)
    rid = start(c, spending_limit_usd=0.5)
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "needs_input" and len(run["sources"]) == 1 and len(first.prompts) == 1
    assert {f.name for f in (runs_dir / rid / "pages").iterdir()} == _named(run), "the second page was captured, then dropped"


def test_a_failed_run_and_a_finished_run_keep_exactly_the_pages_they_name(runs_dir, index_file, capsys):
    doc = index_doc()
    doc["companies"]["BBB"]["pages"][0]["page_sha256"] = "0" * 64
    index_file.write_text(json.dumps(doc))
    c, _ = client(runs_dir, sources.load_index(index_file))
    rid = start(c)
    run = run_of(runs_dir, rid)
    assert {f.name for f in (runs_dir / rid / "pages").iterdir()} == _named(run) and len(_named(run)) == 4


def test_a_resumed_run_captures_the_dropped_page_again_to_the_same_bytes(runs_dir, index, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 1)
    c, first = client(runs_dir, index, second=None, wonder=None)
    rid = start(c)
    before = {f.name: f.read_bytes() for f in (runs_dir / rid / "pages").iterdir()}
    assert c.post(f"/api/runs/{rid}/resume").status_code == 202
    c.post(f"/api/runs/{rid}/resume")
    run = run_of(runs_dir, rid)
    after = {f.name: f.read_bytes() for f in (runs_dir / rid / "pages").iterdir()}
    assert run["run"]["status"] == "done" and set(after) == _named(run) and len(after) == 6
    assert all(after[n] == b for n, b in before.items() if n in after), "the pages kept from before are unchanged"
