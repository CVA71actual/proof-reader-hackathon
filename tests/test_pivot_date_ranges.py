"""DATE-LITERAL-001: one endpoint is not the whole printed range."""
import pytest
from checks.event_checks import date_match


@pytest.mark.parametrize('wording,start,end,quote', [
    ('2027', '2027-01-01', '2027-12-31', 'Data in 2026-2027.'),
    ('2026', '2026-01-01', '2026-12-31', 'Data in 2026-2027.'),
    ('Q3 2027', '2027-07-01', '2027-09-30', 'Launch window Q3 2027 to Q1 2028.'),
    ('Q1 2028', '2028-01-01', '2028-03-31', 'Launch window Q3 2027 through Q1 2028.'),
    ('Q3 ’27', '2027-07-01', '2027-09-30', 'Window Q3 ’27–Q1 ’28.'),
    ('March 12, 2027', '2027-03-12', None, 'Window March 12, 2027 to April 14, 2027.'),
    ('2026-2027', '2026-01-01', '2027-12-31', 'Data in 2026-2027.'),
    ('Q3 2027 to Q1 2028', '2027-07-01', '2028-03-31', 'Window Q3 2027 to Q1 2028.'),
])
def test_range_endpoints_and_unparsed_full_ranges_stay_unresolved(wording, start, end, quote):
    check = date_match(wording, start, quote, end)
    assert check.result == 'could_not_run', check.reason
    assert 'range' in check.reason


@pytest.mark.parametrize('wording,start,end,quote', [
    ('2027', '2027-01-01', '2027-12-31', 'Data in 2027.'),
    ('Q3 2027', '2027-07-01', '2027-09-30', 'Launch in Q3 2027. Another trial ran 2025-2026.'),
    ('Q3 2027', '2027-07-01', '2027-09-30', 'Launch in Q3 2027 and trial results in Q1 2028.'),
    ('November 14–17, 2026', '2026-11-14', '2026-11-17', 'Conference November 14–17, 2026.'),
])
def test_standalone_periods_and_supported_whole_day_range_still_compare(wording, start, end, quote):
    assert date_match(wording, start, quote, end).result == 'pass'
