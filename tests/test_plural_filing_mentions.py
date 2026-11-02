# PIVOT-001: original classifier probes are retained as diagnostics; type_match
# is retired. Both positive and negative interpretations require human review.
from tests.pivot_assertions import assert_proposal_held
"""A plural filing noun does not establish an event any more than a singular noun."""
import pytest
from checks import event_checks as ec

@pytest.mark.parametrize('noun', ['submissions', 'resubmissions', 'filings'])
def test_plural_budget_mentions_stay_unresolved(noun):
    quote = f"The board's approval of the FDA {noun} budget is expected in Q1 2027."
    kind, check = ec.event_type_for(quote), ec.type_match('filing', quote)
    assert kind == 'unknown' and check.result == 'not_needed', (kind, check)
    assert_proposal_held(quote, "filing")

@pytest.mark.parametrize('noun', ['submission', 'resubmission', 'filing'])
def test_singular_budget_control(noun):
    quote = f"The board's approval of the FDA {noun} budget is expected in Q1 2027."
    assert ec.event_type_for(quote) == 'unknown'
    assert ec.type_match('filing', quote).result == 'not_needed'
    assert_proposal_held(quote, 'filing')

def test_supported_plural_control():
    quote = 'Regulatory submissions are expected in Q1 2027.'
    assert ec.event_type_for(quote) == 'filing'
    assert ec.type_match('filing', quote).result == 'not_needed'
    assert_proposal_held(quote, 'filing')
