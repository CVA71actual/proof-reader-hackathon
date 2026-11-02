"""Job 52i (CVA ruling 2026-10-31): "Submission is mentioned, but a filing event is not established."
A filing noun establishes filing only when it ends its phrase (next token: end of clause or _AGENCY_END).
PIVOT-001: retain these lexical diagnostics and original inputs, but the retired
type check cannot accept meaning. Every proposal must remain held for review."""
import pytest
from checks import event_checks as ec
from checks.common import PASS, NOT_NEEDED
from tests.pivot_assertions import assert_proposal_held

BOARD = "The board's approval of the FDA submission budget is expected in Q1 2027."
MENTIONED = [(BOARD, "Submission"), ("submission timeline update", "Submission"),
             ("The filing strategy for PN-881 is set in 2027.", "Filing"),
             ("A resubmission plan is expected in 2027.", "Resubmission"),
             ("regulatory submissions planned for 2027", "Submissions")]   # Job 52k: "planned" is not on the list
FILING = ["BLA submission in 2027", "NDA accepted for review", "submitted the BLA.", "the filing is expected in Q3",
          "The BLA submission.", "Submission of the NDA is expected in 2027.",
          "The resubmission was accepted for review in Q1 2027."]


@pytest.mark.parametrize("text,word", MENTIONED)
def test_mention_diagnostic_is_preserved_but_meaning_needs_review(text, word):
    assert ec.event_type_for(text) == "unknown"
    assert ec.event_type_for(text, loose=True) == "unknown"       # no inference on the loose path either
    assert ec.event_type_reason(text) == ec.EVENT_MENTIONED_NOT_ESTABLISHED
    sentence = f"{word} {'are' if word.endswith('s') else 'is'} mentioned, but a filing event is not established."
    assert ec.reason_words(ec.EVENT_MENTIONED_NOT_ESTABLISHED, text) == sentence
    r = ec.type_match("filing", text)
    assert r.result == NOT_NEEDED  # PIVOT: diagnostic wording cannot certify meaning
    assert_proposal_held(text, "filing")


@pytest.mark.parametrize("text", FILING)
def test_filing_word_that_ends_its_phrase_stays_filing(text):
    assert ec.event_type_for(text) == "filing"
    assert ec.event_type_reason(text) is None
    assert ec.type_match("filing", text).result == NOT_NEEDED
    assert_proposal_held(text, "filing")


def test_board_sentence_regulatory_row_stays_unresolved():
    assert ec.type_match("regulatory_decision", BOARD).result == NOT_NEEDED
    assert_proposal_held(BOARD, "regulatory_decision")


def test_retired_type_check_leaves_proposal_held_without_a_mistake():
    run, state = assert_proposal_held(BOARD, "filing")
    assert all(value == 0 for value in state["mistakes"].values())
    assert state["mistake_list"] == []
    assert run["rows"][0]["quote"] == BOARD


def test_literal_date_pass_does_not_accept_the_filing_proposal():
    # PIVOT: date presence/math is mechanical; meaning remains a human decision.
    q = "NDA submission planned in Aug 2026."
    assert ec.type_match("filing", q).result == NOT_NEEDED
    assert_proposal_held(q, "filing", date_text="Aug 2026", start="2026-08-01", end="2026-08-31")
    assert ec.date_match("Aug 2026", "2026-08-01", q, "2026-08-31", event_type="filing").result == PASS


def test_schema_allows_new_reason():
    import json
    schema = json.load(open("runs/schema/run.schema.json"))
    assert "event_mentioned_not_established" in json.dumps(schema)
