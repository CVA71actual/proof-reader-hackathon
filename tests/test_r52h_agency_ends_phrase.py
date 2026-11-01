"""Job 52h: "of" never binds an agency; by/from bind only when the agency ends its phrase."""
import pytest
from checks import event_checks as ec
from checks.common import PASS

POS = ["A decision by the FDA.", "The decision by the U.S. FDA expected in Q1 2027.",
       "Approval from the EMA in Q1 2027.", "Approval from Health Canada.", "FDA approval is expected.",
       "The FDA's decision is expected.", "PDUFA target action date is in Q1 2027."]
NEG = ["The board's approval of the FDA submission budget is expected in Q1 2027.",
       "The company made its decision from FDA feedback to advance PN-881 in Q1 2027.",
       "Approval from the FDA meeting is expected.", "Decision of the PMDA is expected in Q1 2027."]

@pytest.mark.parametrize("t", POS)
def test_positive(t):
    assert ec.event_type_for(t) == "regulatory_decision"
    assert ec.type_match("regulatory_decision", t).result == PASS

@pytest.mark.parametrize("t", NEG)
def test_negative(t):
    # sentence 1 reads as "filing" (its own word "submission"), never regulatory_decision; the rest are unknown
    assert ec.event_type_for(t) in ("unknown", "filing")
    r = ec.type_match("regulatory_decision", t)
    assert r.result == "unresolved"
