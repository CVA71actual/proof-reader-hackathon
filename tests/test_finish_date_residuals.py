"""Codex 5b residual rulings (2026-11-02), DATE-001 / UNKNOWN-001 / IDENTITY-001 / NEXT-CALLER-001.

1. A day with no printed year ('March 18') never rolls into a year, next year included: no ISO bounds, not a failed
   claim, timing unresolved through the real build_row -> state -> calendar -> report path.
2. A dateline past the header scan (DATELINE_WORDS) counts only as a captured explicit anchor (the quote the date is
   compared against prints it); the filing date is never silently substituted for an ambiguous release date.
"""
from datetime import date

from checks import event_checks as ec
from report.data import build_view
from runs import build_entry
from runs.calendar import calendar_entries
from runs.state import current_state
from tests.test_pivot_proposal_flow import AT, make_run

YEARLESS = ("yearless", "Cedar Bio expects CB-100 data on March 18.", "clinical readout", "March 18",
            "2027-03-18", [])


def _date_check(row):
    return next(c for c in row["checks"] if c["name"] == ec.DATE_ID)


def test_march_18_under_a_november_filing_is_not_dated_2027_anywhere():
    _, run = make_run(YEARLESS)
    row = run["rows"][0]
    assert row["date_text"] == "March 18" and row["date"] is None and row["date_end"] is None
    assert any(t["step"] == "Year not printed" and "2027-03-18" in t["text"] for t in row["trail"])
    check = _date_check(row)
    assert check["result"] == "not_needed"            # not a failed claim
    assert check["compared"]["timing"] == {"wording": "March 18", "start": None, "end": None,
                                           "precision": "unresolved"}
    target = current_state(run, [])["targets"]["e1"]
    assert target["values"]["date"] is None and target["values"]["date_end"] is None
    assert target["timing_state"] == "timing_unresolved" and target["status"] != "failed"
    keep = {"at": AT, "action": "keep", "by": "person", "target": "e1", "fix": None, "note": None}
    state = current_state(run, [keep])
    for entry in calendar_entries(run, state):
        assert entry["date"] is None and entry["date_end"] is None
    view = build_view(run, [keep], generated_at=AT)
    shown = [r for key in ("calendar", "past", "later", "undated", "contingent") for r in view.get(key, [])]
    assert all(r.get("id") != "e1" for r in shown)
    assert "2027-03-18" not in repr(view)


def test_a_yearless_day_is_never_rolled_whatever_the_filing_date():
    for filed in ("2026-11-02", "2026-03-01", "2027-03-19", None):
        assert ec.parse_date_phrase("March 18", filed) == ("day", None, None)
        assert ec.date_match("March 18", "2027-03-18", "Data on March 18.", filed=filed).result == "could_not_run"
        assert ec.date_match("March 18", "2026-03-18", "Data on March 18.", filed=filed).result == "could_not_run"
    # a printed year is still compared literally
    assert ec.date_match("March 18, 2027", "2027-03-18", "Data on March 18, 2027.").result == "pass"
    assert ec.date_match("March 18, 2027", "2026-03-18", "Data on March 18, 2027.").result == "fail"


def _page(text):
    words = [{"id": f"w{i}", "text": t, "x0": 10.0, "y0": 10.0 + 12 * i, "x1": 60.0, "y1": 20.0 + 12 * i}
             for i, t in enumerate(text.split())]
    return {"words": words, "page_width": 612.0, "page_height": 792.0}


NAV = " ".join(f"menu{i}" for i in range(ec.DATELINE_WORDS + 20))       # site navigation pushes the dateline down
DATELINE = "SOUTH SAN FRANCISCO, Calif., August 5, 2026 (GLOBE NEWSWIRE) -- Acme Therapeutics announced today"
LATE = _page(f"{NAV} {DATELINE} that the study met its goal.")
SOURCE = {"id": "s", "kind": "filing", "published_date": "2026-08-06", "pages": [{"page_number": 1}]}


def test_a_late_dateline_is_not_found_by_the_header_scan_and_the_filing_date_is_not_substituted():
    assert ec.dateline_date(LATE["words"]) is None
    assert ec.dateline_date(LATE["words"], whole_page=True) == date(2026, 8, 5)
    assert ec.release_day(LATE["words"], "2026-08-06") is None          # not (2026-08-06, "filing date")
    assert ec.release_day(LATE["words"], "2026-08-06", anchor="the study met its goal") is None
    assert ec.release_day(LATE["words"], "2026-08-06", anchor=DATELINE) == (date(2026, 8, 5), "captured dateline")
    # unchanged: a header dateline, and a page with no dateline at all
    head = _page(f"{DATELINE} that the study met its goal.")
    assert ec.release_day(head["words"], "2026-08-06") == (date(2026, 8, 5), "release dateline")
    assert ec.release_day(_page("Acme announced today.")["words"], "2026-08-06") == (date(2026, 8, 6), "filing date")


def _today_row(quote):
    ev = {"ticker": "ACME", "company": "Acme", "event_type": "trial_results", "title": "Study",
          "date_text": "announced today", "date": None, "date_end": None, "quote": quote, "program": "AC-1",
          "indication": None, "box": None, "word_ids": None}
    return build_entry.build_row(1, ev, {"prompt_text": "p", "usage": {}, "fake": False}, SOURCE, LATE,
                                 "2026-10-29T00:00:00+00:00")


def test_a_late_dateline_dates_today_only_when_the_quote_prints_it():
    anchored = _today_row(f"{DATELINE} that the study met its goal.")
    assert anchored["date"] == "2026-08-05"
    assert any(t["step"] == "Date taken from the captured dateline" for t in anchored["trail"])
    assert _date_check(anchored)["result"] == "pass"
    bare = _today_row("Acme Therapeutics announced today that the study met its goal.")
    assert bare["date"] is None                           # neither 2026-08-05 by a longer scan nor the filing date
    assert _date_check(bare)["result"] == "could_not_run"
    assert _date_check(bare)["compared"]["timing"]["start"] is None
