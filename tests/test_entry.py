"""R4a: the first real entry. A real filing page (PTGX 8-K exhibit 99.1, page 3 rendered by code from the filed
HTML), Muse Spark's proposed events, the code checks, and runs/ptgx-r4a/ served by the decisions server.

No network: the saved Muse Spark reading is used when it is present, otherwise the labelled fake reading.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import capture_source, read_events
from checks import event_checks
from runs import build_entry
from runs.state import current_state
from server.app import create_app
from tests.conftest import without_trails

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
RUNS = REPO / "runs"
RUN_DIR = RUNS / "ptgx-r4a"
SCHEMA = Draft202012Validator(json.loads((RUNS / "schema" / "run.schema.json").read_text()))
PIN = "ptgx_8k_2026-08-05_ex991_p3"

# From the pin record (orders/R4A_PIN_PTGX_2026-10-28.md).
PIN_HTML_SHA = "ecfd2d79a6e812707d22a9f9a5b86ab1ff7dcc4e8bd9aa2d4c0c7636a7280d57"
PIN_PDF_SHA = "4ba68e37da0c15348d3eb9c3164a8f477ca81fa42d570fd648961e69cb5a1c50"
PIN_URL = "https://www.sec.gov/Archives/edgar/data/1377121/000110465926091099/tm2622272d1_ex99-1.htm"
PN8047_QUOTE = ("PN-8047 (oral small molecule hepcidin functional mimetic): IND-enabling studies are ongoing, "
                "with Phase 1 initiation anticipated in Q1 2027.")
PN458O_QUOTE = ("PN-458o (dual GLP/GIP agonist peptide): IND-enabling studies are ongoing with Phase 1 initiation "
                "anticipated in the second half of 2027.")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def entry():
    return capture_source.load_entry(PIN)


@pytest.fixture(scope="module")
def words_doc():
    return json.loads((FIXTURES / f"{PIN}.words.json").read_text())


@pytest.fixture(scope="module")
def reading(words_doc, entry):
    if read_events.READING_PATH.exists():
        return json.loads(read_events.READING_PATH.read_text())
    return read_events.read_fake(words_doc, entry)


@pytest.fixture(scope="module")
def run():
    from runs.check_load import load_bound_run
    return load_bound_run(RUN_DIR)


def by_program(run, program):
    return next(r for r in run["rows"] if r.get("program") == program)


# ---------------------------------------------------------------- capture


def test_capture_hashes_match_the_pin(entry):
    assert entry["source_sha256"] == PIN_HTML_SHA
    assert entry["rendered_pdf_sha256"] == PIN_PDF_SHA
    assert entry["source_url"] == PIN_URL
    assert (entry["page_number"], entry["rendered_page_count"]) == (3, 6)
    assert entry["renderer"] == "Google Chrome 156.0.8078.48 headless print-to-pdf"
    assert (entry["ticker"], entry["form"], entry["exhibit"]) == ("PTGX", "8-K", "99.1")
    assert entry["accession"] == "0001104659-26-091099"
    assert capture_source.PINS[PIN]["source_sha256"] == PIN_HTML_SHA
    assert capture_source.PINS[PIN]["rendered_pdf_sha256"] == PIN_PDF_SHA


def test_captured_page_files_match_the_manifest(entry, words_doc):
    assert sha(FIXTURES / entry["page_file"]) == entry["page_sha256"]
    assert sha(FIXTURES / entry["words_file"]) == entry["words_sha256"]
    assert len(words_doc["words"]) == entry["word_count"]
    assert words_doc["page_number"] == 3
    assert (words_doc["page_width"], words_doc["page_height"]) == (612, 792)


def test_the_ions_manifest_is_unchanged(manifest):
    assert manifest["ticker"] == "IONS" and manifest["page_number"] == 95
    assert PIN in manifest["entries"]


def test_capture_refuses_a_file_that_is_not_the_pinned_one(tmp_path):
    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF-1.4 not the pinned file")
    with pytest.raises(SystemExit, match="not the pinned one"):
        capture_source.capture(PIN, other, fixtures=tmp_path)


@pytest.mark.skipif(not capture_source.PINS[PIN]["default_source"].exists(), reason="rendered PDF not on this machine")
def test_capture_is_repeatable(tmp_path, entry):
    got = capture_source.capture(PIN, fixtures=tmp_path)
    assert got["page_sha256"] == entry["page_sha256"] and got["words_sha256"] == entry["words_sha256"]


def test_both_events_are_printed_on_the_page(words_doc):
    for q in (PN8047_QUOTE, PN458O_QUOTE):
        assert event_checks.quote_match(q, words_doc["words"]).result == "pass"


# ---------------------------------------------------------------- reading


def test_the_reading_is_bound_to_its_prompt_and_page(reading, entry):
    assert reading["prompt_sha256"] == read_events.sha256_text(reading["prompt_text"])
    assert reading["words_sha256"] == entry["words_sha256"]
    assert reading["readable"] and isinstance(reading["events"], list)
    if reading["fake"]:
        assert reading["model"] == "fake"
    else:
        assert reading["model"].startswith("muse-spark") and "fake_note" not in reading
        assert reading["raw_response"]


def test_the_saved_reading_is_the_live_one():
    if not read_events.READING_PATH.exists():
        pytest.skip("no live reading saved")
    rec = json.loads(read_events.READING_PATH.read_text())
    assert rec["fake"] is False and rec["model"] == "muse-spark-1.3"
    assert read_events.parse_events(rec["raw_response"])[0] == rec["events"]


def test_fake_reading_is_labelled_and_never_saved_as_the_live_one(words_doc, entry, tmp_path):
    fake = read_events.read_fake(words_doc, entry)
    assert fake["fake"] is True and fake["model"] == "fake" and "FAKE" in fake["fake_note"]
    run = build_entry.build_entry_run(fake, tmp_path / "practice")
    assert run["run"]["label"] == build_entry.FAKE_LABEL != build_entry.REAL_LABEL
    assert all(r["trail"][1]["tag"] == "Code" and r["trail"][1]["text"].startswith("FAKE") for r in run["rows"])


def test_parse_events_never_guesses():
    assert read_events.parse_events("")[0] is None
    assert read_events.parse_events("no json here")[0] is None
    assert read_events.parse_events('{"rows": []}')[0] is None
    assert read_events.parse_events('```json\n{"events": []}\n```') == ([], None)


def test_a_reading_with_no_events_builds_no_run(reading, tmp_path):
    failed = {**reading, "readable": False, "events": None, "call_failed": True,
              "parse_problem": "The model call did not succeed (APIError, status 500)."}
    with pytest.raises(SystemExit, match="no run is built"):
        build_entry.build_entry_run(failed, tmp_path / "x")


# ---------------------------------------------------------------- the run


def test_the_run_validates_and_is_labelled(run):
    errors = list(SCHEMA.iter_errors(run))
    assert errors == [], errors[:3]
    assert run["run"]["id"] == "ptgx-r4a" and run["run"]["sample"] is False
    assert run["run"]["label"] == "PTGX · one real entry"
    assert run["tables"] == [] and run["mistakes"] == [] and run["exclusions"] == []
    assert (RUN_DIR / "decisions.jsonl").read_text() == ""


def test_the_source_says_it_was_rendered_from_the_filed_html(run, entry):
    (src,) = run["sources"]
    assert src["kind"] == "filing" and src["url"] == PIN_URL
    assert src["sha256"] == PIN_PDF_SHA
    assert src["rendered_from"] == {"format": "html", "sha256": PIN_HTML_SHA, "renderer": entry["renderer"]}
    (page,) = src["pages"]
    assert page["page_number"] == 3
    assert sha(RUN_DIR / page["image"]) == page["image_sha256"]
    assert sha(RUN_DIR / page["words"]) == page["words_sha256"] == entry["words_sha256"]


def test_the_run_build_is_current(run, reading, tmp_path):
    if reading["fake"]:
        pytest.skip("the committed run is built from the live reading")
    fresh = build_entry.build_entry_run(reading, tmp_path / "ptgx-r4a", finished_at=run["run"]["finished_at"])
    assert without_trails(fresh) == without_trails(run)


def test_the_pn8047_row(run):
    row = by_program(run, "PN-8047")
    assert row["ticker"] == "PTGX" and row["event_type"] == "milestone"
    assert row["date_text"] == "Q1 2027"
    assert row["quote"] == PN8047_QUOTE
    checks = {c["name"]: c for c in row["checks"]}
    assert checks["source_rule"]["result"] == "pass"
    if row["status"] == "matched":
        assert (row["date"], row["date_end"]) == ("2027-01-01", "2027-03-31")
        assert row["group"] == "kept"
        assert {c["result"] for c in row["checks"]} == {"pass"}
        assert "Checks passed:" in row["reason"] and "Every check passed" not in row["reason"]
        assert "keeps the period as stated" in row["reason"]


def test_period_rows_are_handled_as_periods(run):
    for row in run["rows"]:
        period = event_checks.parse_period(row["date_text"])
        if period is None:
            continue
        date_check = next(c for c in row["checks"] if c["name"] == "date_match")
        start, end = period
        if start is None:  # "early 2027", "mid-2027": the dates must fall inside the rough window
            window = event_checks.vague_period_window(row["date_text"])
            if row["date"] and row["date_end"] and date_check["result"] == "pass":
                assert window[0].isoformat() <= row["date"] <= row["date_end"] <= window[1].isoformat()
        elif date_check["result"] == "pass" and row["date"]:
            assert (row["date"], row["date_end"]) == (start.isoformat(), end.isoformat())


def test_the_second_half_row_if_read(run):
    rows = [r for r in run["rows"] if r.get("program") == "PN-458o"]
    if not rows:
        pytest.skip("Muse Spark did not propose the PN-458o row")
    (row,) = rows
    assert "second half of 2027" in row["date_text"]
    if row["status"] == "matched":
        assert (row["date"], row["date_end"]) == ("2027-07-01", "2027-12-31")


def test_every_row_is_checked_by_the_real_functions(run):
    for row in run["rows"]:
        assert [c["name"] for c in row["checks"]] == ["quote_match", "selector_match", "date_match", "source_rule",
                                                      "type_match"]
        assert row["status"] == event_checks.row_status(row["checks"])
        assert row["group"] == build_entry.START_GROUP.get(row["status"], "unverified")
        assert row["sample"] is False and row["source_id"] == run["sources"][0]["id"]


def test_every_trail_starts_with_the_exact_instructions(run, reading):
    for row in run["rows"]:
        first = next(t for t in row["trail"] if t["step"] == "Instructions sent")
        assert (first["tag"], first["who"]) == ("Code", "capture.read_events")
        assert first["text"] == reading["prompt_text"]
        steps = [s["step"] for s in row["trail"]]
        assert "Assigned event type" in steps and "Proposed" in steps and steps.count("Checked") == 5
        for s in row["trail"]:
            if s["tag"] == "Code":
                assert "Muse" not in s["who"]


def test_a_tidied_date_text_is_a_code_step(run, reading):
    for row, ev in zip(run["rows"], reading["events"]):
        tidied = [s for s in row["trail"] if s["step"] == "Tidied"]
        if ev["date_text"] == row["date_text"]:
            assert not tidied
        else:
            assert len(tidied) == 1 and tidied[0]["tag"] == "Code" and tidied[0]["who"] == "runs.build_entry"
            assert ev["date_text"].rstrip(".,;:").rstrip() == row["date_text"]


def test_tidy_date_text_only_drops_the_sentence_end():
    assert build_entry.tidy_date_text("Q1 2027.", PN8047_QUOTE) == "Q1 2027"
    assert build_entry.tidy_date_text("Q1 2027", PN8047_QUOTE) == "Q1 2027"
    assert build_entry.tidy_date_text("Q1 2028.", PN8047_QUOTE) == "Q1 2028."   # not in the quote: left alone
    assert build_entry.tidy_date_text(None, PN8047_QUOTE) is None


# ---------------------------------------------------------------- the checks this page needed


def test_a_program_code_is_not_a_year():
    """'PN-8047' holds four digits; they are a name, not the year 8047."""
    r = event_checks.date_match("Q1 2027", "2027-01-01", PN8047_QUOTE, "2027-03-31", event_type="milestone",
                                title="Phase 1 initiation", program="PN-8047")
    assert r.result == "pass", r.reason
    r = event_checks.date_match("Q1 2027", "2027-01-01", PN8047_QUOTE, "2027-03-31", event_type="milestone",
                                program="PN-8047")
    assert r.result == "pass", r.reason
    # a bare year is still a year, and a year range still names both years
    assert event_checks.date_match("2027", None, "Results in 2027.", event_type="trial_results").result == "pass"
    r = event_checks.date_match("2027", "2027-01-01", "Data in 2026-2027.", "2027-12-31", event_type="trial_results")
    assert r.result == "could_not_run"


def test_initiation_is_a_milestone_word():
    assert "initiation" in event_checks.event_anchors("milestone")
    r = event_checks.date_match("Q1 2027", "2027-01-01", "Phase 1 initiation is anticipated in Q1 2027.",
                                "2027-03-31", event_type="milestone")
    assert r.result == "pass", r.reason


def test_a_wrong_period_on_the_real_row_fails(run, words_doc):
    row = by_program(run, "PN-8047")
    wrong = {**row, "date": "2027-04-01", "date_end": "2027-06-30"}
    from runs.recheck import row_checks
    results = row_checks(wrong, run["sources"][0], words_doc)
    assert event_checks.row_status(results) == "failed"
    assert next(r for r in results if r.check == "date_match").result == "fail"


# ---------------------------------------------------------------- the server


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    shutil.copytree(RUN_DIR, d / "ptgx-r4a")
    return d


@pytest.fixture
def client(runs_dir):
    return TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui"))


def post(client, target, action, note=None, fix=None):
    return client.post("/api/runs/ptgx-r4a/decisions",
                       json={"target": target, "action": action, "note": note, "fix": fix})


def test_the_server_lists_and_shows_the_run(client, run):
    listed = client.get("/api/runs").json()
    assert [(r["id"], r["label"], r["sample"]) for r in listed] == [("ptgx-r4a", "PTGX · one real entry", False)]
    body = client.get("/api/runs/ptgx-r4a").json()
    assert body["rows"] == run["rows"] and body["sources"][0]["rendered_from"]["format"] == "html"
    row = by_program(run, "PN-8047")
    state = body["state"]["targets"][row["id"]]
    assert (state["status"], state["group"]) == ("matched", "kept")
    assert state["claim_assessments"]["visible_passage"]["result"] == "not_checked"


def test_the_server_serves_page_3(client, run):
    page = run["sources"][0]["pages"][0]
    r = client.get(f"/runs/ptgx-r4a/{page['image']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert hashlib.sha256(r.content).hexdigest() == page["image_sha256"]
    words = client.get(f"/runs/ptgx-r4a/{page['words']}").json()
    assert words["page_number"] == 3 and any(w["text"] == "PN-8047" for w in words["words"])


def test_keep_then_undo_on_the_real_row(client, runs_dir, run):
    rid = by_program(run, "PN-8047")["id"]
    r = post(client, rid, "keep", note="Kept as stated: Q1 2027.")
    assert r.status_code == 200, r.text
    st = r.json()["state"]
    assert st["group"] == "kept" and st["decided_by_person"] is True and st["status"] == by_program(run, "PN-8047")["status"]
    lines = (runs_dir / "ptgx-r4a" / "decisions.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["action"] == "keep"
    r = post(client, rid, "undo")
    assert r.status_code == 200 and r.json()["state"]["decided_by_person"] is False
    assert (r.json()["state"]["status"], r.json()["state"]["group"]) == ("matched", "kept")
    assert len((runs_dir / "ptgx-r4a" / "decisions.jsonl").read_text().splitlines()) == 2


def test_fix_on_the_real_row_is_rechecked(client, runs_dir, run):
    row = by_program(run, "PN-8047")
    r = post(client, row["id"], "fix", fix={"field": "date_end", "from": row["date_end"], "to": "2027-06-30"})
    assert r.status_code == 200, r.text
    written = r.json()["written"]
    assert [w["action"] for w in written] == ["fix", "recheck"]
    rc = written[1]["recheck"]
    assert rc["result"] == "fail" and rc["after"]["result"] == "fail"   # Q1 2027 ends on 2027-03-31
    assert r.json()["state"]["group"] == "open"
    assert post(client, row["id"], "undo").status_code == 200
    s = client.get("/api/runs/ptgx-r4a").json()["state"]["targets"][row["id"]]
    assert s["values"]["date_end"] == row["date_end"]


def test_remove_on_the_real_row(client, run):
    rid = by_program(run, "PN-8047")["id"]
    r = post(client, rid, "remove", note="Not needed for this test.")
    assert r.status_code == 200 and r.json()["state"]["group"] == "removed"
    assert client.get("/api/runs/ptgx-r4a").json()["state"]["counts"]["excluded_by_person"] == 1


def test_state_rule_with_no_decisions_leaves_the_run_as_it_was(run):
    st = current_state(run, [])
    for row in run["rows"]:
        target = st["targets"][row["id"]]
        assert target["group"] == row["group"]
