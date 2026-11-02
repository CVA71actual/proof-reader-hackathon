# PIVOT-001: original classifier probes are retained as diagnostics; type_match
# is retired. Both positive and negative interpretations require human review.
from tests.pivot_assertions import assert_proposal_held
"""Check 30: coordination does not establish that an agency phrase has ended."""
import pytest
from checks import event_checks as ec
from checks.common import PASS

@pytest.mark.parametrize('agencies', ['FDA and EMA', 'FDA or EMA', 'FDA, EMA and MHRA'])
def test_company_decision_from_coordinated_agency_feedback_is_not_regulatory(agencies):
    quote = f'The company made its decision from {agencies} feedback to advance PN-881 in Q1 2027.'
    kind = ec.event_type_for(quote)
    check = ec.type_match('regulatory_decision', quote)
    assert kind != 'regulatory_decision' and check.result != PASS, (kind, check)
    assert_proposal_held(quote, 'regulatory_decision')

@pytest.mark.parametrize('quote', [
    'A decision by the FDA is expected in Q1 2027.',
    'Approval from the EMA in Q1 2027.',
    'FDA approval of PN-881 is expected in Q1 2027.',
])
def test_supported_agency_actor_controls(quote):
    assert ec.event_type_for(quote) == 'regulatory_decision'
    assert ec.type_match('regulatory_decision', quote).result == 'not_needed'
    assert_proposal_held(quote, 'regulatory_decision')
