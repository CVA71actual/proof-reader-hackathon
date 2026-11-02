"""One word-boundary rule for every fixed-word test: the edge of a word is anything that is not a letter, digit,
underscore or hyphen (Job 52b: a slash is an edge)."""
import pytest

from checks.event_checks import date_match, event_type_for, type_match
from tests.pivot_assertions import assert_proposal_held

NOT_THE_WORD = ["data-room", "data2", "data_code", "_data", "2data"]
THE_WORD = ["data.", "(data)", "data,", "the data", "data/set"]


@pytest.mark.parametrize("w", NOT_THE_WORD)
def test_glued_forms_are_not_coverage_keywords_and_literal_dates_do_not_accept_them(w):
    quote = f"The company expects {w} in Q4 2027."
    assert event_type_for(quote) == "unknown"
    assert type_match("trial_results", quote).result == "not_needed"
    # DATE-LITERAL-001: printed date words do not need an event keyword.
    assert date_match("Q4 2027", "2027-10-01", quote, "2027-12-31", event_type="trial_results").result == "pass"
    assert_proposal_held(quote, "trial_results", date_text="Q4 2027", start="2027-10-01", end="2027-12-31")


@pytest.mark.parametrize("w", THE_WORD)
def test_punctuation_changes_coverage_keywords_but_never_accepts_meaning(w):
    quote = f"The company expects {w} in Q4 2027."
    assert event_type_for(quote) == "trial_results"
    assert type_match("trial_results", quote).result == "not_needed"
    assert_proposal_held(quote, "trial_results", date_text="Q4 2027", start="2027-10-01", end="2027-12-31")
    quote = f"Topline {w} are due in Q4 2027."
    assert date_match("Q4 2027", "2027-10-01", quote, "2027-12-31", event_type="trial_results").result == "pass"

