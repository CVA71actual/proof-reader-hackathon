import pytest

from checks import event_checks
from report import data as report_data
from runs.recheck import could_not_run_reason
from runs.state import current_state
from tests.test_r49_status_owner import _check, _row, _run


def _corrected_state(result):
    run = _run(_row("matched", "kept", [_check("date_match")]))
    fix = {"field": "date", "from": "2026-08-01", "to": "2026-08-05"}
    rc = {"result": result, "reason": "r", "checks": []}
    decisions = [
        {"at": "2026-08-10T11:00:00+00:00", "target": "e1", "action": "fix", "note": None, "fix": fix,
         "by": "person"},
        {"at": "2026-08-10T11:00:30+00:00", "target": "e1", "action": "recheck", "note": None, "fix": None,
         "by": "code", "recheck": rc},
    ]
    return current_state(run, decisions)


@pytest.mark.parametrize("result", ["unresolved", "could_not_run"])
def test_an_unresolved_recheck_is_treated_like_could_not_run(result):
    st = _corrected_state(result)
    t = st["targets"]["e1"]
    assert t["group"] == "corrected" and t["recheck"]["result"] == result
    assert st["unresolved"] == 1, "needs attention"
    assert st["counts"]["fixes_pending"] == 1


def test_a_passing_recheck_does_not_need_attention():
    st = _corrected_state("pass")
    assert st["unresolved"] == 0 and st["counts"]["fixes_pending"] == 0


def test_report_text_says_unresolved():
    text = report_data._check_status_text({"recheck": {"result": "unresolved"}, "effective_checks": [1]})
    assert "unresolved" in text.lower() and "could not run" not in text


def test_ui_needs_attention_tests_unresolved():
    src = open("ui/app.js").read()
    line = next(l for l in src.splitlines() if "t.needs_recheck ||" in l and "undecided" in l)
    assert "'unresolved'" in line


def test_could_not_run_names_its_own_reason_not_the_unresolved_one():
    checks = [{"result": "unresolved", "reason": "U"}, {"result": "could_not_run", "reason": "C"}]
    assert could_not_run_reason(checks).endswith("C")


@pytest.mark.parametrize("q", ["The patient file was updated in 2027", "Market acceptance of the product in 2027"])
def test_bare_file_and_acceptance_are_not_filing_evidence(q):
    assert event_checks.event_type_for(q) != "filing"
    assert event_checks.event_type_for(q) == "unknown"


def test_data_on_file_reads_as_trial_results():
    # Operator ruling: bare "data" stays trial-results evidence; "Data on file" is a recorded false-success shape.
    assert event_checks.event_type_for("Data on file 2027") == "trial_results"


@pytest.mark.parametrize("q", ["NDA accepted for review", "BLA submission"])
def test_accepted_for_review_and_submission_are_filing(q):
    assert event_checks.event_type_for(q) == "filing"


@pytest.mark.parametrize("result", ["unresolved", "could_not_run"])
def test_edit_status_treats_unresolved_like_could_not_run(result):
    run = _run(_row("matched", "kept", [_check("date_match")]))
    run["run"].update({"id": "r1", "number": 1})
    fix = {"field": "date", "from": "2026-08-01", "to": "2026-08-05"}
    decisions = [
        {"at": "2026-08-10T11:00:00+00:00", "target": "e1", "action": "fix", "note": None, "fix": fix,
         "by": "person"},
        {"at": "2026-08-10T11:00:30+00:00", "target": "e1", "action": "recheck", "note": None, "fix": None,
         "by": "code", "recheck": {"result": result, "reason": "r", "checks": []}},
    ]
    view = report_data.build_view(run, decisions, generated_at="2026-08-11T00:00:00+00:00")
    assert [e["edit_status"] for e in view["edits"]] == ["This change was not demonstrated to be a mistake."]


def test_ui_empty_checks_text_names_unresolved():
    src = open("ui/app.js").read()
    i = src.index("No checks ran on the current value yet")
    block = src[i:i + 600]
    assert "result === 'unresolved'" in block and "is unresolved" in block
