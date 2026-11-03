"""EXPORT-LAYOUT-001 (CVA 2026-11-03): the calendar is on page 1 of the PDF and the Word file; the technical log moved
to an appendix; nothing was dropped; the footer names the models the run actually used. No model calls, no network.

The conservation snapshots in tests/fixtures/report_layout/*.strings.json hold every body string (Word paragraphs and
table cells) the PRE-CHANGE writers emitted for each run at FIXED_TIME. Regenerate them only when the wording of the
report changes on purpose (see _body_strings below for how they were made)."""

from __future__ import annotations

import copy
import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from docx.oxml.ns import qn
from pypdf import PdfReader

from report.data import (APPENDIX_HEADING, FOOTER, PLACEMENT_SECTIONS, TITLE, build_view, footer_line, load_run_dir,
                         models_used)
from report.docx_report import build_docx
from report.pdf_report import build_pdf

REPO = Path(__file__).resolve().parent.parent
SNAPSHOTS = REPO / "tests" / "fixtures" / "report_layout"
FIXED_TIME = "2026-10-28T20:00:00Z"
RUNS = {
    "e2e_brief": REPO / "tests" / "fixtures" / "e2e_saved_run" / "brief",        # real research run, no review
    "e2e_reviewed": REPO / "tests" / "fixtures" / "e2e_saved_run" / "reviewed",  # real research run, reviewed
    "sample": REPO / "runs" / "sample",                                           # no research: standing footer
}
# Lines that belong to the technical log (the appendix), never to page 1.
LOG_MARKERS = ("Run status:", "Page outcomes:", "Stopping reason:", "capture failed:", "searched, not found",
               "capture limitations:", "limitation stated by the model:", "Built on ")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _view(name: str) -> dict:
    run, decisions = load_run_dir(RUNS[name])
    return build_view(run, decisions, generated_at=FIXED_TIME)


def _pdf_pages(view: dict) -> list[str]:
    return [(p.extract_text() or "") for p in PdfReader(BytesIO(build_pdf(view))).pages]


def _pdf_body(pages: list[str], footer: str) -> str:
    """The pages' text without the footer and page-number lines, so a paragraph split by a page break reads whole."""
    lines = []
    for page in pages:
        lines += [ln for ln in page.splitlines()
                  if ln.strip() and ln.strip() != footer and not re.fullmatch(r"page \d+", ln.strip())]
    return _squash("".join(lines))


def _body_strings(docx_bytes: bytes) -> list[str]:
    """Every non-empty paragraph and table cell of the Word body, in document order (the snapshot format)."""
    doc = Document(BytesIO(docx_bytes))
    out = [p.text for p in doc.paragraphs if p.text.strip()]
    out += [c.text for t in doc.tables for r in t.rows for c in r.cells if c.text.strip()]
    return out


def _body_order(docx_bytes: bytes) -> list[tuple[str, str]]:
    """(kind, text) for each top-level body element in order: ('p', paragraph text) or ('tbl', first cell)."""
    doc = Document(BytesIO(docx_bytes))
    out = []
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:p"):
            out.append(("p", "".join(t.text or "" for t in el.iter(qn("w:t")))))
        elif el.tag == qn("w:tbl"):
            out.append(("tbl", "".join(t.text or "" for t in el.iter(qn("w:t")))))
    return out


@pytest.mark.parametrize("name", list(RUNS))
def test_pdf_page_one_holds_the_calendar_heading_and_the_first_row(name):
    view = _view(name)
    pages = _pdf_pages(view)
    page1 = pages[0]
    flat1 = _squash(page1)
    assert TITLE in page1                                   # the title is unchanged and opens the report
    assert _squash(view["run_line"]) in flat1               # one line: run number, companies, run date
    if view.get("research_label"):
        assert _squash(view["research_label"]) in flat1     # the review label exactly as today
    assert re.search(r"(?m)^Calendar$", page1), page1
    first = view["calendar"][0]
    assert _squash(first["date_display"]) in flat1
    assert _squash(first["event"]) in flat1
    assert _squash(first["review_label"]) in flat1
    assert "Summary" in page1                               # the counts sit directly under the table
    assert flat1.index("Calendar") < flat1.index("Summary")
    for marker in LOG_MARKERS:                              # the technical log is no longer on page 1
        assert marker not in page1, marker
    assert "Window:" in page1


@pytest.mark.parametrize("name", list(RUNS))
def test_docx_calendar_table_comes_before_any_log_text(name):
    view = _view(name)
    order = _body_order(build_docx(view))
    kinds = [k for k, _ in order]
    assert "tbl" in kinds
    table_at = kinds.index("tbl")
    before = [t for k, t in order[:table_at]]
    assert before[0] == TITLE
    assert before[1] == view["run_line"]
    assert all(not any(m in t for m in LOG_MARKERS) for t in before), before
    assert not any(line in before for line in view["research_lines"])
    # Section order after the table: Summary, Needs your review, placements, appendix, sources
    texts = [t for _, t in order]
    def at(label):
        return next(i for i, t in enumerate(texts) if t == label)
    assert table_at < at("Summary") < at(view["needs_review_heading"]) < at(APPENDIX_HEADING) < at("Sources")
    placed = [f"{label} · {len(view[key])}" for key, label in view["placement_sections"] if view.get(key)]
    positions = [at(p) for p in placed]
    assert positions == sorted(positions)
    assert all(at(view["needs_review_heading"]) < p < at(APPENDIX_HEADING) for p in positions)
    # the run record and research log open the appendix, unchanged
    appendix = texts[at(APPENDIX_HEADING):]
    assert appendix[1] == f"Run {view['run_number']}: {view['run_label']}"
    assert appendix[2] == f"Run date: {view['run_date']}"
    for line in view["research_lines"]:
        assert line in appendix


def test_placement_order_is_undated_then_later_then_context():
    assert [key for key, _ in PLACEMENT_SECTIONS] == ["undated", "timing_unresolved", "contingent", "later", "past"]


@pytest.mark.parametrize("name", list(RUNS))
def test_every_pre_change_string_is_still_present(name):
    """Layout only: each body string the old report carried (snapshot) is in the new Word file verbatim and in the
    new PDF text (whitespace removed, footer lines stripped so page breaks do not split a paragraph)."""
    view = _view(name)
    old = json.loads((SNAPSHOTS / f"{name}.strings.json").read_text())
    assert len(old) > 50
    new_docx = _body_strings(build_docx(view))
    missing_docx = [s for s in old if s not in new_docx]
    assert not missing_docx, missing_docx
    pdf = _pdf_body(_pdf_pages(view), view["footer"])
    missing_pdf = [s for s in old if _squash(s) not in pdf]
    assert not missing_pdf, missing_pdf
    # the title is unchanged
    assert old[0] == TITLE == new_docx[0]


@pytest.mark.parametrize("name", list(RUNS))
def test_calendar_status_never_says_kept_by_you_for_automatic_inclusion(name):
    view = _view(name)
    doc = Document(BytesIO(build_docx(view)))
    table = doc.tables[0]
    assert [c.text for c in table.rows[0].cells] == ["Date / period", "Ticker", "Program", "Event", "Source", "Status"]
    for row, cells in zip(view["calendar"], table.rows[1:]):
        status = cells.cells[5].text
        assert status.startswith(row["review_label"])
        if row["inclusion"] != "kept":
            assert "Kept by you" not in status, status
    automatic = [r for r in view["calendar"] if r["inclusion"] != "kept"]
    assert automatic, "fixture should carry at least one automatically included row"


def test_footer_names_the_reviewer_that_ran():
    view = _view("e2e_reviewed")
    assert view["footer"] == "Research: Muse Spark · Review: Muse Spark Open · Built by code from the saved results"
    assert view["models_used"] == {"researcher": "Muse Spark", "reviewers": ["Muse Spark Open"], "adjudicators": []}
    for i, page in enumerate(_pdf_pages(view)):
        assert view["footer"] in page, f"footer missing on PDF page {i + 1}"
    doc = Document(BytesIO(build_docx(view)))
    assert any(view["footer"] in p.text for s in doc.sections for p in s.footer.paragraphs)
    assert "Built with Meta Muse models" not in view["footer"]


def test_footer_for_a_research_run_without_a_review_says_so():
    view = _view("e2e_brief")
    assert view["footer"] == "Research: Muse Spark · Review: none ran · Built by code from the saved results"


def test_footer_names_deepseek_and_the_adjudicator_from_the_run_record():
    """The reviewer that RAN, from run.research.review (reviewer_ran), plus the adjudicator when disputes reached it."""
    run, _ = load_run_dir(RUNS["e2e_reviewed"])
    run = copy.deepcopy(run)
    entry = run["run"]["research"]["review"]["PTGX"]
    entry["reviewer"] = "DeepSeek V4 Pro"
    entry["reviewer_ran"] = {"name": "DeepSeek V4 Pro", "model": "deepseek-ai/DeepSeek-V4-Pro", "provider": "baseten",
                             "family": "DeepSeek"}
    entry["adjudication"] = {"stage": "adjudication", "status": "complete", "outcomes": [
        {"dispute_id": "PTGX-F1", "outcome": "upheld", "model": "muse-wonder"}], "usage": {"model_requests": 1}}
    assert footer_line(run) == ("Research: Muse Spark · Review: DeepSeek V4 Pro · Adjudication: Muse Wonder · "
                                "Built by code from the saved results")
    # the declared fallback that ran instead of the configured reviewer is what the footer names
    entry["reviewer"] = "Muse Spark Open"
    entry["reviewer_ran"] = {"name": "Muse Spark Open", "model": "meta-models/muse-spark-open-1026",
                             "provider": "baseten", "family": "Muse Spark (Meta)"}
    entry["adjudication"] = {"stage": "adjudication", "status": "not_needed", "outcomes": [], "usage": None}
    run["run"]["research"]["reviewer_access"] = {"name": "DeepSeek V4 Pro", "refused": True, "chosen": "Muse Spark Open"}
    assert footer_line(run) == "Research: Muse Spark · Review: Muse Spark Open · Built by code from the saved results"
    assert "DeepSeek" not in footer_line(run)


def test_runs_without_research_keep_the_standing_footer():
    view = _view("sample")
    assert view["footer"] == FOOTER == "Built with Meta Muse models · built by code from the saved results"
    assert view["models_used"] is None
    assert models_used({"run": {}}) is None
    for page in _pdf_pages(view):
        assert FOOTER in page
