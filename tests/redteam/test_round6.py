"""Red Team round 6. A failure is a finding: each assertion is the result the code should have given.

Base: product main 590590b. No model call, no network, no key.
Scope: Job 14. "today" is the release dateline (the filing date only when no dateline is printed).
A quote with words of more than one event type takes the first listed type, and a wrong proposal fails.
"""

from datetime import date

from checks.event_checks import date_match, dateline_date, event_type_for, release_day, type_match
from runs import build_entry

READING = {"prompt_text": "p", "usage": {}, "fake": False}
AT = "2026-10-29T00:00:00+00:00"


def page(text: str) -> dict:
    """One word per whitespace-separated token, one line each."""
    words = [{"id": f"w{i}", "text": t, "x0": 10.0, "y0": 10.0 + 12 * i, "x1": 80.0, "y1": 20.0 + 12 * i}
             for i, t in enumerate(text.split())]
    return {"words": words, "page_width": 612.0, "page_height": 792.0}


def source(published: str) -> dict:
    return {"id": "s", "kind": "filing", "published_date": published, "pages": [{"page_number": 1}]}


def build(text: str, quote: str, date_text: str, published: str, date_value=None) -> dict:
    words_doc = page(text)
    event = {"ticker": "ACME", "company": "Acme", "event_type": "milestone", "title": "Update",
             "date_text": date_text, "date": date_value, "date_end": None, "quote": quote,
             "program": None, "indication": None, "box": None,
             "word_ids": [w["id"] for w in words_doc["words"]]}
    return build_entry.build_row(1, event, READING, source(published), words_doc, AT)


def _date_check(row: dict) -> dict:
    return next(c for c in row["checks"] if c["name"] == "date_match")


# ---------------------------------------------------------------- today


def test_a_clock_after_today_is_still_the_release_day():
    """'today at 8:00 a.m. ET' is the release day. The clock is not a different date."""
    quote = "Roivant to host a call today at 8:00 a.m. ET."
    phrases = [
        ("today at 8:00 a.m. ET", quote),
        ("announced today at 8:00 a.m. ET", "Acme announced today at 8:00 a.m. ET that the study met its goal."),
        ("this morning at 8:00 a.m. ET", "Acme said this morning at 8:00 a.m. ET that the study met its goal."),
    ]
    for phrase, printed in phrases:
        got = date_match(phrase, "2026-08-05", printed, today="2026-08-05")
        assert got.result == "pass", (phrase, got.result, got.reason)
    text = "SOUTH SAN FRANCISCO, Calif., August 5, 2026 -- " + quote
    row = build(text, quote, "today at 8:00 a.m. ET", "2026-08-06")
    assert row["date"] == "2026-08-05", row["date"]
    step = next(t for t in row["trail"] if t["step"].startswith("Date taken from"))
    assert step["step"] == "Date taken from the release dateline"
    assert _date_check(row)["result"] == "pass"


def test_a_dash_between_the_city_and_the_month_is_still_the_dateline():
    """The printed release day comes first. A dash where the comma would be is still the dateline."""
    viking = "SAN DIEGO, CA \u2013 September 22, 2026 \u2013 Viking announced today that the maintenance study met its goal."
    taysha = "Dallas \u2013 August 11, 2026 \u2013 Taysha announced today that dosing is complete."
    found = {
        "viking": dateline_date(page(viking)["words"]),
        "taysha": dateline_date(page(taysha)["words"]),
    }
    assert found == {"viking": date(2026, 9, 22), "taysha": date(2026, 8, 11)}, found
    quote = "Viking announced today that the maintenance study met its goal."
    row = build(viking, quote, "today", "2026-09-23")
    assert release_day(page(viking)["words"], "2026-09-23") == (date(2026, 9, 22), "release dateline")
    assert row["date"] == "2026-09-22", row["date"]
    step = next(t for t in row["trail"] if t["step"].startswith("Date taken from"))
    assert step["step"] == "Date taken from the release dateline"


def test_a_business_wire_credit_does_not_hide_the_dateline():
    """'CITY, State -- (BUSINESS WIRE) -- Month D, YYYY' is the release day, then the filing date."""
    text = ("SHANGHAI & CAMBRIDGE, Mass. -- (BUSINESS WIRE) -- Oct. 22, 2026 -- "
            "Zai Lab today announced a private placement.")
    quote = "Zai Lab today announced a private placement."
    words = page(text)["words"]
    assert dateline_date(words) == date(2026, 10, 22)
    assert release_day(words, "2026-10-23") == (date(2026, 10, 22), "release dateline")
    row = build(text, quote, "today", "2026-10-23")
    assert row["date"] == "2026-10-22", row["date"]
    step = next(t for t in row["trail"] if t["step"].startswith("Date taken from"))
    assert step["step"] == "Date taken from the release dateline"


# ---------------------------------------------------------------- event type


def test_a_decision_on_a_filing_is_a_decision():
    """Words of both a decision and a filing: the first listed type wins, and the filing label fails."""
    quote = "The FDA approval follows the NDA submission."
    assert event_type_for(quote) == "regulatory_decision"
    assert type_match("regulatory_decision", quote).result == "pass"
    got = type_match("filing", quote)
    assert got.result == "fail", got.reason


def test_presentation_of_data_is_trial_results():
    """'presentation of data' is trial results. A conference label on that quote fails."""
    quote = "The company expects the presentation of data in 2027."
    assert event_type_for(quote) == "trial_results"
    assert type_match("trial_results", quote).result == "pass"
    got = type_match("conference", quote)
    assert got.result == "fail", got.reason
