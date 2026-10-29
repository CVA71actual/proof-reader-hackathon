"""One word-boundary rule for every fixed-word test: the edge of a word is anything that is not a letter, digit,
underscore, hyphen or slash."""
import pytest

from checks.event_checks import date_match, event_type_for, type_match

NOT_THE_WORD = ["data-room", "data/set", "data2", "data_code", "_data", "2data"]
THE_WORD = ["data.", "(data)", "data,", "the data"]


@pytest.mark.parametrize("w", NOT_THE_WORD)
def test_glued_forms_are_not_the_word_data(w):
    quote = f"The company expects {w} in Q4 2027."
    assert event_type_for(quote) == "milestone"
    assert type_match("trial_results", quote).result == "not_needed"
    assert date_match("Q4 2027", "2027-10-01", quote, "2027-12-31", event_type="trial_results").result == "could_not_run"


@pytest.mark.parametrize("w", THE_WORD)
def test_punctuation_is_the_edge_of_the_word_data(w):
    quote = f"The company expects {w} in Q4 2027."
    assert event_type_for(quote) == "trial_results"
    assert type_match("trial_results", quote).result == "pass"
    quote = f"Topline {w} are due in Q4 2027."
    if w.endswith((".", ",")):      # the mark ends the clause, so the date is no longer in the data's clause
        return
    assert date_match("Q4 2027", "2027-10-01", quote, "2027-12-31", event_type="trial_results").result == "pass"

