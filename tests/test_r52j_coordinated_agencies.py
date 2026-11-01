"""Job 52j: coordinated agencies are not an actor; a comma after an agency ends it only before a listed word."""
import pytest

from checks import event_checks as ec
from checks.common import PASS


def test_a_comma_then_a_listed_word_ends_the_agency_phrase():
    q = "Approval by the MHRA, expected in 2027."
    assert ec.event_type_for(q) == "regulatory_decision" and ec.type_match("regulatory_decision", q).result == PASS


@pytest.mark.parametrize("agencies", ["FDA and EMA", "FDA or EMA", "FDA, EMA and MHRA"])
def test_coordinated_agency_feedback_is_unknown_agency_not_established(agencies):
    q = f"The company made its decision from {agencies} feedback to advance PN-881 in Q1 2027."
    assert ec.event_type_for(q) == "unknown"
    assert ec.event_type_reason(q) == ec.AGENCY_NOT_ESTABLISHED


def test_approval_from_two_coordinated_agencies_is_unresolved():
    check = ec.type_match("regulatory_decision", "Approval from the FDA and EMA is expected in 2027.")
    assert check.result == "unresolved" and "agency not named" in check.reason
