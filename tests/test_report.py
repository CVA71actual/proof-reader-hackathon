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


def test_sample_summary_matches_state(sample_out):
    run, decisions = load_run_dir(SAMPLE)
    state = current_state(run, decisions)
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    for group, label in (
        ("kept", "Checked and kept"),
        ("corrected", "Corrected"),
        ("removed", "Checked and removed"),
        ("unverified", "Unverified"),
        ("open", "Still open"),
    ):
        n = state["counts"]["rows"][group]
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
    assert "Companies read:" in text and "Companies unread:" in text
    assert "Pages read:" in text and "unread: not recorded" in text
    assert "Stopping reason:" in text


def test_calendar_report_carries_identity_source_quote_and_each_check(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    row = result["view"]["calendar"][0]
    assert f"Evidence {row['id']}:" in text
    assert f"page {row['page_number']}" in text
    assert "indication" in text and "action" in text and "quote:" in text
    assert "Quote found on page:" in text and "Date read as written:" in text


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
    assert [r["id"] for r in result["contingent"]] == ["undated"]


def test_report_keeps_unresolved_and_fix_history_after_keep():
    run, _ = load_run_dir(SAMPLE)
    fix = {"field": "date", "from": "2027-07-09", "to": "2027-07-19"}
    decisions = [
        {"at": "2026-10-27T16:00:00+00:00", "target": "e7", "action": "fix", "note": None,
         "fix": fix, "by": "person"},
        {"at": "2026-10-27T16:05:00+00:00", "target": "e7", "action": "recheck", "note": None,
         "fix": None, "by": "code", "recheck": {"result": "could_not_run", "reason": "No source",
         "checks": [], "before": None, "after": None}},
        {"at": "2026-10-27T16:10:00+00:00", "target": "e7", "action": "keep",
         "note": "Keep despite unavailable check", "fix": None, "by": "person"},
    ]
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
    fixed = [
        {"at": "2026-10-31T10:00:00Z", "target": "e7", "action": "fix", "by": "person",
         "fix": {"field": "date", "from": "2027-07-09", "to": "2027-07-19"}},
        {"at": "2026-10-31T10:01:00Z", "target": "e7", "action": "recheck", "by": "code",
         "recheck": {"result": "pass", "reason": "New date passes.", "checks": [],
                     "before": {"result": "fail", "checks": raw["e7"]["checks"]},
                     "after": {"result": "pass", "checks": [{"name": "date_match", "label": "Date read as written",
                                                                  "result": "pass", "reason": "The fixed date matches."}]}}},
        {"at": "2026-10-31T10:02:00Z", "target": "e1", "action": "fix", "by": "person",
         "fix": {"field": "date", "from": "2027-03-12", "to": "2027-03-13"}},
        {"at": "2026-10-31T10:03:00Z", "target": "e1", "action": "recheck", "by": "code",
         "recheck": {"result": "pass", "reason": "Existing checks pass.", "checks": [],
                     "before": {"result": "pass", "checks": raw["e1"]["checks"]},
                     "after": {"result": "pass", "checks": [{"name": "date_match", "label": "Date read as written",
                                                                  "result": "pass", "reason": "The edited date passes."}]}}},
    ]
    view = build_view(run, fixed, generated_at=FIXED_TIME)
    e7 = next(row for row in view["calendar"] + view["later"] if row["id"] == "e7")
    assert [(c["name"], c["result"]) for c in e7["check_limitations"]] == [("Date read as written", "pass")]
    assert any(c["result"] == "fail" for c in e7["check_history"])
    assert any(c["result"] == "fail" for c in e7["check_history"])
    assert any(c["id"] == "e7" and c["caught_by"] == "person" for c in view["corrections"])
    assert any(e["id"] == "e1" and "old value passed" in e["edit_status"] for e in view["edits"])
    assert not any(c["id"] == "e1" for c in view["corrections"])

    pending = build_view(run, [fixed[0]], generated_at=FIXED_TIME)
    pending_e7 = next(row for row in pending["open_rows"] if row["id"] == "e7")
    assert pending_e7["check_limitations"] == []
    assert "Re-check pending" in pending_e7["check_status_text"]
    assert any(c["result"] == "fail" for c in pending_e7["check_history"])

    pdf_text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(build_pdf(view))).pages)
    doc = Document(BytesIO(build_docx(view)))
    doc_text = "\n".join(p.text for p in doc.paragraphs)
    for text in (pdf_text, doc_text):
        assert "current-value checks: Date read as written: pass" in text
        assert "original checks (history, before edit)" in text
        assert "caught by person" in text and "Other edits · no mistake demonstrated" in text


def test_sample_calendar_omits_removed(sample_out):
    result = build("sample", out=sample_out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    # e6 is removed (third-party calendar); e10 removed by person — must not appear in calendar table as kept events
    view = result["view"]
    cal_ids = {r["id"] for r in view["calendar"]}
    assert "e6" not in cal_ids
    assert "e10" not in cal_ids
    assert "e9" not in cal_ids  # unverified
    # The fixed six-month horizon puts this 2027 period in the Later section.
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
    run_dir = tmp_path / "once"
    run_dir.mkdir()
    (run_dir / "run.json").write_text((SAMPLE / "run.json").read_text())
    (run_dir / "decisions.jsonl").write_text((SAMPLE / "decisions.jsonl").read_text())
    first = build("once", run_dir=run_dir, generated_at=FIXED_TIME)
    assert first["pdf"].exists()
    with pytest.raises(ReportExistsError):
        build("once", run_dir=run_dir, generated_at=FIXED_TIME)
    # --out always allowed
    build("once", run_dir=run_dir, out=tmp_path / "test-out", generated_at=FIXED_TIME)


def test_ptgx_lists_pn8047_period_and_rendered_note(tmp_path):
    out = tmp_path / "ptgx-out"
    result = build("ptgx-r4a", run_dir=PTGX, out=out, generated_at=FIXED_TIME)
    text = _pdf_text(result["pdf"]) + "\n" + _docx_text(result["docx"])
    assert "PN-8047" in text
    assert "Q1 2027" in text
    assert "page rendered by code from the filed HTML" in text
    assert "PN-458o" in text
    # period as stated, not an invented day for PN-8047
    e2 = next(r for r in result["view"]["calendar"] if "PN-8047" in (r["program"] or r["event"]))
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
    assert view["counts"] == {g: state["counts"]["rows"][g] for g in view["group_labels"]}
    assert view["unresolved"] == state["unresolved"]
    assert view["state"] is state or view["counts"]["kept"] == state["counts"]["rows"]["kept"]


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
    for rid in ("demo", "sample", "ptgx-r4a", "ptgx-r5", "ptgx-scan", "twelve-1", "twelve-4"):
        run, decisions = load_run_dir(RUNS / rid)
        view = build_view(run, decisions, generated_at=FIXED_TIME)
        reports = next((RUNS / rid / "report").glob("*.pdf")), next((RUNS / rid / "report").glob("*.docx"))
        text = _pdf_text(reports[0]) + "\n" + _docx_text(reports[1])
        assert f"Window: {view['calendar_window']}" in text, rid
        if view["later"]:
            assert f"Later · {len(view['later'])}" in text, rid
        if view["contingent"]:
            assert f"Contingent · {len(view['contingent'])}" in text, rid
