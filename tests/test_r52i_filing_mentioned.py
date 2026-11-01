"""Job 52i (CVA ruling 2026-10-31): "Submission is mentioned, but a filing event is not established."
A filing noun establishes filing only when it ends its phrase (next token: end of clause or _AGENCY_END)."""
import pytest
from checks import event_checks as ec
from checks.common import PASS

BOARD = "The board's approval of the FDA submission budget is expected in Q1 2027."
MENTIONED = [(BOARD, "Submission"), ("submission timeline update", "Submission"),
             ("The filing strategy for PN-881 is set in 2027.", "Filing"),
             ("A resubmission plan is expected in 2027.", "Resubmission"),
             ("regulatory submissions planned for 2027", "Submissions")]   # Job 52k: "planned" is not on the list
FILING = ["BLA submission in 2027", "NDA accepted for review", "submitted the BLA.", "the filing is expected in Q3",
          "The BLA submission.", "Submission of the NDA is expected in 2027.",
          "The resubmission was accepted for review in Q1 2027."]


@pytest.mark.parametrize("text,word", MENTIONED)
def test_mentioned_filing_word_is_unresolved_with_cva_sentence(text, word):
    assert ec.event_type_for(text) == "unknown"
    assert ec.event_type_for(text, loose=True) == "unknown"       # no inference on the loose path either
    assert ec.event_type_reason(text) == ec.EVENT_MENTIONED_NOT_ESTABLISHED
    sentence = f"{word} {'are' if word.endswith('s') else 'is'} mentioned, but a filing event is not established."
    assert ec.reason_words(ec.EVENT_MENTIONED_NOT_ESTABLISHED, text) == sentence
    r = ec.type_match("filing", text)
    assert r.result == "unresolved" and r.reason == sentence    # not fail, not pass


@pytest.mark.parametrize("text", FILING)
def test_filing_word_that_ends_its_phrase_stays_filing(text):
    assert ec.event_type_for(text) == "filing"
    assert ec.event_type_reason(text) is None
    assert ec.type_match("filing", text).result == PASS


def test_board_sentence_regulatory_row_stays_unresolved():
    assert ec.type_match("regulatory_decision", BOARD).result == "unresolved"


def test_unresolved_type_match_routes_to_judgment_needed_and_keeps_quote():
    from runs import build_entry, state
    r = ec.type_match("filing", BOARD)
    other = ec.CheckResult("date_match", "Date", PASS, "ok", {})
    status = ec.row_status([other, r])
    assert status != ec.MATCHED and status != ec.FAILED                # not kept, not a mistake
    group = build_entry.START_GROUP.get(status, "unverified")
    assert group in state.UNRESOLVED                                    # Judgment needed
    assert BOARD == "The board's approval of the FDA submission budget is expected in Q1 2027."   # quote untouched


def test_mentioned_word_still_anchors_the_date_clause():
    # the type is unresolved ("planned" is not on the closed list), but the date check still finds the clause
    q = "NDA submission planned in Aug 2026."
    assert ec.type_match("filing", q).result == "unresolved"
    assert ec.date_match("Aug 2026", "2026-08-01", q, "2026-08-31", event_type="filing").result == PASS


def test_schema_allows_new_reason():
    import json
    schema = json.load(open("runs/schema/run.schema.json"))
    assert "event_mentioned_not_established" in json.dumps(schema)
