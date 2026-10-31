"""Job 39: the time window tests the period's END (GLASS 9 #6)."""
import json
from datetime import date

import pytest

from checks.event_checks import period_last_day
from runs import rewindow
from server.jobs import add_months, window_reason

NOW = date(2026, 10, 29)
LAST = add_months(NOW, 6)          # 2027-04-29


def row(text, start, end=None):
    return {"id": "e1", "date_text": text, "date": start, "date_end": end}


@pytest.mark.parametrize("text,start", [
    ("H2 2026", "2026-07-01"), ("Q4 2026", "2026-10-01"), ("by year end 2026", "2026-09-01"),
    ("end of 2026", "2026-09-01"), ("the second half of calendar year 2026", "2026-07-01")])
def test_a_period_ending_after_today_is_kept(text, start):
    assert window_reason(row(text, start), NOW, LAST, 6) is None


def test_a_period_ending_before_today_is_removed_with_the_end_named():
    assert window_reason(row("H1 2026", "2026-01-01"), NOW, LAST, 6) == (
        "Outside the time window: the period ends 2026-06-30, before today (2026-10-29).")


def test_date_end_wins_and_an_exact_past_day_reads_as_before():
    assert period_last_day(row("anything", "2026-07-01", "2026-12-31")) == date(2026, 12, 31)
    assert window_reason(row("anything", "2026-07-01", "2026-12-31"), NOW, LAST, 6) is None
    assert window_reason(row("Sept. 4, 2026", "2026-09-04"), NOW, LAST, 6) == (
        "Outside the time window: the date 2026-09-04 is before today (2026-10-29).")
    assert period_last_day(row("Sept. 4, 2026", "2026-09-04")) is None


def test_a_period_starting_after_the_window_end_is_removed():
    assert window_reason(row("H2 2027", "2027-07-01"), NOW, LAST, 6) == (
        "Outside the time window: the date 2027-07-01 is later than 6 months from today (2027-04-29).")


def test_undated_row_is_never_touched():
    assert window_reason(row(None, None), NOW, LAST, 6) is None


def make_run():
    def r(i, text, day, end, quote, group, status):
        checks = [{"name": "date_match", "label": "Date read as written", "result": "pass",
                   "reason": "supported", "compared": {}}]
        if i == "e3":
            checks.append({"name": "quote_match", "label": "Quote found on page", "result": "fail",
                           "reason": "not found", "compared": {}})
        return {"id": i, "date_text": text, "date": day, "date_end": end, "quote": quote,
                "event_type": "trial_results", "title": "topline results", "group": group, "status": status,
                "reason": None, "trail": [], "checks": checks}
    rows = [r("e1", "H2 2026", "2026-07-01", "2026-12-31",
               "The Company expects topline results in the second half of calendar year 2026.", "removed", "matched"),
            r("e2", "H1 2026", "2026-01-01", "2026-06-30",
               "The Company expects topline results in the first half of calendar year 2026.", "removed", "matched"),
            r("e3", "Q4 2026", "2026-10-01", "2026-12-31",
               "The Company expects topline results in Q4 2026.", "removed", "failed"),
            r("e4", "Q4 2026", "2026-10-01", "2026-12-31",
               "The Company expects topline results in Q4 2026.", "removed", "matched")]  # a person decided this one
    old = "Already past: the event date {} was before this run began (2026-10-29)."
    ex = [{"id": f"x{n}", "target": x["id"], "rule": "event_in_past", "by": "code",
           "reason": old.format(x["date"])} for n, x in enumerate(rows, 1)]
    return {"run": {"choices": {"time_window_months": 6}, "started_at": "2026-10-29T10:00:00+00:00"},
            "rows": rows, "tables": [], "mistakes": [], "exclusions": ex}


def test_rewindow_puts_back_the_right_rows_and_leaves_person_decisions_alone(tmp_path, capsys):
    d = tmp_path / "r1"
    d.mkdir()
    run = make_run()
    (d / "run.json").write_text(json.dumps(run))
    (d / "decisions.jsonl").write_text(json.dumps({"action": "remove", "target": "e4", "note": "no"}) + "\n")
    before = (d / "run.json").read_bytes()
    assert rewindow.main(["r1", "--runs-dir", str(tmp_path)]) == 0
    assert (d / "run.json").read_bytes() == before                         # dry run saves nothing
    assert "2 put back" in capsys.readouterr().out
    assert rewindow.main(["--all", "--runs-dir", str(tmp_path), "--write"]) == 0
    saved = json.loads((d / "run.json").read_text())
    by = {x["id"]: x for x in saved["rows"]}
    assert (by["e1"]["group"], by["e3"]["group"]) == ("kept", "open")
    assert by["e2"]["group"] == "removed" and by["e4"]["group"] == "removed"
    assert [x["target"] for x in saved["exclusions"]] == ["e2", "e4"]
    step = by["e1"]["trail"][-1]
    assert (step["step"], step["tag"], step["text"]) == ("Put back", "Code",
                                                          "the past-event rule now tests the period's end against the run date")
    assert by["e2"]["trail"][-1]["step"] == "Date re-checked" and by["e4"]["trail"] == []
