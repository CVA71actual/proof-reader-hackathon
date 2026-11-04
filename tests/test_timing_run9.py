"""Run 9 timing defects (Codex audit 2026-11-03, section 5 of the Run 9 operator order), fixed in checks/.

Rows and quotes are copied from the saved run-9 rows (candidate/runs/run-9, read-only); the pages are synthetic
(make_run: the page words are the quote) where placement needs a whole run.
"""
import pytest

from checks.common import COULD_NOT_RUN, FAIL, NOT_NEEDED, PASS
from checks.event_checks import clean_date_text, date_action_mismatch, date_match, parse_date_phrase
from runs.calendar import run_interval_partition
from runs.state import current_state
from tests.test_pivot_proposal_flow import make_run

FILED = "2026-08-05"          # PTGX 10-Q filing date (src-r1-ptgx)

E1 = ("with initiation expected in early first quarter 2027",
      "We believe data from our Phase 1 study supports our decision to advance PN-881 into a comprehensive Phase 2b "
      "psoriasis program, with initiation expected in early first quarter 2027.")
E2 = ("the initiation of a Phase 1 study for PN-477o is anticipated in the first half of 2027",
      "A Phase 1 clinical study for PN-477sc has been initiated, and the initiation of a Phase 1 study for PN-477o is "
      "anticipated in the first half of 2027.")
E3 = ("with the initiation of a Phase 1 study anticipated in the first quarter of 2027",
      "IND-enabling studies for PN-8047 are ongoing, with the initiation of a Phase 1 study anticipated in the first "
      "quarter of 2027.")
E5_TEXT = ("In September 2025, JNJ submitted an application to the European Medicines Agency (\"EMA\") seeking the first "
           "approval of ICOTYDE for the treatment of adults and pediatric patients 12 years of age or older with "
           "moderate-to-severe plaque psoriasis.")
E5_QUOTE = E5_TEXT.replace('("EMA")', "(“EMA”)")
E5_TITLE = "ICOTYDE EMA approval decision"
SUBMISSION_REASON = "The date in the text is the submission date, not the decision date."


def timing(r):
    return r.compared["timing"]


# ---------------------------------------------------------------- e1 / e2 / e3: printed quarter / half guidance

@pytest.mark.parametrize("text,quote,start,end", [
    (*E1, "2027-01-01", "2027-01-31"),      # early Q1 = the quarter's first month (existing rule, test_cost_and_words)
    (*E2, "2027-01-01", "2027-06-30"),
    (*E3, "2027-01-01", "2027-03-31"),
])
def test_quarter_and_half_guidance_gives_period_bounds_and_keeps_its_wording(text, quote, start, end):
    r = date_match(text, None, quote, None, filed=FILED, title="initiation")
    assert r.result == PASS
    assert timing(r) == {"wording": text, "start": start, "end": end, "precision": "period"}
    assert "timing could not be read" not in r.reason.lower()


@pytest.mark.parametrize("text,quote,start,end", [
    (*E1, "2027-01-01", "2027-01-31"),
    (*E2, "2027-01-01", "2027-06-30"),
    (*E3, "2027-01-01", "2027-03-31"),
])
def test_guidance_rows_place_in_the_window_by_their_bounds_never_as_a_day(text, quote, start, end):
    _, run = make_run(("guidance", quote, "Phase 1 initiation", text, None, []))
    state = current_state(run, [])
    t = state["targets"]["e1"]
    assert (t["values"]["date"], t["values"]["date_end"]) == (start, end)      # a period, never a single day
    assert t["values"]["date_text"] == text                                      # 'early' etc. stays as printed
    assert t["placement"] == "calendar" and t["timing_state"] != "timing_unresolved"
    partition, _, _ = run_interval_partition(run, state)
    assert [e["id"] for e in partition["calendar"]] == ["e1"] and partition["timing_unresolved"] == []


def test_second_half_2027_is_later_by_its_bounds():
    quote = "PN-458o: IND-enabling studies are ongoing, with Phase 1 initiation expected in the second half of 2027."
    _, run = make_run(("later", quote, "Phase 1 initiation", "second half of 2027", None, []))
    state = current_state(run, [])
    partition, _, _ = run_interval_partition(run, state)
    assert [e["id"] for e in partition["later"]] == ["e1"]


def test_a_quarter_row_given_as_one_day_still_fails():
    # A display interval is never turned into a precise asserted day.
    r = date_match(*E3[:1], "2027-01-01", E3[1], None, filed=FILED)
    assert r.result == FAIL


# ---------------------------------------------------------------- e10 versus e28: a field label before the date

TRUIST = "Date: Thursday, November 12, 2026"


@pytest.mark.parametrize("label", ["Date: ", "When: ", "Dates: ", "Date and time: ", "date: ", ""])
def test_a_leading_field_label_does_not_change_the_result(label):
    text = f"{label}Thursday, November 12, 2026"
    quote = f"{label}Thursday, November 12, 2026" if label else TRUIST
    r = date_match(text, "2026-11-12", quote, None, filed="2026-10-20")
    assert r.result == PASS, r.reason
    assert timing(r)["precision"] == "day" and timing(r)["start"] == "2026-11-12"


def test_e10_and_e28_same_day_same_result():
    e10 = date_match(TRUIST, "2026-11-12", TRUIST, None, filed="2026-10-20")
    e28 = date_match("Thursday, November 12, 2026", "2026-11-12", TRUIST, None, filed="2026-10-20")
    assert e10.result == e28.result == PASS
    assert timing(e10)["start"] == timing(e28)["start"] == "2026-11-12"
    assert parse_date_phrase(TRUIST) == parse_date_phrase("Thursday, November 12, 2026")


def test_label_with_a_wrong_day_still_fails():
    assert date_match(TRUIST, "2026-11-13", TRUIST, None).result == FAIL


def test_only_field_labels_are_taken_off():
    assert clean_date_text("Date: Thursday, November 12, 2026") == "November 12, 2026"
    assert clean_date_text("Deadline: November 12, 2026") == "Deadline: November 12, 2026"


# ---------------------------------------------------------------- e25: date == date_end is one day

E25_QUOTE = "protocolSection.statusModule.completionDateStruct.date: 2027-04-01"


def test_same_first_and_last_day_is_one_exact_day():
    r = date_match("2027-04-01", "2027-04-01", E25_QUOTE, "2027-04-01")
    assert r.result == PASS, r.reason
    assert timing(r) == {"wording": "2027-04-01", "start": "2027-04-01", "end": "2027-04-01", "precision": "day"}
    assert r.reason.startswith("The row's first and last day are the same day (2027-04-01)")
    same = date_match("2027-04-01", "2027-04-01", E25_QUOTE, None)
    assert same.result == PASS and timing(same) == timing(r)


def test_one_day_period_row_still_compared_honestly():
    # A real period is not made a day by this rule; a different day still fails; a quarter given as one day fails.
    assert date_match("2027-04-01", "2027-04-01", E25_QUOTE, "2027-04-30").result == FAIL
    assert date_match("2027-04-01", "2027-04-02", E25_QUOTE, "2027-04-02").result == FAIL
    assert date_match("Q1 2027", "2027-01-01", "Data in Q1 2027.", "2027-01-01").result == FAIL


def test_e25_row_places_on_the_calendar():
    _, run = make_run(("e25", "Estimated study completion date: April 1, 2027", "study completion",
                       "April 1, 2027", "2027-04-01", []), event_updates={"date_end": "2027-04-01"})
    assert [c["result"] for c in run["rows"][0]["checks"] if c["name"] == "date_match"] == [PASS]
    t = current_state(run, [])["targets"]["e1"]
    assert t["issues"] == [] and t["placement"] == "calendar" and t["on_calendar"]


# ---------------------------------------------------------------- e5: a submission date is not a decision date

def test_e5_submission_date_is_never_the_decision_date():
    r = date_match(E5_TEXT, None, E5_QUOTE, None, filed=FILED, title=E5_TITLE, event_type="regulatory_decision")
    assert r.result == COULD_NOT_RUN
    assert r.reason.startswith(SUBMISSION_REASON)
    assert timing(r) == {"wording": E5_TEXT, "start": None, "end": None, "precision": "unresolved"}


def test_e5_short_date_text_is_caught_from_the_quote_clause():
    # The reader might give only the month: the quote's clause still dates the submission.
    r = date_match("September 2025", None, E5_QUOTE, None, filed=FILED, title=E5_TITLE)
    assert r.result == COULD_NOT_RUN and r.reason.startswith(SUBMISSION_REASON)
    r = date_match("September 2025", "2025-09-01", E5_QUOTE, "2025-09-30", filed=FILED,
                   event_type="regulatory_decision")         # the model tool path: no title, the event type only
    assert r.result == COULD_NOT_RUN and r.reason.startswith(SUBMISSION_REASON)


def test_e5_row_is_not_placed_by_the_submission_month():
    _, run = make_run(("e5", E5_QUOTE, E5_TITLE, "September 2025", None, []))
    state = current_state(run, [])
    t = state["targets"]["e1"]
    assert (t["values"]["date"], t["values"]["date_end"]) == (None, None)
    assert t["placement"] != "past"


def test_launch_row_names_the_launch():
    q = "The company filed the NDA in March 2026."
    assert date_action_mismatch("March 2026", q, title="US launch of rusfertide") == \
        "The date in the text is the filing date, not the launch date."


@pytest.mark.parametrize("text,quote,title", [
    # the row is about the submission itself
    ("September 2025", E5_QUOTE, "ICOTYDE EMA submission"),
    # the submission word is the object of the decision
    ("first half of 2027", "The EMA decision on the application is expected in the first half of 2027.", E5_TITLE),
    # the decision clause is a separate clause from the submission
    ("Q1 2027", "The BLA was submitted in Q1 2026; approval is expected in Q1 2027.", "BLA approval decision"),
    # a PDUFA date is nearer than the submission
    ("March 12, 2027", "The FDA accepted the NDA submitted in June 2026 and set a PDUFA date of March 12, 2027.",
     "FDA approval decision"),
    # no decision event on the row
    ("September 2025", E5_QUOTE, "ICOTYDE program update"),
])
def test_submission_rule_stays_narrow(text, quote, title):
    assert date_action_mismatch(text, quote, title=title) is None


def test_decision_date_with_submission_mentioned_later_still_passes():
    q = "The FDA decision is expected in the first half of 2027, following the submission."
    r = date_match("first half of 2027", None, q, None, title="FDA approval decision")
    assert r.result == PASS


# ---------------------------------------------------------------- controls

@pytest.mark.parametrize("text,quote,iso,end,want", [
    ("Q1 2027", "Initiation is expected in Q1 2027.", None, None, PASS),
    ("Q1 2027", "Initiation is expected in Q1 2027.", "2027-01-01", "2027-03-31", PASS),
    ("1H 2027", "Initiation is expected in 1H 2027.", "2027-01-01", "2027-06-30", PASS),
    ("November 12, 2026", "Results on November 12, 2026.", "2026-11-12", None, PASS),
    ("November 12, 2026", "Results on November 12, 2026.", "2026-11-13", None, FAIL),
    ("at upcoming medical conferences", "Takeda will share further findings at upcoming medical conferences.",
     None, None, NOT_NEEDED),
])
def test_short_forms_unchanged(text, quote, iso, end, want):
    assert date_match(text, iso, quote, end, filed=FILED).result == want


def test_by_the_end_of_2026_is_not_made_exact():
    q = "We expect to complete the Phase 1 study in mid-2026 and initiate a Phase 2 study of PN-881 in psoriasis by the end of 2026."
    r = date_match("by the end of 2026", None, q, None, filed="2026-02-26")
    assert r.result == PASS
    assert timing(r)["precision"] == "period" and timing(r)["start"] != timing(r)["end"]
    assert date_match("by the end of 2026", "2026-12-31", q, None, filed="2026-02-26").result == FAIL


def test_two_period_clause_still_could_not_run():
    text = "with data expected in Q1 2027 and a filing in the second half of 2027"
    quote = "The program is on track, with data expected in Q1 2027 and a filing in the second half of 2027."
    assert date_match(text, None, quote, None, filed=FILED).result == COULD_NOT_RUN
