"""Job 14: "today" read from the release dateline (or the filing date), one event-type word table shared by the reader
prompts and type_match, and more date forms (calendar year, late/early/mid year, 1Q27)."""

from datetime import date

import pytest

from capture import read_events
from checks import event_checks
from checks.event_checks import (EVENT_WORDS, date_match, dateline_date, event_type_for, expand_short_year,
                                 is_today_word, parse_period, release_day, type_match, vague_period_window)
from runs import build_entry

SOURCE = {"id": "s", "kind": "filing", "published_date": "2026-08-06", "pages": [{"page_number": 1}]}
READING = {"prompt_text": "p", "usage": {}, "fake": False}


def page(text: str) -> dict:
    """A fake page: one word per whitespace-separated token, laid out on one line each."""
    words = [{"id": f"w{i}", "text": t, "x0": 10.0, "y0": 10.0 + 12 * i, "x1": 60.0, "y1": 20.0 + 12 * i}
             for i, t in enumerate(text.split())]
    return {"words": words, "page_width": 612.0, "page_height": 792.0}


WITH_DATELINE = page("Acme Announces Results SOUTH SAN FRANCISCO, Calif., August 5, 2026 (GLOBE NEWSWIRE) -- Acme "
                     "Therapeutics announced today that the study met its goal.")
NO_DATELINE = page("Acme Therapeutics announced today that the study met its goal.")
QUOTE = "Acme Therapeutics announced today that the study met its goal."


def _row(words_doc, date_text="announced today", date=None):
    ev = {"ticker": "ACME", "company": "Acme", "event_type": "trial_results", "title": "Study", "date_text": date_text,
          "date": date, "date_end": None, "quote": QUOTE, "program": "AC-1", "indication": None, "box": None,
          "word_ids": [w["id"] for w in words_doc["words"][-10:]]}
    return build_entry.build_row(1, ev, READING, SOURCE, words_doc, "2026-10-29T00:00:00+00:00")


# ---------------------------------------------------------------- the dateline

def test_the_dateline_is_found_by_code():
    assert dateline_date(WITH_DATELINE["words"]) == date(2026, 8, 5)
    assert dateline_date(page("NEW YORK, Aug. 5, 2026 -- Acme")["words"]) == date(2026, 8, 5)
    assert dateline_date(page("SAN DIEGO, September 1, 2026")["words"]) == date(2026, 9, 1)
    assert dateline_date(NO_DATELINE["words"]) is None


def test_a_weekday_date_or_a_late_date_is_not_a_dateline():
    assert dateline_date(page("The call is Thursday, August 6, 2026 at 8 a.m.")["words"]) is None
    late = page(" ".join(["word"] * 200) + " BOSTON, August 5, 2026")
    assert dateline_date(late["words"]) is None, "only the top of the page is searched"


def test_release_day_prefers_the_dateline_then_the_filing_date():
    assert release_day(WITH_DATELINE["words"], "2026-08-06") == (date(2026, 8, 5), "release dateline")
    assert release_day(NO_DATELINE["words"], "2026-08-06") == (date(2026, 8, 6), "filing date")
    assert release_day(NO_DATELINE["words"], None) is None


@pytest.mark.parametrize("text", ["today", "Today", "announced today", "this morning", "earlier today"])
def test_the_today_words(text):
    assert is_today_word(text)


def test_other_words_are_not_today_words():
    assert not is_today_word("Q3 2027") and not is_today_word("tomorrow") and not is_today_word("today, Sept. 4")


# ---------------------------------------------------------------- the row and its trail

def test_today_is_the_dateline_date_when_one_is_printed():
    row = _row(WITH_DATELINE)
    assert row["date"] == "2026-08-05", "the dateline, not the filing date 2026-08-06"
    step = next(t for t in row["trail"] if t["step"].startswith("Date taken from"))
    assert step["step"] == "Date taken from the release dateline" and step["tag"] == "Code"
    assert next(c for c in row["checks"] if c["name"] == "date_match")["result"] == "pass"


def test_today_is_the_filing_date_when_no_dateline_is_printed():
    row = _row(NO_DATELINE, date_text="today")
    assert row["date"] == "2026-08-06"
    step = next(t for t in row["trail"] if t["step"].startswith("Date taken from"))
    assert step["step"] == "Date taken from the filing date" and step["tag"] == "Code"


def test_a_reader_date_that_is_not_the_release_day_fails():
    row = _row(WITH_DATELINE, date="2026-08-06")        # the reader gave the filing date; the page says August 5
    assert row["date"] == "2026-08-06" and not any(t["step"].startswith("Date taken from") for t in row["trail"])
    check = next(c for c in row["checks"] if c["name"] == "date_match")
    assert check["result"] == "fail" and "2026-08-05" in check["reason"]


def test_date_match_today_cases():
    q = "Roivant to host a call today at 8:00 a.m. ET."
    assert date_match("today", "2026-08-05", q, today="2026-08-05").result == "pass"
    assert date_match("today", "2026-08-06", q, today=date(2026, 8, 5)).result == "fail"
    assert date_match("today", None, q, today="2026-08-05").result == "fail"
    assert date_match("today", "2026-08-05", q, "2026-08-31", today="2026-08-05").result == "fail"
    assert date_match("today", "2026-08-05", q).result == "could_not_run"
    assert date_match("this morning", "2026-08-05", q, today="2026-08-05").result == "fail", "not in the quote"


# ---------------------------------------------------------------- one event-type word table

WORD_TYPE = [
    ("submit", "filing"), ("submission", "filing"), ("NDA", "filing"), ("BLA", "filing"), ("MAA", "filing"),
    ("IND", "filing"), ("filed", "filing"),
    ("approval", "regulatory_decision"), ("decision", "regulatory_decision"), ("PDUFA", "regulatory_decision"),
    ("CHMP opinion", "regulatory_decision"),
    ("topline", "trial_results"), ("data", "trial_results"), ("readout", "trial_results"),
    ("results", "trial_results"), ("presentation of data", "trial_results"),
    ("launch", "launch"), ("commercial availability", "launch"),
    ("conference", "conference"), ("symposium", "conference"), ("investor day", "conference"),
    ("webcast", "conference"),
]


@pytest.mark.parametrize("word,kind", WORD_TYPE)
def test_each_word_gives_its_type_and_a_wrong_type_still_fails(word, kind):
    quote = f"The company expects the {word} in 2027."
    assert event_type_for(quote) == kind
    assert word.lower() in EVENT_WORDS[kind]
    assert type_match(kind, quote).result == "pass"
    wrong = next(t for t in ("trial_results", "regulatory_decision", "launch", "conference", "filing") if t != kind)
    assert type_match(wrong, quote).result == "fail"


def test_a_sentence_with_none_of_the_words_is_a_milestone():
    assert event_type_for("The company expects growth in 2027.") == "milestone"
    assert event_type_for(None) == "milestone"


def test_the_reader_prompts_print_the_same_table():
    entry = {"company": "Acme", "ticker": "ACME", "form": "8-K", "exhibit": "99.1", "filing_date": "2026-08-05"}
    guide = event_checks.type_guide()
    assert read_events.EVENT_TYPES is event_checks.EVENT_TYPES
    for prompt in (read_events.build_image_prompt(entry), read_events.build_prompt(page("a b"), entry)):
        assert guide in prompt
    for kind, words in EVENT_WORDS.items():
        assert kind in guide
    for w in ("submit", "NDA", "BLA", "MAA", "IND", "PDUFA", "CHMP opinion", "commercial availability", "investor day",
              "webcast", "symposium", "presentation of data", "topline", "readout"):
        assert w in guide
    assert "milestone: anything else" in guide


def test_the_prompt_lists_only_the_types_asked_for():
    guide = event_checks.type_guide(("filing", "launch"))
    assert "filing:" in guide and "launch:" in guide and "conference" not in guide and "milestone" not in guide


# ---------------------------------------------------------------- more date forms

@pytest.mark.parametrize("text,period", [
    ("calendar year 2028", (date(2028, 1, 1), date(2028, 12, 31))),
    ("2028", (date(2028, 1, 1), date(2028, 12, 31))),
    ("the second half of calendar year 2026", (date(2026, 7, 1), date(2026, 12, 31))),
    ("1Q27", (date(2027, 1, 1), date(2027, 3, 31))),
    ("4Q26", (date(2026, 10, 1), date(2026, 12, 31))),
    ("late 2026", (None, None)), ("early 2027", (None, None)), ("mid-2027", (None, None)),
])
def test_the_period_forms_parse(text, period):
    assert parse_period(text) == period


@pytest.mark.parametrize("text,first,last", [
    ("late 2026", date(2026, 9, 1), date(2026, 12, 31)),
    ("early 2027", date(2027, 1, 1), date(2027, 4, 30)),
    ("mid-2027", date(2027, 5, 1), date(2027, 8, 31)),
])
def test_early_mid_late_year_windows(text, first, last):
    assert vague_period_window(text)[:2] == (first, last)
    q = f"Topline data are expected in {text}."
    kw = dict(event_type="trial_results")
    assert date_match(text, first.isoformat(), q, last.isoformat(), **kw).result == "pass"
    assert date_match(text, None, q, **kw).result == "pass"
    assert date_match(text, "2026-01-01", q, "2026-02-01", **kw).result == "fail"


def test_digit_q_digit_digit_is_a_quarter_only_for_years_20_to_39():
    assert expand_short_year("1Q27") == "Q1 2027" and expand_short_year("to start in 4Q26.") == "to start in Q4 2026."
    assert expand_short_year("2Q45") == "2Q45" and expand_short_year("1Q19") == "1Q19"
    assert expand_short_year("PN1Q27X") == "PN1Q27X"
    assert parse_period("2Q45") is None


def test_date_match_reads_the_new_forms():
    kw = dict(event_type="milestone", title="Extension studies")     # the title's longest word marks the clause
    q = "VANQUISH Extension Studies to Commence in 1Q27."
    assert date_match("1Q27", "2027-01-01", q, "2027-03-31", **kw).result == "pass"
    assert date_match("1Q27", "2027-04-01", q, "2027-06-30", **kw).result == "fail"
    q = "Topline data from the Phase 3 trial are expected in 4Q26."
    assert date_match("4Q26", "2026-10-01", q, "2026-12-31", event_type="trial_results").result == "pass"
    q = "Commercial launch is planned for calendar year 2028."
    assert date_match("calendar year 2028", "2028-01-01", q, "2028-12-31", event_type="launch").result == "pass"
    assert date_match("calendar year 2028", "2028-01-01", q, "2028-06-30", event_type="launch").result == "fail"
    q = "The NDA will be filed in the second half of calendar year 2026."
    assert date_match("the second half of calendar year 2026", "2026-07-01", q, "2026-12-31",
                      event_type="filing").result == "pass"
    assert date_match("the second half of calendar year 2026", "2026-01-01", q, "2026-06-30",
                      event_type="filing").result == "fail"


@pytest.mark.parametrize("text", ["data2", "data_code", "_data", "2data"])
def test_has_word_rejects_digit_and_underscore_adjacency(text):
    assert not event_checks._has_word(text, "data")


@pytest.mark.parametrize("text", ["data.", "(data)", "the data, shown"])
def test_has_word_accepts_punctuation_adjacency(text):
    assert event_checks._has_word(text, "data")
