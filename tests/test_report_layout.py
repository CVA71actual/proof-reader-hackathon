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
    # the build-time line is local time, so the snapshot's wording depends on the machine's time zone: require
    # that a "Built on" line is present, and compare every other string verbatim
    assert any(s.startswith("Built on ") for s in new_docx)
    old = [s for s in old if not s.startswith("Built on ")]
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


def _refused_deepseek_record() -> dict:
    """The real reviewer producer with DeepSeek ready and the spend gate refusing the first call (Codex PR183 item 3,
    from Manus's producer probe): status incomplete, zero model requests, zero chat calls."""
    from capture import research_reviewer as rv

    class FakeDeepSeek:
        model, name, provider, family, price_name = ("deepseek-ai/DeepSeek-V4-Pro", "DeepSeek V4 Pro", "baseten",
                                                     "DeepSeek", "DeepSeek V4 Pro")
        called = 0

        def ready(self):
            return None

        def chat(self, **kwargs):
            self.called += 1
            raise AssertionError("no call should be admitted")

    def no_network(_):
        raise AssertionError("no search or capture")

    model = FakeDeepSeek()
    record = rv.review_draft(company={"ticker": "PTGX", "name": "Synthetic company", "as_of": "2026-11-03"},
                             assignment="Synthetic omission review", proposals=[], sources=[], search=no_network,
                             fetch_capture=no_network, spend_gate=lambda req: None, model=model)
    assert model.called == 0 and record["status"] == "incomplete" and record["usage"]["model_requests"] == 0
    return record


DEEPSEEK = {"name": "DeepSeek V4 Pro", "model": "deepseek-ai/DeepSeek-V4-Pro", "provider": "baseten",
            "family": "DeepSeek"}


def _with_review(record: dict | None, usage) -> dict:
    """The reviewed fixture with its PTGX entry replaced the way runs/research.py save_review persists one: the
    selected identity as reviewer_ran, the review record, and the company's review usage (absent = legacy)."""
    run, _ = load_run_dir(RUNS["e2e_reviewed"])
    run = copy.deepcopy(run)
    entry = run["run"]["research"]["review"]["PTGX"]
    entry.update({"status": (record or {}).get("status", entry["status"]), "reviewer": DEEPSEEK["name"],
                  "reviewer_ran": dict(DEEPSEEK), "reviewer_choice": dict(DEEPSEEK),
                  "adjudication": {"stage": "adjudication", "status": "not_needed", "outcomes": [], "usage": None}})
    if record is not None:
        entry["review"] = record
    if usage is None:
        entry.pop("usage", None)
        entry["review"] = {k: v for k, v in entry["review"].items() if k != "usage"}
    else:
        entry["usage"] = usage
    run["run"]["research"]["reviewer_access"] = {"name": "DeepSeek V4 Pro", "chosen": "DeepSeek V4 Pro"}
    return run


def test_footer_does_not_name_a_selected_reviewer_that_made_zero_calls():
    """Selection is not execution: DeepSeek selected, first call refused by the spend gate, model_requests 0. The
    footer, models_used and the counters line name no reviewer; the access record still shows the choice."""
    record = _refused_deepseek_record()
    run = _with_review(record, dict(record["usage"]))
    assert footer_line(run) == "Research: Muse Spark · Review: none ran · Built by code from the saved results"
    assert models_used(run)["reviewers"] == []
    view = build_view(run, [], generated_at=FIXED_TIME)
    assert "DeepSeek" not in view["footer"]
    assert "by DeepSeek" not in view["counters_line"]
    assert "research review not started: no reviewer calls were made" in view["counters_line"]
    assert run["run"]["research"]["reviewer_access"]["chosen"] == "DeepSeek V4 Pro"      # access history kept
    for page in _pdf_pages(view):
        assert "Review: none ran" in page and "DeepSeek V4 Pro ·" not in page


def test_footer_names_an_incomplete_review_that_made_calls():
    """An attempted-but-incomplete review still names the model that made the calls."""
    record = _refused_deepseek_record()
    record = {**record, "usage": {**record["usage"], "model_requests": 3}}
    run = _with_review(record, {"model_requests": 3, "search_requests": 0})
    assert run["run"]["research"]["review"]["PTGX"]["status"] == "incomplete"
    assert footer_line(run) == "Research: Muse Spark · Review: DeepSeek V4 Pro · Built by code from the saved results"
    view = build_view(run, [], generated_at=FIXED_TIME)
    assert "by DeepSeek V4 Pro" in view["counters_line"] and "(review incomplete)" in view["counters_line"]


def test_footer_keeps_the_recorded_name_when_legacy_usage_is_missing():
    """A legacy entry with no usage recorded anywhere is unknown, not zero: the recorded reviewer stays named."""
    run = _with_review(None, None)
    entry = run["run"]["research"]["review"]["PTGX"]
    assert "usage" not in entry and "usage" not in entry["review"]
    assert footer_line(run) == "Research: Muse Spark · Review: DeepSeek V4 Pro · Built by code from the saved results"


def test_adjudicator_named_only_when_it_made_calls():
    run, _ = load_run_dir(RUNS["e2e_reviewed"])
    run = copy.deepcopy(run)
    entry = run["run"]["research"]["review"]["PTGX"]
    entry["adjudication"] = {"stage": "adjudication", "status": "complete", "outcomes": [], "usage": {
        "model_requests": 0}}
    assert "Adjudication" not in footer_line(run)
    entry["adjudication"]["usage"]["model_requests"] = 2
    assert "Adjudication: Muse Wonder" in footer_line(run)


def test_long_multi_reviewer_footer_wraps_clear_of_the_page_number():
    """Manus layout item: three reviewers plus the adjudicator wrap in the PDF instead of running under 'page N'."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.lib.utils import simpleSplit
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch

    run, decisions = load_run_dir(RUNS["e2e_reviewed"])
    wide = copy.deepcopy(run)
    reviews = wide["run"]["research"]["review"]
    seed = copy.deepcopy(reviews["PTGX"])
    for t, name, family in [("PTGX", "DeepSeek V4 Pro", "DeepSeek"), ("ROIV", "Claude Opus 5.5", "Claude"),
                            ("IMVT", "Muse Spark Open", "Muse Spark (Meta)")]:
        e = copy.deepcopy(seed)
        e["reviewer"] = name
        e["reviewer_ran"] = {"name": name, "family": family, "provider": "synthetic"}
        e["adjudication"] = {"stage": "adjudication", "status": "complete", "outcomes": [{"model": "muse-wonder"}],
                             "usage": {"model_requests": 1}}
        reviews[t] = e
    view = build_view(wide, decisions, generated_at=FIXED_TIME)
    footer = view["footer"]
    assert "Review: DeepSeek V4 Pro and Claude Opus 5.5 and Muse Spark Open" in footer
    assert 0.75 * inch + stringWidth(footer, "Helvetica", 7) > letter[0] - 0.75 * inch - stringWidth("page 1",
                                                                                                     "Helvetica", 7)
    room = letter[0] - 1.5 * inch - stringWidth("page 99", "Helvetica", 7) - 12
    lines = simpleSplit(footer, "Helvetica", 7, room)
    assert len(lines) >= 2
    import pymupdf
    pdf = pymupdf.open(stream=build_pdf(view), filetype="pdf")
    for page in pdf:
        number = next(b for b in page.get_text("words") if b[4] == "page")
        footer_words = [w for w in page.get_text("words") if w[1] > number[1] - 1 and w[4] != "page"
                        and not w[4].isdigit()]
        assert footer_words, "footer missing"
        assert max(w[2] for w in footer_words if abs(w[1] - number[1]) < 2) < number[0], "footer runs under page N"
        assert " ".join(w[4] for w in footer_words) == " ".join(footer.split())


def test_runs_without_research_keep_the_standing_footer():
    view = _view("sample")
    assert view["footer"] == FOOTER == "Built with Meta Muse models · built by code from the saved results"
    assert view["models_used"] is None
    assert models_used({"run": {}}) is None
    for page in _pdf_pages(view):
        assert FOOTER in page


@pytest.mark.parametrize("retry_name", ["DeepSeek V4 Pro", "Different reviewer"])
def test_refused_retry_keeps_the_actual_earlier_reviewer_and_adjudicator(retry_name):
    record = _refused_deepseek_record()
    run = _with_review(record, dict(record["usage"]))
    entry = run["run"]["research"]["review"]["PTGX"]
    earlier = copy.deepcopy(entry)
    earlier["review"]["usage"]["model_requests"] = 2
    earlier["adjudicator"] = "Earlier adjudicator"
    earlier["adjudication"] = {"status": "incomplete", "usage": {"model_requests": 1}}
    entry["attempts"] = [earlier]
    entry["reviewer"] = retry_name
    entry["reviewer_ran"] = {**DEEPSEEK, "name": retry_name}
    assert models_used(run)["reviewers"] == ["DeepSeek V4 Pro"]
    assert models_used(run)["adjudicators"] == ["Earlier adjudicator"]
    view = build_view(run, [], generated_at=FIXED_TIME)
    assert "Review: DeepSeek V4 Pro" in view["footer"]
    assert "no reviewer calls were made" not in view["counters_line"]
    assert "(review incomplete)" in view["counters_line"]
    assert "Different reviewer" not in view["footer"]
    # The report survives a save/reload and never assigns the retry identity to history.
    assert models_used(json.loads(json.dumps(run))) == models_used(run)


def test_old_attempt_without_identity_or_usage_stays_unknown_after_refused_retry():
    record = _refused_deepseek_record()
    run = _with_review(record, dict(record["usage"]))
    run["run"]["research"]["review"]["PTGX"]["attempts"] = [
        {"attempt": 1, "status": "incomplete"}]
    view = build_view(run, [], generated_at=FIXED_TIME)
    assert "Review: unknown (record incomplete)" in view["footer"]
    assert "Review: none ran" not in view["footer"]
    assert "DeepSeek" not in view["footer"]
    assert "no reviewer calls were made" not in view["counters_line"]
