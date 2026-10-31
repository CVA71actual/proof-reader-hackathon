"""Two defects the live twelve-company run (runs/twelve-1) exposed, one test each."""

from checks.event_checks import date_match
from runs import compare

AT = "2026-10-29T00:00:00+00:00"
USAGE = {"prompt_text": "Read the page.", "usage": {"prompt_tokens": 10, "completion_tokens": 4}}


def test_a_typographic_apostrophe_in_the_date_text_is_found_in_the_quote():
    """The date text 'Q1 ’27' is printed in the quote. It was reported as not part of the quote."""
    result = date_match("Q1 ’27", "2027-01-01", "Launch in Q1 ’27 is planned.", "2027-03-31")
    assert "not part of the quote" not in result.reason
    assert date_match("Aug ’26", "2026-08-01", "Filing in Aug ’26.").reason != "The date text is not part of the quote."


def _row(group):
    return {"quote": "Drug A starts a Phase 3 in Q3 2027.", "program": "Drug A", "indication": None,
            "event_type": "unknown", "date": "2027-07-01", "date_end": "2027-09-30", "status": "matched",
            "group": group, "checks": [{"result": "pass"}], "reason": "matched", "trail": []}


def test_a_disagreement_does_not_reopen_a_row_the_time_window_removed():
    row = _row("removed")
    event = {**_row("open"), "indication": "Disease X", "trail": None, "checks": None, "reason": None}
    rows, _new = compare.apply_events([row], [event], USAGE, {}, {}, AT, 2)
    assert rows[0]["disagreement"]["field"] == "indication"       # the disagreement is still on record
    assert rows[0]["group"] == "removed" and rows[0]["status"] == "matched"


def test_a_disagreement_still_opens_a_row_that_was_not_removed():
    row = _row("kept")
    event = {**_row("open"), "indication": "Disease X", "trail": None, "checks": None, "reason": None}
    rows, _new = compare.apply_events([row], [event], USAGE, {}, {}, AT, 2)
    assert rows[0]["group"] == "open" and rows[0]["status"] == "undecided"
