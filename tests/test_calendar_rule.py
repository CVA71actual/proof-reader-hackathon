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
import json
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


def producer_finding(**over):
    """A finding in the S2 producer's shape (runs/research.py save_review on claude/research-s2-wire @ 8cd269a)."""
    f = {"finding_id": "F1", "attempt": 1, "kind": "lost_condition",
         "text": "The proposed event omits the condition subject to FDA feedback.", "by": "Muse Spark Open",
         "row_id": "e1", "revision_id": 1, "current": True, "basis": "synthetic-basis", "evidence": [],
         "review_status": "complete", "reviewed_at": AT,
         "adjudication": {"outcome": "not_sent", "human_accepted": False, "reason": "Not sent."}}
    f.update(over)
    return f


def research_run(findings):
    run = clean_run()
    run["rows"][0]["research"] = {"revision": 1, "placement_proposal": "upcoming", "evidence": [],
                                  "review_findings": findings}
    return run


def test_current_producer_finding_holds_the_row_and_its_text_reaches_state_pdf_and_docx():
    reason = "The proposed event omits the condition subject to FDA feedback."
    run = research_run([producer_finding()])
    t = target(run)
    assert t["inclusion"] == "review" and not t["on_calendar"] and t["needs_attention"]
    assert t["issues"] == [f"The reviewer found lost condition: {reason}"]
    v = view(run)
    assert v["calendar"] == [] and v["open_rows"][0]["issues"] == t["issues"]
    for text in (pdf_text(v), docx_text(v)):
        assert reason in " ".join(text.split())
    # positive control: no findings, and an empty list, leave the clean row included
    assert target(research_run([]))["inclusion"] == "included"
    del run["rows"][0]["research"]["review_findings"]
    assert target(run)["inclusion"] == "included"
    # a current bound Keep still overrides
    held = research_run([producer_finding()])
    assert target(held, [kept_decision(held)])["inclusion"] == "kept"


def test_explanation_is_only_an_alias_of_the_producer_text():
    f = producer_finding()
    f["explanation"] = f.pop("text")
    assert target(research_run([f]))["issues"] == [
        "The reviewer found lost condition: The proposed event omits the condition subject to FDA feedback."]


def test_stale_or_noncurrent_findings_are_history_not_issues():
    old = producer_finding(revision_id=1, current=False, stale=True,
                           stale_reason="The row's revision or evidence changed after this finding was made.",
                           text="The earlier proposal omitted a condition.")
    run = research_run([old])
    run["rows"][0]["research"]["revision"] = 2
    t = target(run)
    assert t["issues"] == [] and t["inclusion"] == "included"
    assert t["review_findings_history"] == ["The reviewer found lost condition: The earlier proposal omitted a condition."]
    assert run["rows"][0]["research"]["review_findings"] == [old]          # the record is untouched
    # negative control: the same row with one current finding beside the stale one is held
    run["rows"][0]["research"]["review_findings"] = [old, producer_finding(revision_id=2)]
    assert target(run)["inclusion"] == "review"


def test_unknown_currentness_fails_closed_to_review():
    legacy = producer_finding()
    del legacy["current"]
    t = target(research_run([legacy]))
    assert t["inclusion"] == "review" and "(not established as current)" in t["issues"][0]
    # current:true but for another revision or another row cannot be taken as current or as history
    for f in (producer_finding(revision_id=7), producer_finding(row_id="e9")):
        t = target(research_run([f]))
        assert t["inclusion"] == "review" and "not established as current: it names row" in t["issues"][0]
    t = target(research_run("not a list"))
    assert t["issues"] == ["The reviewer's finding could not be read"]
    t = target(research_run(["a plain note"]))
    assert t["inclusion"] == "review"


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


def test_placement_reads_the_code_derived_period_bounds_never_the_raw_row_dates():
    # The raw row has no date/date_end; the passing date check derives early Q1 2027 = January 2027.
    run = case_run("Cedar Bio will release financial results in early Q1 2027.", "early Q1 2027", None)
    row = run["rows"][0]
    assert row["date"] is None and row.get("date_end") is None
    state = current_state(run, [])
    t = state["targets"]["e1"]
    assert (t["values"]["date"], t["values"]["date_end"]) == ("2027-01-01", "2027-01-31")
    assert t["issues"] == [] and t["placement"] == "calendar" and t["inclusion"] == "included"
    partition, _, _ = run_interval_partition(run, state)
    assert [(e["id"], e["date"], e["date_end"]) for e in partition["calendar"]] == [("e1", "2027-01-01", "2027-01-31")]
    assert partition["undated"] == [] and partition["timing_unresolved"] == []
    v = view(run)
    assert [r["id"] for r in v["calendar"]] == ["e1"] and v["undated"] == []


def test_failed_secondary_span_support_from_the_real_intake_holds_the_row(tmp_path):
    """NEXT-CALLER-001: the S1 propose/check tool path records a cited span that is not on the page; the row must
    not be placed automatically (Manus 19:32 item 1). Positive control: the same intake with valid spans only."""
    from tests import research_fakes as F
    from tests.test_research_s1 import QUOTE, _propose_run
    good = [{"source_id": "src-r1-ptgx", "page_number": 1, "quote": QUOTE, "word_ids": None, "roles": ["action", "timing"]},
            {"source_id": "src-r1-ptgx", "page_number": 1, "quote": "subject to FDA feedback.", "word_ids": None,
             "roles": ["condition"]}]
    bad = [*good, {"source_id": "src-r1-ptgx", "page_number": 1, "quote": "not on the page at all",
                   "word_ids": ["w99999"], "roles": ["context"]}]

    def placed(spans, name):
        runs = tmp_path / name
        runs.mkdir()
        ev = F.event("ICARUS readout", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30",
                     spans=spans, conditions=["subject to FDA feedback"])
        _, _, run = _propose_run(runs, [[ev]])
        row = next(r for r in run["rows"] if r.get("research"))
        return run, row, current_state(run, [])["targets"][row["id"]]

    run, row, t = placed(good, "good")
    assert t["issues"] == [] and t["inclusion"] == "included", t["issues"]
    run, row, t = placed(bad, "bad")
    assert {"quote_not_found", "locator_mismatch"} <= {i["code"] for i in row["research"]["issues"]}
    assert not [c for c in row["checks"] if c["result"] == "fail"]      # the ordinary row checks all pass
    assert t["inclusion"] == "review" and not t["on_calendar"] and t["needs_attention"]
    assert any(i.startswith("Cited evidence: The quote of span 2") for i in t["issues"])
    assert any(i.startswith("Cited evidence: Word ids w99999") for i in t["issues"])
    v = build_view(run, [], generated_at=AT)
    assert row["id"] not in [r["id"] for r in v["calendar"]]
    assert [r["id"] for r in v["open_rows"]] == [row["id"]]
    for text in (pdf_text(v), docx_text(v)):
        assert "Issues: Cited evidence: The quote of span 2" in " ".join(text.split())
    # a current bound Keep still overrides
    from tests.pivot_assertions import fresh_keep
    kept = current_state(run, [fresh_keep(run, [], row["id"])])["targets"][row["id"]]
    assert kept["inclusion"] == "kept" and kept["issues"] == []


def _two_page_research_run(tmp_path):
    """The S1 intake on a two-page PDF: primary action/timing on page 1, the FDA condition on page 2."""
    import pymupdf
    from tests import research_fakes as F
    from tests.test_research_s1 import PR1, QUOTE, _propose_run
    doc = pymupdf.open()
    for text in (QUOTE, "This timing is subject to FDA feedback."):
        page = doc.new_page(width=612, height=792)
        page.insert_text((54, 72), text, fontsize=11)
    raw = doc.tobytes()
    doc.close()
    spans = [{"source_id": "src-r1-ptgx", "page_number": 1, "quote": QUOTE, "word_ids": None, "roles": ["action", "timing"]},
             {"source_id": "src-r1-ptgx", "page_number": 2, "quote": "subject to FDA feedback.", "word_ids": None,
              "roles": ["condition"]}]
    ev = F.event("ICARUS readout", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30",
                 spans=spans, conditions=["subject to FDA feedback"])
    _, rid, run = _propose_run(tmp_path, [[ev]], pages={PR1: (200, "application/pdf", raw)})
    return tmp_path / rid, run


def _replace_secondary_words(directory, run, keep_condition):
    """A correctly hashed, image-bound derived transcription replaces page 2's consumed words after intake."""
    import hashlib
    page = run["sources"][0]["pages"][1]
    words = json.loads((directory / page["words"]).read_text())
    for w in words["words"]:
        if not keep_condition:
            w["text"] = "unrelated"
    encoded = (json.dumps(words) + "\n").encode()
    rel = "pages/secondary-derived.words.json"
    (directory / rel).write_bytes(encoded)
    page["derived_words"] = {"file": rel, "sha256": hashlib.sha256(encoded).hexdigest(),
                             "based_on_image_sha256": page["image_sha256"]}
    (directory / "run.json").write_text(json.dumps(run) + "\n")


def test_a_secondary_span_whose_consumed_words_change_after_intake_goes_to_review(tmp_path):
    """Manus CALENDAR-NEW-001 (boundary_tests.py): the loader re-runs each span check on the words it consumes now."""
    from report.data import load_run_dir
    from tests.pivot_assertions import fresh_keep
    directory, run = _two_page_research_run(tmp_path / "neg")
    row_id = next(r for r in run["rows"] if r.get("research"))["id"]
    before = current_state(run, [])["targets"][row_id]
    assert before["inclusion"] == "included" and before["issues"] == []
    old_keep = fresh_keep(run, [], row_id)
    _replace_secondary_words(directory, run, keep_condition=False)
    loaded, _ = load_run_dir(directory)
    span = next(r for r in loaded["rows"] if r["id"] == row_id)["research"]["evidence"][1]
    assert span["quote_match"] == "fail" and span["check_history"][0]["quote_match"] == "pass"
    after = current_state(loaded, [])["targets"][row_id]
    assert after["review_basis"] != before["review_basis"]
    assert after["inclusion"] == "review" and not after["on_calendar"]
    assert any("span 1 is not printed on src-r1-ptgx page 2" in i and "changed after it was first checked" in i
               for i in after["issues"])
    v = build_view(loaded, [], generated_at=AT)
    assert row_id not in [r["id"] for r in v["calendar"]] and [r["id"] for r in v["open_rows"]] == [row_id]
    for text in (pdf_text(v), docx_text(v)):
        assert "Issues: Cited evidence: The quote of span 1" in " ".join(text.split())
    # the Keep made before the change is history; a deliberate Keep of the changed proposal is kept
    assert current_state(loaded, [old_keep])["targets"][row_id]["inclusion"] == "review"
    kept = current_state(loaded, [old_keep, fresh_keep(loaded, [old_keep], row_id)])["targets"][row_id]
    assert kept["inclusion"] == "kept"


def test_a_changed_transcription_that_still_supports_the_span_stays_included(tmp_path):
    from report.data import load_run_dir
    directory, run = _two_page_research_run(tmp_path / "pos")
    row_id = next(r for r in run["rows"] if r.get("research"))["id"]
    _replace_secondary_words(directory, run, keep_condition=True)
    loaded, _ = load_run_dir(directory)
    t = current_state(loaded, [])["targets"][row_id]
    assert t["issues"] == [] and t["inclusion"] == "included"
