"""Job 52d: the regulator must be bound to the decision/approval noun by shape, not merely stand near it; an unknown
says which part is unknown (agency_not_established / event_type_not_established)."""

import pytest

from checks import event_checks as ec
from checks.common import FAIL, NOT_NEEDED, PASS, UNRESOLVED

REG = "regulatory_decision"

POSITIVE = [
    "FDA decision expected", "decision by the FDA", "decision by the U.S. FDA expected", "the FDA's decision",
    "FDA approval", "approval from the EMA", "approval from the European Medicines Agency", "Health Canada approval",
    "PDUFA target action date", "CHMP opinion", "CRL", "MAA decision", "FDA decisions", "key regulatory decisions",
    "The U.S. FDA approval is expected. Nothing else", "MHRA approval", "PMDA decision", "NMPA approval",
]
FALSE_SUCCESSES = [
    "The company told the FDA its decision to discontinue PN-881",
    "a decision of the regulatory affairs team to delay the filing",
    "the regulatory strategy decision by management",
    "approval of the BLA expected in 2027",
    "the FDA's expected marketing approval",
    "approval expected to be granted by the FDA",
    "decision by management",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_bound_regulator_is_regulatory(text):
    assert ec.event_type_for(text) == REG
    assert ec.type_match(REG, text).result == PASS
    assert ec.event_type_reason(text) is None


@pytest.mark.parametrize("text", FALSE_SUCCESSES)
def test_unbound_regulator_is_not_regulatory(text):
    assert ec.event_type_for(text) != REG       # (a real filing word may still make it a filing)
    assert ec.type_match(REG, text).result in (NOT_NEEDED, UNRESOLVED, FAIL)
    assert ec.type_match(REG, text).result != PASS


def test_us_abbreviation_does_not_end_the_window():
    assert ec.event_type_for("decision by the U.S. FDA expected") == REG
    assert ec.event_type_for("The U.S. FDA decision") == REG


def test_application_approval_is_not_a_filing():
    assert ec.event_type_for("approval of the BLA expected in 2027") == "unknown"
    assert ec.event_type_for("approval of the sNDA") == "unknown"
    assert ec.event_type_for("BLA approval expected") == "unknown"
    assert ec.event_type_for("submitted the BLA") == "filing"      # a real filing word stays a filing


def test_agency_not_established_is_unresolved_not_pass_not_fail():
    for text in ("approval of the BLA expected in 2027", "a decision is expected", "the FDA's expected marketing approval"):
        assert ec.event_type_reason(text) == ec.AGENCY_NOT_ESTABLISHED
        r = ec.type_match(REG, text)
        assert r.result == UNRESOLVED
        assert "agency not named" in r.reason
    assert "decision named, agency not named" in ec.type_match(REG, "a decision is expected").reason
    assert ec.reason_words(ec.AGENCY_NOT_ESTABLISHED, "US approval") == "approval named, agency not named"


def test_event_type_not_established():
    text = "The company will hold its annual meeting of shareholders"
    assert ec.event_type_for(text) == "unknown"
    assert ec.event_type_reason(text) == ec.EVENT_TYPE_NOT_ESTABLISHED
    r = ec.type_match(REG, text)
    assert r.result == NOT_NEEDED and "no event named" in r.reason.lower()
    assert ec.reason_words(ec.EVENT_TYPE_NOT_ESTABLISHED) == "no event named"


def test_e58_a_missing_agency_does_not_erase_what_is_supported():
    text = "Potential US approval/launch"
    assert ec.event_type_for(text) == "launch"                       # supported, stays
    assert ec.event_type_reason(text) == ec.AGENCY_NOT_ESTABLISHED   # the approval reading is recorded, not discarded
    r = ec.type_match(REG, text)                                      # a stored regulatory row: unresolved, not fail
    assert r.result == UNRESOLVED and "agency not named" in r.reason
    assert ec.type_match("launch", text).result == PASS


def test_unresolved_keeps_a_row_from_matching():
    r = ec.type_match(REG, "a decision is expected")
    assert ec.row_status([r]) == ec.PROPOSED


# Untested surface (R60, Devin Job 3): "Board approval from regulators is not required" is a NEGATION case. It binds
# as regulatory here (shape 2, "from regulators"); the negation is not handled in this job.
