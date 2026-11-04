"""DUPLICATE-ROWS-001 (CVA 2026-11-03, live run 9 rows e10/e28: "it shouldn't accept something twice").

Rows of the same company on the same exact day whose normalized titles match are one event: only the first in row
order may be placed automatically; each later one goes to review with a possible-duplicate issue. Checks are not
changed, nothing is merged or deleted, a person's Keep still wins, periods are out of scope. All data synthetic.
"""
import copy

from runs.calendar import run_interval_partition
from runs.state import _norm_title, current_state
from tests.test_pivot_proposal_flow import AT, make_run

TITLE = "Truist Securities 2026 Biopharma Symposium participation"


def _one(quote, date_text, day, *, title=TITLE, company="Cedar Bio"):
    _, run = make_run(("custom", quote, "conference", date_text, day, []),
                      event_updates={"title": title, "company": company, "subject_company": company})
    return run


def combine(*runs):
    """One run holding each run's single row as e1, e2, ... All rows cite the same synthetic page (their saved checks
    are bound to it), so a pair on different days shares one quote that prints both days."""
    out = copy.deepcopy(runs[0])
    out["rows"] = []
    for i, r in enumerate(runs, 1):
        assert r["sources"] == out["sources"]
        row = copy.deepcopy(r["rows"][0])
        row["id"] = f"e{i}"
        out["rows"].append(row)
    return out


def truist(date_text="Thursday, November 12, 2026", **kw):
    return _one("Date: Thursday, November 12, 2026", date_text, "2026-11-12", **kw)


def keep(state, target):
    return {"at": AT, "action": "keep", "by": "person", "target": target, "fix": None, "note": None,
            "review_basis": state["targets"][target]["review_basis"]}


def test_single_rows_are_clean_baseline():
    for run in (truist(), truist("November 12, 2026")):
        t = current_state(run, [])["targets"]["e1"]
        assert t["issues"] == [] and t["inclusion"] == "included"


def test_truist_pair_first_placed_second_needs_review():
    # live: "Date: Thursday, November 12, 2026" vs "Thursday, November 12, 2026"; the "Date:" wording is another
    # writer's date-check change, so the pair here differs in date wording that passes today.
    run = combine(truist("Thursday, November 12, 2026"), truist("November 12, 2026"))
    checks_before = [copy.deepcopy(r.get("checks")) for r in run["rows"]]
    state = current_state(run, [])
    first, second = state["targets"]["e1"], state["targets"]["e2"]
    assert first["inclusion"] == "included" and first["issues"] == []
    assert second["inclusion"] == "review" and second["needs_attention"] and not second["on_calendar"]
    assert second["issues"] == [f"Same event as e1 ({TITLE}, 2026-11-12) — possible duplicate"]
    assert [r.get("checks") for r in run["rows"]] == checks_before        # checks untouched, nothing stored
    assert first["effective_checks"] == current_state(combine(truist()), [])["targets"]["e1"]["effective_checks"]
    partition, _, _ = run_interval_partition(run, state)
    assert [e["id"] for e in partition["calendar"]] == ["e1"]


def test_title_normalization_case_punctuation_and_date_phrase():
    a = combine(truist(), truist(title="truist securities 2026 biopharma symposium participation — November 12, 2026"))
    assert current_state(a, [])["targets"]["e2"]["inclusion"] == "review"
    b = combine(truist(), truist(title="  TRUIST Securities, 2026 Biopharma-Symposium participation. "))
    assert current_state(b, [])["targets"]["e2"]["inclusion"] == "review"
    assert _norm_title("On Thursday, November 12, 2026: Truist Symposium") == "truist symposium"
    assert _norm_title("Truist Symposium (2026-11-12)") == "truist symposium"


def test_different_days_both_placed():
    two_days = "Symposium: Thursday, November 12, 2026. Fireside: Friday, November 13, 2026."
    a = _one(two_days, "Thursday, November 12, 2026", "2026-11-12")
    b = _one(two_days, "Friday, November 13, 2026", "2026-11-13")
    state = current_state(combine(a, b), [])
    assert [state["targets"][t]["inclusion"] for t in ("e1", "e2")] == ["included", "included"]
    same_day = current_state(combine(a, _one(two_days, "November 12, 2026", "2026-11-12")), [])
    assert same_day["targets"]["e2"]["inclusion"] == "review"            # control: same page, same day -> duplicate


def test_same_day_different_titles_both_placed():
    # DUPLICATE-RESOLUTION-001: different named meetings on one day are different events. (A fireside chat at the
    # same Truist symposium is the same named meeting and now pairs: tests/test_duplicate_resolution.py.)
    state = current_state(combine(truist(title="Wolfe Research Healthcare Conference participation"),
                                  truist(title="UBS Global Biopharma Conference participation")), [])
    assert [state["targets"][t]["inclusion"] for t in ("e1", "e2")] == ["included", "included"]


def test_different_companies_both_placed():
    state = current_state(combine(truist(), truist(company="Birch Therapeutics")), [])
    assert [state["targets"][t]["inclusion"] for t in ("e1", "e2")] == ["included", "included"]


def test_person_keep_on_the_duplicate_keeps_it():
    run = combine(truist(), truist())
    state = current_state(run, [])
    kept = current_state(run, [keep(state, "e2")])
    assert kept["targets"]["e2"]["inclusion"] == "kept" and kept["targets"]["e2"]["accepted"]
    # DUPLICATE-RESOLUTION-001: the kept copy is the one on the calendar; the automatic copy now waits for review.
    assert kept["targets"]["e1"]["inclusion"] == "review"
    assert kept["targets"]["e1"]["duplicate_of"]["row_id"] == "e2"


def test_removed_first_row_does_not_hold_back_the_later_one():
    run = combine(truist(), truist())
    removed = {"at": AT, "action": "remove", "by": "person", "target": "e1", "fix": None, "note": None}
    state = current_state(run, [removed])
    assert state["targets"]["e1"]["inclusion"] == "excluded"
    assert state["targets"]["e2"]["inclusion"] == "included"


def test_periods_out_of_scope():
    q = _one("Cedar Bio expects the readout in Q1 2027.", "Q1 2027", "2027-01-01", title="Readout")
    state = current_state(combine(q, q), [])
    assert not any("possible duplicate" in i for t in state["targets"].values() for i in (t["issues"] or []))
