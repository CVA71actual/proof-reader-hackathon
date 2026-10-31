"""Job 49: one owner for row status (runs.state); a repair tool only replaces a check result."""
from pathlib import Path

from runs import recheck_dates as rd
from runs.state import current_state

DATE = {"name": "date_match", "label": "Date read as written", "kind": "Code", "result": "pass", "reason": "x",
        "compared": {}}


def _check(name, result="pass"):
    return {"name": name, "label": name, "kind": "Code", "result": result, "reason": "r", "compared": {}}


def _row(status, group, checks, disagreement=None):
    row = {"id": "e1", "source_id": "s1", "page_number": 1, "event_type": "trial_results", "title": "topline",
           "program": "Drug A", "quote": "topline results in August 2026.", "date_text": "August 2026",
           "date": "2026-08-01", "date_end": "2026-08-31", "status": status, "group": group, "reason": "",
           "trail": [], "checks": checks}
    if disagreement:
        row["disagreement"] = disagreement
    return row


def _run(row):
    return {"run": {"started_at": "2026-08-10T10:00:00+00:00", "choices": {}},
            "sources": [{"id": "s1", "published_date": "2026-08-01", "pages": []}],
            "rows": [row], "exclusions": [], "tables": [], "mistakes": []}


TWO_SIDED = {"field": "program", "first_reader": "Drug A", "second_reader": "Drug B"}


def test_a_date_recheck_does_not_resolve_a_two_sided_program_disagreement():
    checks = [_check(n) for n in ("quote_match", "type_match", "source_check", "page_match")] + [
        {**DATE, "reason": "an older explanation"}]
    run = _run(_row("undecided", "open", checks, TWO_SIDED))
    out = rd.recheck_dates(run, Path("."), set(), "t")
    assert out["mutated"] == ["e1"]                         # the date check was replaced...
    row = run["rows"][0]
    assert (row["status"], row["group"]) == ("undecided", "open")      # ...and the repair wrote no status
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "undecided" and t["group"] == "open"


def test_a_one_sided_disagreement_with_a_failed_check_does_not_become_matched():
    one_sided = {"field": "program", "first_reader": "Drug A", "second_reader": None}
    run = _run(_row("undecided", "open", [_check("quote_match", "fail"), DATE], one_sided))
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "undecided" and t["group"] == "open"


def test_a_one_sided_disagreement_with_a_missing_check_does_not_become_matched():
    one_sided = {"field": "program", "first_reader": "Drug A", "second_reader": None}
    run = _run(_row("undecided", "open", [], one_sided))
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "undecided" and t["group"] == "open"


def test_a_one_sided_disagreement_with_every_check_passing_is_matched():
    one_sided = {"field": "program", "first_reader": "Drug A", "second_reader": None}
    run = _run(_row("undecided", "open", [DATE], one_sided))
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "matched" and t["group"] == "kept"


# ---- Job 49b: one shared derivation (runs.state.derive_status) from the CURRENT effective checks
FIVE = ("quote_match", "type_match", "source_check", "page_match")


def _five(date_result="pass"):
    return [_check(n) for n in FIVE] + [{**DATE, "result": date_result}]


def _rechecked(run, new_result):
    run["rows"][0]["checks"][-1] = {**DATE, "result": new_result}
    return current_state(run, [])


def test_saved_pass_rechecked_fail_is_not_matched_and_not_on_the_calendar():
    run = _run(_row("matched", "kept", _five()))
    st = _rechecked(run, "fail")
    t = st["targets"]["e1"]
    assert (t["status"], t["group"]) == ("failed", "open")
    assert st["counts"]["rows"]["kept"] == 0
    from runs.calendar import on_calendar
    assert not on_calendar(t)


def test_saved_fail_rechecked_pass_is_matched_and_kept():
    run = _run(_row("failed", "open", _five("fail")))
    t = _rechecked(run, "pass")["targets"]["e1"]
    assert (t["status"], t["group"]) == ("matched", "kept")


def test_saved_fail_rechecked_pass_with_a_disagreement_stays_for_a_person():
    run = _run(_row("failed", "open", _five("fail"), TWO_SIDED))
    t = _rechecked(run, "pass")["targets"]["e1"]
    assert (t["status"], t["group"]) == ("undecided", "open")


def test_second_reader_only_row_still_needs_review_when_checks_pass():
    row = _row("undecided", "open", _five())
    row["found_by"] = "second_reader"
    t = current_state(_run(row), [])["targets"]["e1"]
    assert (t["status"], t["group"]) == ("undecided", "open")


def test_a_human_keep_overrides_the_derived_status():
    run = _run(_row("matched", "kept", _five("fail")))
    keep = {"action": "keep", "target": "e1", "by": "person", "at": "t"}
    t = current_state(run, [keep])["targets"]["e1"]
    assert t["group"] == "kept"


def test_a_code_exclusion_is_untouched_by_the_derivation():
    run = _run(_row("failed", "removed", _five("pass")))
    t = current_state(run, [])["targets"]["e1"]
    assert (t["status"], t["group"]) == ("failed", "removed")
