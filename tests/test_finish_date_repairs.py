"""Offline finish-path cases: real printed timestamps and equivalent period spelling."""
from datetime import date

import pytest

from checks import event_checks as ec


@pytest.mark.parametrize("wording", [
    "Oct 02, 2026, 06:00 ET", "Oct 02, 2026, 23:59 ET",
    "Oct 02, 2026 at 00:00 UTC", "Oct 02, 2026, 6:00 a.m. ET",
])
def test_printed_timestamp_parses_and_reaches_date_check(wording):
    assert ec.parse_date_phrase(wording) == ("day", date(2026, 10, 2), date(2026, 10, 2))
    result = ec.date_match(wording, "2026-10-02", wording)
    assert result.result == "pass"
    assert result.compared["timing"]["start"] == "2026-10-02"
    assert ec.date_match(wording, "2026-10-03", wording).result == "fail"


@pytest.mark.parametrize("wording", ["Oct 02, 2026, 25:00 ET", "Oct 02, 2026, 06:99 ET"])
def test_invalid_clock_is_not_silently_removed(wording):
    assert ec.parse_date_phrase(wording)[0] == "none"


@pytest.mark.parametrize("wording,printed", [
    ("by year-end", "by year - end"), ("by year - end", "by year-end"),
])
def test_relative_period_uses_same_spelling_normalization_as_catalogue(wording, printed):
    result = ec.date_match(wording, "2026-09-01", f"Results expected {printed}.",
                           date_end="2026-12-31", filed="2026-08-05")
    assert result.result == "pass"
    assert ec.date_match(wording, "2027-09-01", f"Results expected {printed}.",
                         date_end="2027-12-31", filed="2026-08-05").result == "fail"
    assert ec.date_match(wording, "2026-09-01", "Results expected next quarter.",
                         date_end="2026-12-31", filed="2026-08-05").result == "fail"
