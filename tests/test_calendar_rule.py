"""CALENDAR-RULE-001 (CVA conflict-review rule, 2026-11-02). All data here is synthetic; no model is called.

One inclusion predicate in runs.review.review_projection, no second status store:
  - a clean proposal (not excluded, no issues, no reviewer findings, period in the window tested on its end,
    future-facing) is on the calendar automatically as "Included — not reviewed by you";
  - a row with issues or reviewer findings goes to review, off the calendar until a person keeps it, reasons shown;
  - a past row is Context (past); an undated row is "Undated: needs timing"; neither is on a date of the calendar;
  - Remove, Undo and history work on every row, including automatically included ones;
  - nothing ever says "Kept by you" for automatic inclusion, and counts keep the two apart.
"""
import copy
from io import BytesIO

from docx import Document
from pypdf import PdfReader

from report.data import build_view
from report.docx_report import build_docx
from report.pdf_report import build_pdf
from runs.calendar import run_interval_partition
from runs.review import CONTEXT_LABEL, INCLUDED_LABEL, UNDATED_LABEL
from runs.state import current_state
from tests.test_pivot_proposal_flow import AT, CASES, decision, kept_decision, make_run

EARNINGS = CASES[0]          # November 12, 2026; the run is as of 2026-11-01


def clean_run(**updates):
    _, run = make_run(EARNINGS, event_updates=updates or None)
    return run


def case_run(quote, date_text, day):
    """A clean synthetic row whose page words are its own quote (so the quote checks can pass)."""
    _, run = make_run(("custom", quote, "earnings release", date_text, day, []))
    return run


def target(run, log=()):
    return current_state(run, list(log))["targets"]["e1"]


def view(run, log=()):
    return build_view(run, list(log), generated_at=AT)


def pdf_text(v):
    return "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(build_pdf(v))).pages)


def docx_text(v):
    doc = Document(BytesIO(build_docx(v)))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def test_clean_in_window_proposal_is_included_automatically_never_kept():
    run = clean_run()
    state = current_state(run, [])
    t = state["targets"]["e1"]
    assert t["issues"] == [] and t["placement"] == "calendar"
    assert t["inclusion"] == "included" and t["on_calendar"] and not t["needs_attention"]
    assert t["review_label"] == INCLUDED_LABEL and t["decision_label"] is None
    assert not t["accepted"] and t["semantic_status"] == "unresolved"     # GROUP-AXIS: no human acceptance
    assert "not reviewed by you" in t["reason"] and "must decide" not in t["reason"]
    assert state["counts"]["inclusion"]["included"] == 1 and state["counts"]["inclusion"]["kept"] == 0
    assert state["unresolved"] == 0 and state["kept_rows"] == 0
    partition, _, _ = run_interval_partition(run, state)
    assert [e["id"] for e in partition["calendar"]] == ["e1"]
    assert partition["calendar"][0]["inclusion"] == "included"


def test_keep_is_still_kept_by_you_and_counted_apart():
    run = clean_run()
    keep = kept_decision(run)
    state = current_state(run, [keep])
    t = state["targets"]["e1"]
    assert t["inclusion"] == "kept" and t["accepted"] and t["decision_label"] == "Kept by you"
    assert state["counts"]["inclusion"]["kept"] == 1 and state["counts"]["inclusion"]["included"] == 0


def test_row_with_an_issue_goes_to_review_off_the_calendar_with_its_reason():
    run = clean_run(quote="Nothing like this is printed on the page at all.")
    state = current_state(run, [])
    t = state["targets"]["e1"]
    assert t["issues"] and t["inclusion"] == "review"
    assert t["needs_attention"] and not t["on_calendar"]
    assert state["unresolved"] == 1
    v = view(run)
    assert v["calendar"] == [] and [r["id"] for r in v["open_rows"]] == ["e1"]
    assert v["open_rows"][0]["issues"] == t["issues"]
    for text in (pdf_text(v), docx_text(v)):
        flat = " ".join(text.split())
        # (the PDF may break a page inside the line, so only the start of the reason is compared there)
        assert "Needs your review" in flat and "Issues: Quote found on page" in flat
    # A person's Keep puts it on the calendar as kept, not as automatically included.
    kept = target(run, [kept_decision(run)])
    assert kept["inclusion"] == "kept" and kept["on_calendar"]


def test_reviewer_findings_send_a_research_row_to_review():
    run = clean_run()
    run["rows"][0]["research"] = {"placement_proposal": "upcoming", "review_findings": [
        {"finding_id": "f1", "kind": "contradictory_support", "row_ids": ["e1"],
         "explanation": "The second source gives a different quarter."}]}
    t = target(run)
    assert t["inclusion"] == "review" and not t["on_calendar"]
    assert t["issues"] == ["The reviewer found contradictory support: The second source gives a different quarter."]
    run["rows"][0]["research"]["review_findings"] = []
    assert target(run)["inclusion"] == "included"
    del run["rows"][0]["research"]["review_findings"]
    assert target(run)["inclusion"] == "included"
    run["rows"][0]["research"]["review_findings"] = "not a list"
    assert target(run)["issues"] == ["The reviewer's finding could not be read"]


def test_research_context_placement_is_never_on_the_calendar_even_in_the_window():
    run = clean_run()
    run["rows"][0]["research"] = {"placement_proposal": "context"}
    t = target(run)
    assert t["placement"] == "past" and t["inclusion"] == "context" and t["review_label"] == CONTEXT_LABEL
    v = view(run)
    assert v["calendar"] == [] and [r["id"] for r in v["past"]] == ["e1"]


def test_past_row_goes_to_context_not_the_calendar():
    run = case_run("Cedar Bio released financial results on October 12, 2026.", "October 12, 2026", "2026-10-12")
    t = target(run)
    assert t["issues"] == [], t["issues"]
    assert t["placement"] == "past" and t["inclusion"] == "context" and t["review_label"] == CONTEXT_LABEL
    assert t["on_calendar"] and not t["needs_attention"]    # eligible for the partition, placed in Past
    v = view(run)
    assert v["calendar"] == [] and [r["id"] for r in v["past"]] == ["e1"]
    assert v["counts"]["context"] == 1 and v["unresolved"] == 0
    for text in (pdf_text(v), docx_text(v)):
        assert "Context (past) · 1" in text


def test_period_is_tested_on_its_end():
    # Q4 2026 started before the run (2026-11-01) but ends inside the window: it is on the calendar.
    run = case_run("Cedar Bio will release financial results in Q4 2026.", "Q4 2026", None)
    t = target(run)
    assert t["issues"] == [], t["issues"]
    assert t["values"]["date_end"] == "2026-12-31"
    assert t["placement"] == "calendar" and t["inclusion"] == "included"


def test_undated_row_is_listed_as_needing_timing_never_on_a_date():
    run = case_run("Cedar Bio will release financial results.", None, None)
    t = target(run)
    assert t["issues"] == [], t["issues"]
    assert t["inclusion"] == "undated" and t["review_label"] == UNDATED_LABEL
    v = view(run)
    assert v["calendar"] == [] and [r["id"] for r in v["undated"]] == ["e1"]
    assert v["undated"][0]["date_display"] == "Undated · no timing stated"
    for text in (pdf_text(v), docx_text(v)):
        assert "Undated: needs timing · 1" in text


def test_remove_and_undo_on_an_included_row():
    run = clean_run()
    remove = {**decision("remove"), "note": "Not a catalyst for us."}
    removed = current_state(run, [remove])
    t = removed["targets"]["e1"]
    assert t["inclusion"] == "excluded" and not t["on_calendar"] and t["review_label"] == "Removed by you"
    assert removed["counts"]["inclusion"]["included"] == 0 and removed["counts"]["inclusion"]["excluded"] == 1
    assert [x["rule"] for x in removed["exclusions"]] == ["person_removed"]
    v = view(run, [remove])
    assert v["calendar"] == [] and [x["id"] for x in v["exclusions"]] == ["e1"]
    # Undo restores the recomputed inclusion; the history keeps both decisions in the log.
    log = [remove, decision("undo")]
    back = target(run, log)
    assert back["inclusion"] == "included" and back["on_calendar"] and back["last_action"] is None
    assert [r["id"] for r in view(run, log)["calendar"]] == ["e1"]


def test_expired_keep_is_history_and_an_issue_never_auto_included():
    run = clean_run()
    keep = kept_decision(run)
    changed = copy.deepcopy(run)
    changed["sources"][0]["published_date"] = "2026-10-31"     # source metadata changed after the Keep
    t = target(changed, [keep])
    assert not t["accepted"] and t["review_history"]
    assert "Your earlier Keep expired because the evidence changed" in t["issues"]
    assert t["inclusion"] == "review" and not t["on_calendar"]


def test_exports_never_say_kept_by_you_for_automatic_inclusion():
    run = clean_run()
    v = view(run)
    assert v["counts"]["included"] == 1 and v["counts"]["kept"] == 0
    assert v["calendar"][0]["person_tag"] is None and v["calendar"][0]["review_label"] == INCLUDED_LABEL
    for text in (pdf_text(v), docx_text(v)):
        flat = " ".join(text.split())
        assert "Included — not reviewed by you" in flat
        assert "Included automatically — not reviewed by you: 1" in flat
        assert "Kept by you: 0" in flat
        assert "Kept by you ·" not in flat and "· Kept by you" not in flat
    kept_view = view(run, [kept_decision(run)])
    for text in (pdf_text(kept_view), docx_text(kept_view)):
        flat = " ".join(text.split())
        assert "Kept by you: 1" in flat and "Included automatically — not reviewed by you: 0" in flat
        assert "Accepted by you · Kept by you" in flat


def test_undated_research_context_is_context_not_needing_timing():
    run = case_run("Cedar Bio released financial results.", None, None)
    run["rows"][0]["research"] = {"placement_proposal": "context"}
    t = target(run)
    assert t["issues"] == [] and t["placement"] == "past" and t["inclusion"] == "context"


def test_rows_without_current_support_are_never_placed_automatically():
    run = clean_run()
    # an edit not yet re-checked
    edit = {**decision("fix"), "fix": {"field": "title", "from": run["rows"][0]["title"], "to": "Edited"}}
    t = target(run, [edit])
    assert "Your edit has not been checked yet" in t["issues"] and t["inclusion"] == "review"
    # saved checks that no longer bind to the page are history, and the row waits for review
    stale = copy.deepcopy(run)
    stale["sources"][0]["pages"][0]["words_sha256"] = "0" * 64
    t = target(stale)
    assert t["issues"] == ["The saved checks no longer match this page; they are history"]
    assert t["inclusion"] == "review" and not t["on_calendar"]
    # a row only the code's coverage scan found
    found = copy.deepcopy(run)
    found["rows"][0]["found_by"] = "coverage"
    assert target(found)["inclusion"] == "review"
