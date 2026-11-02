# PIVOT-001: original classifier probes are retained as diagnostics; type_match
# is retired. Both positive and negative interpretations require human review.
from tests.pivot_assertions import assert_proposal_held
"""An agency inside the decision's object does not establish its decision maker."""
import pytest
from checks import event_checks as ec
from checks.common import PASS

@pytest.mark.parametrize('text', [
    "The board's approval of the FDA submission budget is expected in Q1 2027.",
    "The company made its decision from FDA feedback to advance PN-881 in Q1 2027.",
])
def test_company_actor_does_not_become_regulator(text):
    kind = ec.event_type_for(text)
    result = ec.type_match('regulatory_decision', text)
    assert kind != 'regulatory_decision' and result.result != PASS, (kind, result)
    assert_proposal_held(text, 'regulatory_decision')

@pytest.mark.parametrize('text', [
    'FDA approval of PN-881 is expected in Q1 2027.',
    'A decision by the FDA is expected in Q1 2027.',
    'Approval from the EMA is expected in Q1 2027.',
])
def test_agency_actor_positive_controls(text):
    assert ec.event_type_for(text) == 'regulatory_decision'
    assert ec.type_match('regulatory_decision', text).result == 'not_needed'
    assert_proposal_held(text, 'regulatory_decision')
