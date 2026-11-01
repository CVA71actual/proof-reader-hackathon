"""Job 52k: a plural filing noun (submissions, resubmissions, filings) goes through the same phrase-end rule as the
singular. "are" joins the closed continuation list as the missing be-verb form; nothing else is added."""
import dataclasses

import pytest
from checks import event_checks as ec
from checks.common import PASS

FILING = ["filings expected in Q3", "BLA submission in 2027", "submissions are expected in Q1 2027",
          "the filing is expected in Q3"]
MENTION = [("regulatory submissions planned for 2027", "Submissions"),
           ("The FDA submissions budget is set.", "Submissions"),
           ("filings strategy update", "Filings")]


@pytest.mark.parametrize("text", FILING)
def test_positive_controls_stay_filing(text):
    assert ec.event_type_for(text) == "filing"
    assert ec.type_match("filing", text).result == PASS


@pytest.mark.parametrize("text,word", MENTION)
def test_plural_mentions_are_unresolved(text, word):
    assert ec.event_type_for(text) == "unknown"
    assert ec.event_type_for(text, loose=True) == "unknown"
    r = ec.type_match("filing", text)
    assert r.result == "unresolved"
    assert r.reason == f"{word} are mentioned, but a filing event is not established."


def test_list_gained_only_are():
    assert "are" in ec._AGENCY_END
    assert not {"planned", "scheduled", "due"} & ec._AGENCY_END


def test_unresolved_type_check_is_not_a_caught_mistake():
    """CVA ruling: a mention row is a conservative limitation of the checker, not an error in the proposal. It is
    counted as unresolved (Judgment needed), is never escalated as a failure, and adds nothing to mistake counts;
    the quote and its timing phrase stay on the row."""
    from runs import build_entry, escalate
    from runs.state import current_state
    from tests.test_r49_status_owner import _check, _row, _run
    quote = "Regulatory submissions planned for 2027."
    tm = ec.type_match("filing", quote)
    assert tm.result == "unresolved"
    date = ec.CheckResult("date_match", "Date", PASS, "ok", {})
    status = ec.row_status([date, tm])
    assert status not in (ec.MATCHED, ec.FAILED)
    group = build_entry.START_GROUP.get(status, "unverified")
    tm_dict = {**_check("type_match", "unresolved"), "reason": tm.reason}
    row = {**_row(status, group, [_check("quote_match"), _check("date_match"), tm_dict]),
           "event_type": "filing", "quote": quote, "date_text": "2027", "date": "2027-01-01",
           "date_end": "2027-12-31"}
    run = _run(row)
    assert escalate.candidates(run["rows"]) == []             # not sent anywhere as a failure
    out = current_state(run, [])
    assert out["mistakes"] == {k: 0 for k in out["mistakes"]}  # no mistake caught by anyone
    assert out["mistake_list"] == []
    assert out["unresolved"] == 1                              # Judgment needed
    assert out["counts"]["rows"]["open"] + out["counts"]["rows"]["unverified"] == 1
    assert run["rows"][0]["quote"] == quote and run["rows"][0]["date_text"] == "2027"
