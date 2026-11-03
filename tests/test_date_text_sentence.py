"""A date text written as a clause: the one date or period it ends on is read and checked as a short date text."""
import pytest

from checks.common import COULD_NOT_RUN, FAIL, NOT_NEEDED, PASS
from checks.event_checks import date_in_clause, date_match

FILED = "2026-08-05"

# Run 9 (rows e1-e4): (date text, quote, words read)
RUN9 = [
    ("with initiation expected in early first quarter 2027",
     "We believe data from our Phase 1 study supports our decision to advance PN-881 into a comprehensive Phase 2b "
     "psoriasis program, with initiation expected in early first quarter 2027.",
     "early first quarter 2027"),
    ("the initiation of a Phase 1 study for PN-477o is anticipated in the first half of 2027",
     "A Phase 1 clinical study for PN-477sc has been initiated, and the initiation of a Phase 1 study for PN-477o is "
     "anticipated in the first half of 2027.",
     "first half of 2027"),
    ("with a Prescription Drug User Fee Act target action date in August 2026",
     "The NDA for rusfertide is currently under priority review by the FDA, with a Prescription Drug User Fee Act "
     "target action date in August 2026.",
     "August 2026"),
    ("with the initiation of a Phase 1 study anticipated in the first quarter of 2027",
     "IND-enabling studies for PN-8047 are ongoing, with the initiation of a Phase 1 study anticipated in the first "
     "quarter of 2027.",
     "first quarter of 2027"),
]


@pytest.mark.parametrize("date_text,quote,words", RUN9)
def test_run9_clauses_read_their_period(date_text, quote, words):
    assert date_in_clause(date_text, FILED) == words
    got = date_match(date_text, None, quote, None, filed=FILED)
    short = date_match(words, None, quote, None, filed=FILED)
    assert got.result == short.result == PASS
    assert got.reason.startswith(f"Read the period '{words}' from the date text.")
    assert got.reason.endswith(short.reason)
    assert got.compared["timing"]["wording"] == date_text
    assert got.compared["timing"]["start"] == short.compared["timing"]["start"]
    assert got.compared["timing"]["end"] == short.compared["timing"]["end"]


@pytest.mark.parametrize("date_text,quote,words", RUN9)
@pytest.mark.parametrize("date_iso,date_end", [("2027-01-01", "2027-03-31"), ("2026-08-01", "2026-08-31"),
                                               ("2027-01-01", "2027-06-30"), ("2027-05-01", None)])
def test_clause_result_is_the_short_form_result(date_text, quote, words, date_iso, date_end):
    got = date_match(date_text, date_iso, quote, date_end, filed=FILED)
    short = date_match(words, date_iso, quote, date_end, filed=FILED)
    assert got.result == short.result
    assert got.compared["found"] == short.compared["found"]


def test_exact_day_in_clause_is_read_as_a_date():
    quote = "The FDA set a PDUFA target action date on March 12, 2027."
    got = date_match("with a target action date on March 12, 2027", "2027-03-12", quote, None, filed=FILED)
    assert got.result == PASS
    assert got.reason.startswith("Read the date 'March 12, 2027' from the date text.")


def test_two_different_periods_could_not_run():
    text = "with data expected in Q1 2027 and a filing in the second half of 2027"
    quote = "The program is on track, with data expected in Q1 2027 and a filing in the second half of 2027."
    assert date_in_clause(text, FILED) is None
    got = date_match(text, None, quote, None, filed=FILED)
    assert got.result == COULD_NOT_RUN
    assert not got.reason.startswith("Read the")


def test_same_period_twice_is_one_period():
    text = "Q1 2027 data, with initiation expected in Q1 2027"
    assert date_in_clause(text, FILED) == "Q1 2027"


def test_period_not_in_quote_does_not_pass():
    text = "with initiation expected in the first quarter of 2027"
    quote = "IND-enabling studies are ongoing, with initiation of a Phase 1 study expected in the second half of 2027."
    got = date_match(text, None, quote, None, filed=FILED)
    short = date_match("first quarter of 2027", None, quote, None, filed=FILED)
    assert got.result == short.result == FAIL


def test_vague_clause_unchanged():
    quote = ("The open-label extension of the VERIFY trial is ongoing and Takeda will share further findings at "
             "upcoming medical conferences.")
    assert date_in_clause("at upcoming medical conferences") is None
    got = date_match("at upcoming medical conferences", None, quote, None)
    assert got.result == NOT_NEEDED
    assert got.reason == "The quote names no exact day and no period, so there is nothing to compare."


@pytest.mark.parametrize("text", [
    # the date does not end the text: words after it can change what it means (run 9, e5)
    'In September 2025, JNJ submitted an application to the European Medicines Agency ("EMA") seeking approval.',
    "with launch expected in 2027 and beyond",
    "with initiation expected in Q1 2027 at the earliest",
    # a bounding word before it: not 'in' the period
    "with initiation expected by Q1 2027",
    "with initiation expected no later than Q1 2027",
    "with initiation expected after August 2026",
    "with initiation expected before the first half of 2027",
    # no date at all
    "with initiation expected soon",
])
def test_clauses_left_unread(text):
    assert date_in_clause(text, FILED) is None


def test_run9_e5_unchanged():
    text = ('In September 2025, JNJ submitted an application to the European Medicines Agency ("EMA") seeking the first '
            'approval of ICOTYDE for the treatment of adults and pediatric patients 12 years of age or older with '
            'moderate-to-severe plaque psoriasis.')
    quote = text.replace('("EMA")', "(“EMA”)")
    got = date_match(text, None, quote, None, filed=FILED)
    assert got.result == COULD_NOT_RUN
    assert not got.reason.startswith("Read the")


@pytest.mark.parametrize("short,quote,date_iso,date_end,expected", [
    ("early first quarter 2027", "Initiation is expected in early first quarter 2027.", None, None, PASS),
    ("first half of 2027", "Initiation is anticipated in the first half of 2027.", "2027-01-01", "2027-06-30", PASS),
    ("August 2026", "The PDUFA date is in August 2026.", "2026-09-01", "2026-09-30", FAIL),
    ("Q1 2027", "Initiation is expected in Q2 2027.", None, None, FAIL),
    ("in Q1 2027", "Initiation is expected in Q1 2027.", None, None, PASS),
])
def test_short_forms_unchanged(short, quote, date_iso, date_end, expected):
    assert date_in_clause(short, FILED) is None
    got = date_match(short, date_iso, quote, date_end, filed=FILED)
    assert got.result == expected
    assert not got.reason.startswith("Read the")
    assert got.compared["timing"]["wording"] == short
