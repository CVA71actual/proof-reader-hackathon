"""Codex P2 (main 9ce1ea0): slide-style two-digit years with an apostrophe ('Q1 ’27', 'Aug ’26', '1H ’27') are 20YY."""

from datetime import date

import pytest

from checks.event_checks import date_match, expand_short_year, parse_period


@pytest.mark.parametrize("text,period", [
    ("Q1 ’27", (date(2027, 1, 1), date(2027, 3, 31))),
    ("Q1 '27", (date(2027, 1, 1), date(2027, 3, 31))),
    ("Q1’27", (date(2027, 1, 1), date(2027, 3, 31))),
    ("Aug ’26", (date(2026, 8, 1), date(2026, 8, 31))),
    ("1H ’27", (date(2027, 1, 1), date(2027, 6, 30))),
    ("2H ’27", (date(2027, 7, 1), date(2027, 12, 31))),
    ("FY ’27", (date(2027, 1, 1), date(2027, 12, 31))),
])
def test_a_two_digit_year_with_an_apostrophe_is_20yy(text, period):
    assert parse_period(text) == period


def test_without_the_apostrophe_a_number_is_not_a_year():
    assert expand_short_year("Aug 26") == "Aug 26" and parse_period("Aug 26") is None
    assert expand_short_year("Q1 ’27 and Q3 ’28") == "Q1 2027 and Q3 2028"


def test_date_match_reads_the_same_period_in_the_quote_and_the_date_text():
    q = "Topline results are expected in Q1 ’27."
    assert date_match("Q1 ’27", "2027-01-01", q, "2027-03-31", event_type="trial_results").result == "pass"
    assert date_match("Q1 ’27", "2027-04-01", q, "2027-06-30", event_type="trial_results").result == "fail"
    assert date_match("1H ’27", "2027-01-01", "Phase 3 initiation in 1H ’27.", "2027-06-30",
                      event_type="milestone").result == "pass"
    assert date_match("Aug ’26", "2026-08-01", "NDA submission planned in Aug ’26.", "2026-08-31",
                      event_type="filing").result == "pass"


def test_the_date_text_must_still_be_in_the_quote():
    r = date_match("Q1 ’27", "2027-01-01", "Topline results are expected in Q2 ’27.", "2027-03-31")
    assert r.result == "fail" and "not part of the quote" in r.reason
