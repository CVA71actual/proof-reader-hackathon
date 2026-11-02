"""M1: code-built PDF + Word report. No network."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

from report.build import ReportExistsError, build, report_filenames
from report.data import FOOTER, BANNED_WORDS, build_view, load_run_dir
from runs.state import current_state

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
SAMPLE = RUNS / "sample"
PTGX = RUNS / "ptgx-r4a"

FIXED_TIME = "2026-10-28T20:00:00Z"


def _pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _docx_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    # footer
    for section in doc.sections:
        for p in section.footer.paragraphs:
            parts.append(p.text)
    return "\n".join(parts)


def _new_keep(run, decisions, target, note=None):
    """New human acceptance in a unit fixture; never back-fill a historical Keep."""
    return {"at": "2026-11-01T22:30:00Z", "target": target, "action": "keep",
            "note": note, "fix": None, "by": "person",
            "review_basis": current_state(run, decisions)["targets"][target]["review_basis"]}


def _copy_and_keep(source, tmp_path, target):
    """Exercise normal server acceptance against disposable saved evidence."""
    import shutil
    from fastapi.testclient import TestClient
    from server.app import create_app

    root = tmp_path / "runs"
    destination = root / source.name
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("report"))
    with TestClient(create_app(runs_dir=root)) as client:
        response = client.post(f"/api/runs/{source.name}/decisions",
                               json={"target": target, "action": "keep"})
        assert response.status_code == 200, response.text
        assert response.json()["state"]["accepted"]
    return destination


def _mask_timestamp(data: bytes, stamp: str) -> bytes:
    return data.replace(stamp.encode("utf-8"), b"TIMESTAMP_MASKED")


@pytest.fixture
def sample_out(tmp_path):
    return tmp_path / "sample-out"


def test_sample_builds_both_files(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    assert result["pdf"].is_file()
    assert result["docx"].is_file()
    assert result["pdf"].name == "biotech-coverage-catalyst-calendar-run2.pdf"
    assert result["docx"].name == "biotech-coverage-catalyst-calendar-run2.docx"
    assert result["pdf"].stat().st_size > 500
    assert result["docx"].stat().st_size > 500


def test_sample_summary_matches_review_state_not_mechanical_groups(sample_out):
    run, decisions = load_run_dir(SAMPLE)
    state = current_state(run, decisions)
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    for group, label in (
        ("accepted", "Accepted by you"),
        ("proposed", "Proposed — needs review"),
        ("excluded", "Excluded"),
    ):
        n = state["counts"]["review"][group]
        assert f"{label}: {n}" in text
    m = state["mistakes"]
    excluded = state["excluded"]["code"] + state["excluded"]["person"]
    assert (
        f"Mistakes caught by code {m['code']} · by the second reader {m['second_reader']} "
        f"· by a person {m['person']} · Excluded {excluded}"
    ) in text
    assert f"Unresolved (needs attention): {state['unresolved']}" in text
    assert "Sample data — not real events" in text


def test_report_names_run_status_read_coverage_and_page_count_limitation(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    assert "Run status: Done" in text
    assert "Page outcomes: captured 6 · attempted 6 · read 5 · unreadable 1 · skipped 0 · unread 1" in text
    assert "Stopping reason:" in text


def test_report_prints_distinct_page_outcomes_from_the_shared_read_view(sample_out):
    from report.data import load_run_dir
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf

    run, decisions = load_run_dir(SAMPLE)
    run["run"]["page_outcomes"] = [
        {"source_id": "a", "ticker": "AAA", "company": "A", "page_number": 1,
         "captured": True, "attempted": True, "read": True, "unreadable": False, "skipped": False,
         "unread": False},
        {"source_id": "b", "ticker": "BBB", "company": "B", "page_number": 2,
         "captured": True, "attempted": True, "read": False, "unreadable": True, "skipped": False,
         "unread": True},
    ]
    view = build_view(run, decisions, generated_at=FIXED_TIME)
    assert view["page_outcome_counts"] == {"captured": 2, "attempted": 2, "read": 1,
                                           "unreadable": 1, "skipped": 0, "unread": 1}
    assert [(x["ticker"], x["read"], x["unreadable"]) for x in view["page_outcomes_by_company"]] == [
        ("AAA", 1, 0), ("BBB", 0, 1)]
    from pypdf import PdfReader
    text = "\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(build_pdf(view))).pages)
    doc = Document(BytesIO(build_docx(view)))
    text += "\n" + "\n".join(p.text for p in doc.paragraphs)
    assert "Page outcomes: captured 2 · attempted 2 · read 1 · unreadable 1 · skipped 0 · unread 1" in text
    assert "AAA page outcomes: captured 1 · attempted 1 · read 1 · unreadable 0 · skipped 0 · unread 0" in text


def test_calendar_report_carries_identity_source_quote_and_each_check(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    row = result["view"]["open_rows"][0]
    assert row["id"] in text
    assert f"page {row['page_number']}" in text
    assert "indication" in text and "source" in text and "quote:" in text
    assert "Claimed words visible on the rendered page: not_checked" in text
    assert "Quote found on page: pass" in text  # preserved as history, not current evidence


def test_interval_partition_keeps_overlapping_period_and_human_past_row():
    from runs.calendar import partition_intervals

    rows = [
        {"id": "overlap", "date": "2026-09-01", "date_end": "2026-10-31"},
        {"id": "past", "date": "2026-08-01", "date_end": "2026-08-31", "group": "kept"},
        {"id": "later", "date": "2027-05-01", "date_end": None},
        {"id": "undated", "date": None, "date_end": None},
    ]
    result = partition_intervals(rows, "2026-10-01", "2027-04-01")
    assert [r["id"] for r in result["calendar"]] == ["overlap"]
    assert [r["id"] for r in result["past"]] == ["past"]
    assert [r["id"] for r in result["later"]] == ["later"]
    assert [r["id"] for r in result["undated"]] == ["undated"]


def test_report_keeps_unresolved_and_fix_history_after_keep():
    run, _ = load_run_dir(SAMPLE)
    fix = {"field": "date", "from": "2027-07-09", "to": "2027-07-19"}
    decisions = [
        {"at": "2026-10-27T16:00:00+00:00", "target": "e7", "action": "fix", "note": None,
         "fix": fix, "by": "person"},
        {"at": "2026-10-27T16:05:00+00:00", "target": "e7", "action": "recheck", "note": None,
         "fix": None, "by": "code", "recheck": {"result": "could_not_run", "reason": "No source",
         "checks": [], "before": None, "after": None}},
    ]
    # FIX-THEN-KEEP-001: an edit and unavailable check do not imply acceptance.
    assert not current_state(run, decisions)["targets"]["e7"]["accepted"]
    decisions.append(_new_keep(run, decisions, "e7", "Keep despite unavailable check"))
    view = build_view(run, decisions, generated_at=FIXED_TIME)
    history = [c for c in view["edits"] if c["id"] == "e7"]
    assert history and history[-1]["latest_recheck"] == "could_not_run"
    assert history[-1]["override_reason"] == "Keep despite unavailable check"
    assert not any(c["id"] == "e7" for c in view["corrections"])
    assert history[-1]["event"] and history[-1]["ticker"]
    pending_view = build_view(run, [decisions[0]], generated_at=FIXED_TIME)
    assert any(r["id"] == "e7" for r in pending_view["open_rows"])
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf
    pdf_text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(build_pdf(view))).pages)
    doc = Document(BytesIO(build_docx(view)))
    doc_text = "\n".join(p.text for p in doc.paragraphs)
    assert "could_not_run" in pdf_text and "Keep despite unavailable check" in pdf_text
    assert "could_not_run" in doc_text and "Keep despite unavailable check" in doc_text


def test_report_uses_current_recheck_for_evidence_and_separates_ordinary_edits(sample_out):
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf

    run, _ = load_run_dir(SAMPLE)
    raw = {row["id"]: row for row in run["rows"]}
    quote = "Samplebio Six will report final results on July 19, 2027."
    fixed = [
        {"at": "2026-10-31T10:00:00Z", "target": "e7", "action": "fix", "by": "person",
         "fix": {"field": "date", "from": "2027-07-09", "to": "2027-07-19"}},
        {"at": "2026-10-31T10:00:30Z", "target": "e7", "action": "fix", "by": "person",
         "fix": {"field": "date_text", "from": "July 9, 2027", "to": "July 19, 2027"}},
        {"at": "2026-10-31T10:00:45Z", "target": "e7", "action": "fix", "by": "person",
         "fix": {"field": "quote", "from": raw["e7"]["quote"], "to": quote}},
        {"at": "2026-10-31T10:01:00Z", "target": "e7", "action": "recheck", "by": "code",
         "recheck": {"result": "pass", "reason": "New date and quote pass.", "checks": []}},
        {"at": "2026-10-31T10:02:00Z", "target": "e1", "action": "fix", "by": "person",
         "fix": {"field": "date", "from": "2027-03-12", "to": "March 12, 2027"}},
        {"at": "2026-10-31T10:03:00Z", "target": "e1", "action": "recheck", "by": "code",
         "recheck": {"result": "pass", "reason": "The same day remains in the row.", "checks": []}},
    ]
    from runs.check_load import bind_decisions_on_load
    fixed = bind_decisions_on_load(run, fixed, SAMPLE)
    assert not current_state(run, fixed)["targets"]["e7"]["accepted"]
    fixed.append(_new_keep(run, fixed, "e7"))
    view = build_view(run, fixed, generated_at=FIXED_TIME)
    e7 = next(row for row in view["later"] if row["id"] == "e7")
    current = e7["check_limitations"]
    # Type is a proposal, not a second keyword verdict. Literal checks still pass.
    assert all(c["result"] == ("not_needed" if c["name"] == "Event meaning needs review" else "pass")
               for c in current[:-1])
    assert [(c["name"], c["result"]) for c in current[-1:]] == [
        ("Claimed words visible on the rendered page", "not_checked")]
    assert any(c["result"] == "fail" for c in e7["check_history"])
    assert any(c["result"] == "fail" for c in e7["check_history"])
    assert any(c["id"] == "e7" and c["caught_by"] == "person" for c in view["corrections"])
    assert any(e["id"] == "e1" and "old value passed" in e["edit_status"] for e in view["edits"])
    assert not any(c["id"] == "e1" for c in view["corrections"])

    pending = build_view(run, [fixed[0]], generated_at=FIXED_TIME)
    pending_e7 = next(row for row in pending["open_rows"] if row["id"] == "e7")
    assert [(c["name"], c["result"]) for c in pending_e7["check_limitations"]] == [
        ("Claimed words visible on the rendered page", "not_checked")]
    assert "visible words have not been checked" in pending_e7["check_status_text"]
    assert any(c["result"] == "fail" for c in pending_e7["check_history"])

    pdf_text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(build_pdf(view))).pages)
    doc = Document(BytesIO(build_docx(view)))
    doc_text = "\n".join(p.text for p in doc.paragraphs)
    for text in (pdf_text, doc_text):
        assert "current-value checks:" in text
        assert "Claimed words visible on the rendered page: not_checked" in text
        assert "Original checks · history (before edit)" in text
        assert "caught by person" in text and "Other edits · no mistake demonstrated" in text


def test_sample_calendar_omits_removed(sample_out, tmp_path):
    run_dir = _copy_and_keep(SAMPLE, tmp_path, "e2")
    result = build("sample", run_dir=run_dir, out=sample_out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    # e6 is removed (third-party calendar); e10 removed by person — must not appear in calendar table as kept events
    view = result["view"]
    cal_ids = {r["id"] for r in view["calendar"]}
    assert "e6" not in cal_ids
    assert "e10" not in cal_ids
    assert "e9" not in cal_ids  # unverified
    # A fresh human Keep admits this source-bound period; historical Keeps do not.
    e2 = next(r for r in view["later"] if r["id"] == "e2")
    assert e2["date_display"] == "Q3 2027"
    assert "Q3 2027" in text


def test_sample_footer_on_every_pdf_page(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    reader = PdfReader(str(result["pdf"]))
    assert len(reader.pages) >= 1
    for i, page in enumerate(reader.pages):
        t = page.extract_text() or ""
        assert FOOTER in t, f"footer missing on PDF page {i + 1}"


def test_banned_words_absent(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    text = (_pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])).lower()
    # "Unverified" is the approved group name and may contain the letters of "verified".
    for word in BANNED_WORDS:
        assert re.search(rf"(?<![a-z]){word}(?![a-z])", text) is None
    # "correct" alone is banned; "corrected" / "Corrections" are allowed
    assert re.search(r"\bcorrect\b", text) is None


def test_deterministic_with_timestamp_masked(sample_out):
    a = build("sample", out=sample_out / "a", generated_at=FIXED_TIME)
    b = build("sample", out=sample_out / "b", generated_at="2026-10-28T21:00:00Z")
    line_a, line_b = a["view"]["generated_at"], b["view"]["generated_at"]
    assert _pdf_text(a["pdf"]).replace(line_a, "T") == _pdf_text(b["pdf"]).replace(line_b, "T")
    # docx package parts carry zip timestamps; compare body + footer text
    assert _docx_text(a["docx"]).replace(line_a, "T") == _docx_text(b["docx"]).replace(line_b, "T")


ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def test_built_on_line_is_in_words_and_no_iso_timestamp_in_either_file(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    for text in (_pdf_text(result["pdf"]), _docx_text(result["docx"])):
        assert re.search(r"Built on \d{1,2} [A-Z][a-z]+ \d{4} at \d\d:\d\d \(local time\)", text)
        assert "Generated" not in text
        assert not ISO_TIMESTAMP.search(text)


def test_past_run_never_rebuilt(tmp_path):
    # Simulate an already-saved report under a fake run folder.
    import shutil

    run_dir = tmp_path / "once"
    shutil.copytree(SAMPLE, run_dir)
    shutil.rmtree(run_dir / "report")
    first = build("once", run_dir=run_dir, generated_at=FIXED_TIME)
    assert first["pdf"].exists()
    with pytest.raises(ReportExistsError):
        build("once", run_dir=run_dir, generated_at=FIXED_TIME)
    # --out always allowed
    build("once", run_dir=run_dir, out=tmp_path / "test-out", generated_at=FIXED_TIME)


def test_ptgx_lists_pn8047_period_and_rendered_note(tmp_path):
    out = tmp_path / "ptgx-out"
    run, _ = load_run_dir(PTGX)
    target = next(r["id"] for r in run["rows"] if "PN-8047" in r["program"])
    run_dir = _copy_and_keep(PTGX, tmp_path, target)
    result = build("ptgx-r4a", run_dir=run_dir, out=out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    assert "PN-8047" in text
    assert "Q1 2027" in text
    assert "page rendered by code from the filed HTML" in text
    assert "PN-458o" in text
    # period as stated, not an invented day for PN-8047
    e2 = next(r for r in result["view"]["calendar"] if "PN-8047" in r["program"])
    assert e2["date_display"] == "Q1 2027"
    assert FOOTER in _pdf_text(result["pdf"])


def test_filenames_helper():
    assert report_filenames(12) == (
        "biotech-coverage-catalyst-calendar-run12.pdf",
        "biotech-coverage-catalyst-calendar-run12.docx",
    )


def test_view_uses_same_state_counts():
    run, decisions = load_run_dir(SAMPLE)
    state = current_state(run, decisions)
    view = build_view(run, decisions, generated_at=FIXED_TIME)
    # GROUP-AXIS-001: summaries report acceptance; mechanical counts remain diagnostic.
    assert view["counts"] == state["counts"]["review"]
    assert view["mechanical_counts"] == state["counts"]["rows"]
    assert view["unresolved"] == state["unresolved"]
    assert view["state"] == state
    assert view["counts"]["accepted"] == sum(t["accepted"] for t in state["targets"].values() if t["kind"] == "row")


def _last_page_body(pdf_bytes: bytes) -> tuple[int, list[str]]:
    pages = PdfReader(BytesIO(pdf_bytes)).pages
    lines = [ln for ln in (pages[-1].extract_text() or "").splitlines() if ln.strip()]
    body = [ln for ln in lines if not ln.startswith(("Built with", "page "))]
    return len(pages), body


def test_twelve1_last_page_is_not_blank_or_an_orphan_line():
    from report.pdf_report import build_pdf

    run, decisions = load_run_dir(RUNS / "twelve-1")
    view = build_view(run, decisions, generated_at=FIXED_TIME)
    n, body = _last_page_body(build_pdf(view))
    assert n >= 19  # row evidence now prints verbatim and can add pages
    assert body, "last page holds only the footer"
    # The expanded evidence section flows ahead of Sources; the document still
    # carries a complete source fingerprint on its final page.
    assert any("sha256:" in ln for ln in body)


def test_saved_reports_last_page_has_content():
    for rid in ("demo", "ptgx-r4a", "ptgx-r5", "ptgx-scan", "twelve-1"):
        pdf = next((RUNS / rid / "report").glob("*.pdf"))
        _, body = _last_page_body(pdf.read_bytes())
        assert body, f"{rid}: last page holds only the footer"


def test_saved_reports_match_the_current_window_and_contingent_sections():
    from report.build import build
    import tempfile
    for rid in ("demo", "sample", "ptgx-r4a", "ptgx-r5", "ptgx-scan", "twelve-1", "twelve-4"):
        run, decisions = load_run_dir(RUNS / rid)
        view = build_view(run, decisions, generated_at=FIXED_TIME)
        with tempfile.TemporaryDirectory() as temp:
            made = build(rid, run_dir=RUNS / rid, out=Path(temp), generated_at=FIXED_TIME)
            reports = made["pdf"], made["docx"]
            text = _pdf_text(reports[0]) + "\n" + _docx_text(reports[1])
        assert f"Window: {view['calendar_window']}" in text, rid
        if view["later"]:
            assert f"Later · {len(view['later'])}" in text, rid
        if view["contingent"]:
            assert f"Contingent · {len(view['contingent'])}" in text, rid
        if view["undated"]:
            assert f"Undated · no timing stated · {len(view['undated'])}" in text, rid


def _rendered_texts(view):
    """PDF text and DOCX text (paragraphs + table cells), whitespace-collapsed so wrapped lines still match."""
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf
    pdf = " ".join((p.extract_text() or "") for p in PdfReader(BytesIO(build_pdf(view))).pages)
    doc = Document(BytesIO(build_docx(view)))
    parts = [p.text for p in doc.paragraphs]
    parts += [cell.text for table in doc.tables for row in table.rows for cell in row.cells]
    docx = " ".join(parts)
    return {"pdf": re.sub(r"\s+", " ", pdf), "docx": re.sub(r"\s+", " ", docx)}


def _pivot_views():
    """The four review states, each from a real decision log through the production projection."""
    import copy

    from tests.test_pivot_proposal_flow import AT, CASES, decision, kept_decision, make_run

    _, run = make_run(CASES[1])
    edit = {**decision("fix"), "fix": {"field": "title", "from": run["rows"][0]["title"], "to": "Revised proposed title"}}
    keep = kept_decision(run, [edit])
    expired_run = copy.deepcopy(run)
    expired_run["rows"][0]["conditions"] = []  # the row changed after the Keep, so the Keep is history
    logs = {
        "proposed": (run, []),
        "fix_only": (run, [edit]),
        "accepted": (run, [edit, keep]),
        "expired": (expired_run, [edit, keep]),
    }
    return {name: build_view(r, log, generated_at=AT) for name, (r, log) in logs.items()}, logs


def test_renderer_prints_projected_review_labels_and_context():
    views, logs = _pivot_views()
    context = ("relevance: Synthetic qualified proposal; meaning needs review. · subject company: Cedar Bio · "
               "relation: Assigned company; no other relationship proposed.")
    # Every label comes from runs.review.review_projection via report/data.py for a real decision log
    # (literal wording pinned in tests/test_pivot_review_projection.py); the renderer prints them verbatim.
    targets = {name: current_state(run, log)["targets"]["e1"] for name, (run, log) in logs.items()}
    assert len({t["review_label"] for t in targets.values()}) == 4
    assert targets["fix_only"]["decision_label"] == "Fixed by you — not yet accepted"
    assert targets["accepted"]["review_label"] == "Accepted by you"
    assert targets["accepted"]["decision_label"] == "Kept by you"
    expected_open = {}
    for name in ("proposed", "fix_only", "expired"):
        view, target = views[name], targets[name]
        assert view["calendar"] == [] and len(view["open_rows"]) == 1, name
        row = view["open_rows"][0]
        assert (row["group_label"], row["decision_label"]) == (target["review_label"], target["decision_label"]), name
        assert view["no_accepted_rows_text"] == "No rows accepted by you.", name
        decision = target["decision_label"]
        expected_open[name] = target["review_label"] + (f" · {decision}" if decision and decision != target["review_label"] else "")
    assert expected_open["fix_only"] == "Corrected, awaiting your review · Fixed by you — not yet accepted"
    for name, prefix in expected_open.items():
        for fmt, text in _rendered_texts(views[name]).items():
            where = f"{name}/{fmt}"
            assert f"{prefix} · CEDR · Cedar Bio" in text, where
            assert context in text, where
            assert "No rows accepted by you." in text, where
            # never shown as current acceptance
            assert "Kept by you" not in text, where
            assert "Accepted by you ·" not in text, where
            assert "Accepted by you · Fixed by you" not in text, where
            assert "Kept and corrected" not in text and "kept or corrected" not in text, where
    accepted = views["accepted"]
    assert accepted["open_rows"] == [] and len(accepted["calendar"]) == 1
    assert accepted["no_accepted_rows_text"] is None
    for fmt, text in _rendered_texts(accepted).items():
        assert "Accepted by you · Contingent · Kept by you" in text, fmt
        assert "Accepted by you — ordered by date." in text, fmt
        assert "No rows accepted by you." not in text, fmt
        assert f"conditions: if validation succeeds · {context}" in text, fmt
    for name in ("proposed", "fix_only"):
        for fmt, text in _rendered_texts(views[name]).items():
            assert f"conditions: if validation succeeds · {context}" in text, f"{name}/{fmt}"
    for fmt, text in _rendered_texts(views["expired"]).items():
        assert f"conditions: none stated · {context}" in text, fmt
    # the empty-calendar sentence is data.py's, not the renderer's
    sentinel = dict(views["proposed"], no_accepted_rows_text="SENTINEL empty sentence")
    for fmt, text in _rendered_texts(sentinel).items():
        assert "SENTINEL empty sentence" in text and "No rows accepted by you." not in text, fmt


@pytest.mark.parametrize("conditions, shown, absent", [
    (None, "conditions: not stated", "conditions: none stated"),   # unknown: nobody established it
    ([], "conditions: none stated", "conditions: not stated"),     # the reader said there are none
])
def test_renderer_separates_unknown_from_empty_conditions(conditions, shown, absent):
    from tests.test_pivot_proposal_flow import AT, CASES, make_run

    _, run = make_run(CASES[1])
    run["rows"][0]["conditions"] = conditions
    view = build_view(run, [], generated_at=AT)
    assert [r["conditions"] for r in view["open_rows"]] == [conditions]
    for fmt, text in _rendered_texts(view).items():
        assert shown in text, fmt
        assert absent not in text, fmt


def test_renderer_prints_passing_date_check_as_neutral_finding():
    """DATE-LITERAL-001: a passing date_match compares printed wording only; it is shown by its label, never as a pass."""
    import copy

    views, _ = _pivot_views()
    with_pass = [name for name, view in views.items()
                 if any(c.get("check_name") == "date_match" and c["result"] == "pass"
                        for r in view["calendar"] + view["open_rows"]
                        for c in r["check_limitations"] + r.get("check_history", []))]
    assert {"proposed", "accepted"} <= set(with_pass), with_pass  # Fix-only has no checks on the new value; accepted keeps them as history
    for name in with_pass:
        for fmt, text in _rendered_texts(views[name]).items():
            where = f"{name}/{fmt}"
            assert "Date period found in quote" in text, where
            assert "Date period found in quote: pass" not in text, where
            assert "Quote found on page: pass" in text, where  # other checks keep their result word
    failed = copy.deepcopy(views["proposed"])
    for c in failed["open_rows"][0]["check_limitations"]:
        if c.get("check_name") == "date_match":
            c["result"] = "fail"
    for fmt, text in _rendered_texts(failed).items():
        assert "Date period found in quote: fail" in text, fmt


def test_renderer_prints_server_action_with_legacy_provenance_verbatim():
    """`action` is report/data.py's type_presentation summary; the renderer prints it as given, never rebuilt."""
    from tests.test_pivot_proposal_flow import AT, CASES, kept_decision, make_run

    _, run = make_run(CASES[1])
    run["rows"][0].pop("type_hint", None)
    run["rows"][0]["event_type"] = "trial_results"  # a legacy row: category from the old keyword rule
    view = build_view(run, [kept_decision(run)], generated_at=AT)
    row = view["calendar"][0]
    assert row["action"] == row["type_presentation"]["summary"]
    assert "Category (from old keyword rule): trial_results" in row["action"]
    for fmt, text in _rendered_texts(view).items():
        assert f"action {row['action']}" in text, fmt
