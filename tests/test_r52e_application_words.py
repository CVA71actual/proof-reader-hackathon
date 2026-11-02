# PIVOT-001: original classifier probes are retained as diagnostics; type_match
# is retired. Both positive and negative interpretations require human review.
from tests.pivot_assertions import assert_proposal_held
import pytest

from checks import event_checks
from checks.common import NOT_NEEDED, COULD_NOT_RUN
from runs.recheck import recheck_line, recheck_result


@pytest.mark.parametrize("q", [
    "Approval of our BLA expected in 2027", "Approval of its NDA expected in 2027",
    "Approval of the pending BLA expected in 2027", "Approval of the U.S. BLA expected in 2027",
    "Approval of the BLA expected in 2027"])
def test_an_application_word_is_not_filing_evidence(q):
    assert event_checks.event_type_for(q) == "unknown"
    assert event_checks.event_type_reason(q) == "agency_not_established"
    assert event_checks.type_match("filing", q).result != "pass"


@pytest.mark.parametrize("q", ["BLA submission in 2027", "The company submitted the BLA in 2027",
                               "Regulatory submissions are expected in Q1 2027", "The NDA was accepted for review"])
def test_a_filing_word_makes_a_filing(q):
    assert event_checks.event_type_for(q) == "filing"
    assert event_checks.type_match("filing", q).result == NOT_NEEDED
    assert_proposal_held(q, "filing")


def test_a_reason_is_set_only_when_no_noun_is_bound():
    q = "FDA approval and Board approval expected in 2027"
    assert event_checks.event_type_for(q) == "regulatory_decision"
    assert event_checks.event_type_reason(q) is None
    assert event_checks.type_match("regulatory_decision", q).result == NOT_NEEDED
    assert_proposal_held(q, "regulatory_decision")


def test_retired_type_recheck_cannot_validate_meaning():
    q = "Approval of our BLA expected in 2027"
    checks = [event_checks.type_match("regulatory_decision", q).to_dict()
              if hasattr(event_checks.type_match("regulatory_decision", q), "to_dict")
              else dict(vars(event_checks.type_match("regulatory_decision", q)))]
    assert checks[0]["result"] == NOT_NEEDED
    # Use the production field-aware path: a semantic edit has no code check.
    assert recheck_result(checks, "row", "event_type") == COULD_NOT_RUN
    line = recheck_line("t", "e1", checks, "row", "event_type")["recheck"]
    assert line["result"] == COULD_NOT_RUN
    assert line["checks"] == checks
    assert "could not run" in line["reason"]
    assert_proposal_held(q, "regulatory_decision")
