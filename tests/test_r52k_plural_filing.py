"""Job 52k: a plural filing noun (submissions, resubmissions, filings) goes through the same phrase-end rule as the
singular. "are" joins the closed continuation list as the missing be-verb form; nothing else is added.
PIVOT-001: retain these lexical diagnostics and original inputs, but the retired
type check cannot accept meaning. Every proposal must remain held for review."""
import pytest
from checks import event_checks as ec
from checks.common import NOT_NEEDED
from tests.pivot_assertions import assert_proposal_held

FILING = ["filings expected in Q3", "BLA submission in 2027", "submissions are expected in Q1 2027",
          "the filing is expected in Q3"]
MENTION = [("regulatory submissions planned for 2027", "Submissions"),
           ("The FDA submissions budget is set.", "Submissions"),
           ("filings strategy update", "Filings")]


@pytest.mark.parametrize("text", FILING)
def test_positive_controls_stay_filing(text):
    assert ec.event_type_for(text) == "filing"
    assert ec.type_match("filing", text).result == NOT_NEEDED
    assert_proposal_held(text, "filing")


@pytest.mark.parametrize("text,word", MENTION)
def test_plural_mentions_are_unresolved(text, word):
    assert ec.event_type_for(text) == "unknown"
    assert ec.event_type_for(text, loose=True) == "unknown"
    r = ec.type_match("filing", text)
    assert r.result == NOT_NEEDED
    assert ec.reason_words(ec.EVENT_MENTIONED_NOT_ESTABLISHED, text) == f"{word} are mentioned, but a filing event is not established."
    assert_proposal_held(text, "filing")


def test_list_gained_only_are():
    assert "are" in ec._AGENCY_END
    assert not {"planned", "scheduled", "due"} & ec._AGENCY_END


def test_retired_type_check_keeps_plural_proposal_held_without_a_mistake():
    """PIVOT replaces semantic keyword verdicts with a separate human-review hold.
    Preserve the original quote/timing and no-escalation/no-mistake assertions.
    """
    from runs import escalate
    quote = "Regulatory submissions planned for 2027."
    assert ec.type_match("filing", quote).result == NOT_NEEDED
    run, out = assert_proposal_held(quote, "filing", date_text="2027",
                                  start="2027-01-01", end="2027-12-31")
    assert escalate.candidates(run["rows"]) == []
    assert out["mistakes"] == {k: 0 for k in out["mistakes"]}
    assert out["mistake_list"] == []
    assert out["unresolved"] == int(bool(out["targets"]["e1"]["issues"]))     # CALENDAR-RULE-001
    assert out["counts"]["review"]["proposed"] == 1  # review count is separate from mechanical groups
    assert run["rows"][0]["quote"] == quote and run["rows"][0]["date_text"] == "2027"
